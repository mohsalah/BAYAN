"""Europe PMC connector (no key). The REST API documents no contact parameter, so the
contact email goes in the User-Agent."""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import get_with_retry, user_agent

BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Europe PMC `LANG` values for the six supported languages.
LANG_CODES = {"en": "eng", "ar": "ara", "fr": "fre", "zh": "chi", "de": "ger", "es": "spa"}
LANG_BACK = {v: k for k, v in LANG_CODES.items()}


def build_query(profile: SearchProfile) -> str:
    parts = [f"(PUB_YEAR:[{profile.year_from} TO {profile.year_to}])"]
    if profile.query:
        parts.insert(0, f"({profile.query})")
    if profile.languages:
        parts.append("(" + " OR ".join(f"LANG:{LANG_CODES[c]}" for c in profile.languages) + ")")
    if profile.document_types and "other" not in profile.document_types:
        parts.append("(NOT SRC:PPR)")  # coarse pre-filter: drop preprints
    return " AND ".join(parts)


JOURNAL_PUB_TYPES = ("journal article", "research-article", "review", "review-article",
                     "systematic review")


def _native_type(record: dict) -> str:
    """The record's own type: 'preprint' for source PPR, else its publication types."""
    if record.get("source") == "PPR":
        return "preprint"
    return "; ".join((record.get("pubTypeList") or {}).get("pubType") or [])


def classify_result(record: dict) -> tuple[str, str]:
    """Europe PMC record -> (category, basis), from `source` and `pubTypeList` only.

    Source PPR (preprint) -> other. A journal article or review publication type ->
    peer_reviewed. Anything else, or no publication type, -> other with the reason.
    Peer-review reports (pub type 'peer review') are never counted as papers.
    """
    if record.get("source") == "PPR":
        return "other", "preprint (source PPR)"
    types = [t.lower() for t in ((record.get("pubTypeList") or {}).get("pubType") or [])]
    if not types:
        return "other", "no publication type in the record"
    if any(t in ("peer review", "peer-review") for t in types):
        return "other", "publication type 'peer review' is a review report, not a paper"
    hit = next((t for t in types if t in JOURNAL_PUB_TYPES), None)
    if hit:
        return "peer_reviewed", f"publication type '{hit}'"
    return "other", "publication types " + ", ".join(repr(t) for t in types) + " not decisive"


def can_return(profile: SearchProfile) -> bool:
    """Europe PMC yields only peer_reviewed and other; skip it when neither is selected."""
    types = profile.document_types
    return not types or "peer_reviewed" in types or "other" in types


def parse_result(record: dict) -> Paper:
    source, rid = record.get("source") or "", record.get("id") or ""
    urls = (record.get("fullTextUrlList") or {}).get("fullTextUrl") or []
    open_urls = [u for u in urls if u.get("availability") == "Open access" and u.get("url")]
    open_urls.sort(key=lambda u: u.get("documentStyle") != "pdf")
    year = record.get("pubYear")
    category, basis = classify_result(record)
    return Paper(
        source="europepmc",
        id=f"{source}:{rid}",
        doi=(record.get("doi") or "").lower().removeprefix("https://doi.org/"),
        title=(record.get("title") or "").strip(),
        abstract=(record.get("abstractText") or "").strip(),
        year=int(year) if str(year).isdigit() else None,
        native_type=_native_type(record),
        category=category,
        category_basis=basis,
        language=LANG_BACK.get(record.get("language") or "", ""),
        venue=((record.get("journalInfo") or {}).get("journal") or {}).get("title") or "",
        first_author=(record.get("authorString") or "").split(",")[0].strip(),
        citations=record.get("citedByCount") or 0,
        is_oa=record.get("isOpenAccess") == "Y",
        oa_url=open_urls[0]["url"] if open_urls else "",
        landing_url=f"https://europepmc.org/article/{source}/{rid}",
        keywords=list((record.get("keywordList") or {}).get("keyword") or []),
    )


class EuropePMCConnector:
    name = "europepmc"
    tier = "open"
    # Document type is a coarse pre-filter (no preprints unless `other` is chosen) plus
    # classification of every record. Affiliation country is not applied: AFF is free text.
    supported_filters = frozenset({"years", "languages", "document_types"})
    # classify_result only returns these two.
    supported_categories = frozenset({"peer_reviewed", "other"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        page_size: int = 100,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._page_size = page_size

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not can_return(profile):
            return
        cursor = "*"
        while True:
            params = {"query": build_query(profile), "format": "json", "resultType": "core",
                      "pageSize": self._page_size, "cursorMark": cursor}
            response = get_with_retry(
                self._client, BASE_URL, params, "Europe PMC", self._sleep, self._max_attempts,
                headers={"User-Agent": user_agent(self._email)},
            )
            data = response.json()
            results = (data.get("resultList") or {}).get("result") or []
            for record in results:
                yield parse_result(record)
            nxt = data.get("nextCursorMark")
            if not results or not nxt or nxt == cursor:
                return
            cursor = nxt
