"""Semantic Scholar connector (Graph API paper search). A server key is optional; without one
the shared public rate limit often answers 429, which is retried and then reported."""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, get_with_retry, user_agent

BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = ("title,abstract,year,venue,authors,externalIds,citationCount,isOpenAccess,"
          "openAccessPdf,publicationTypes,url")
PAGE_SIZE = 100  # the endpoint's largest `limit`
MAX_RECORDS = 1000  # /paper/search returns at most 1,000 results per query
JOURNAL_TYPES = ("JournalArticle", "Review")


def classify_record(record: dict) -> tuple[str, str]:
    """Semantic Scholar paper -> (category, basis), from `publicationTypes` only.

    Conference -> proceedings (it wins when a record is also tagged JournalArticle, as
    proceedings papers often are); JournalArticle or Review -> peer_reviewed; else other.
    """
    types = [t for t in record.get("publicationTypes") or [] if isinstance(t, str)]
    if "Conference" in types:
        return "proceedings", "publication type 'Conference'"
    hit = next((t for t in types if t in JOURNAL_TYPES), None)
    if hit:
        return "peer_reviewed", f"publication type '{hit}'"
    if types:
        return "other", "publication types " + ", ".join(repr(t) for t in types) + \
            " not decisive"
    return "other", "no publication type in the record"


def can_return(profile: SearchProfile) -> bool:
    types = profile.document_types
    return not types or bool({"peer_reviewed", "proceedings", "other"} & set(types))


def parse_record(record: dict) -> Paper:
    authors = [a.get("name", "").strip() for a in record.get("authors") or []
               if isinstance(a, dict) and (a.get("name") or "").strip()]
    pdf = record.get("openAccessPdf") or {}
    category, basis = classify_record(record)
    return Paper(
        source="semanticscholar",
        id=str(record.get("paperId") or ""),
        doi=((record.get("externalIds") or {}).get("DOI") or "").lower(),
        title=(record.get("title") or "").strip(),
        abstract=(record.get("abstract") or "").strip(),
        year=record.get("year") if isinstance(record.get("year"), int) else None,
        native_type="; ".join(t for t in record.get("publicationTypes") or []
                              if isinstance(t, str)),
        category=category,
        category_basis=basis,
        venue=record.get("venue") or "",
        first_author=authors[0] if authors else "",
        authors=authors,
        citations=record.get("citationCount") or 0,
        is_oa=bool(record.get("isOpenAccess")),
        oa_url=(pdf.get("url") or "") if isinstance(pdf, dict) else "",
        landing_url=record.get("url") or "",
    )


def build_params(profile: SearchProfile, offset: int, limit: int) -> dict:
    return {"query": profile.query, "offset": offset, "limit": limit,
            "year": f"{profile.year_from}-{profile.year_to}", "fields": FIELDS}


class SemanticScholarConnector:
    name = "semanticscholar"
    tier = "open"
    # Years are a request parameter; document types come from classifying every record.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset({"peer_reviewed", "proceedings", "other"})
    supported_sorts = frozenset({"relevance"})

    def __init__(
        self,
        api_key: str | None = None,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._api_key = api_key
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._page_size = min(page_size, PAGE_SIZE)
        self.warnings: list[str] = []
        self.truncated = False

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not can_return(profile):
            return
        if not profile.query.strip():
            self.warnings.append("Semantic Scholar was not asked: its search needs query text.")
            return
        headers = {"User-Agent": user_agent(self._email)}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        offset = 0
        while True:
            limit = min(self._page_size, MAX_RECORDS - offset)
            try:
                response = get_with_retry(
                    self._client, BASE_URL, build_params(profile, offset, limit),
                    "Semantic Scholar", self._sleep, self._max_attempts, headers=headers)
            except ConnectorError as exc:
                if "HTTP 429" in str(exc) and not self._api_key:
                    raise ConnectorError(
                        f"{exc}. Without a key Semantic Scholar shares one public rate limit "
                        "between all users; a server key (SEMANTIC_SCHOLAR_API_KEY) gives its "
                        "own.") from None
                raise
            data = response.json()
            records = data.get("data") or []
            for record in records:
                if record.get("paperId"):
                    yield parse_record(record)
            offset += len(records)
            nxt = data.get("next")
            if not records or not isinstance(nxt, int):
                return
            if offset >= MAX_RECORDS:
                self.truncated = True
                self.warnings.append(f"Semantic Scholar returns at most {MAX_RECORDS} results "
                                     "per search; narrow the search to see the rest.")
                return
