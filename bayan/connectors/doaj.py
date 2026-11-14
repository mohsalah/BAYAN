"""DOAJ connector (Directory of Open Access Journals, articles API, no key)."""

import time
from collections.abc import Callable, Iterator
from urllib.parse import quote

import httpx

from ..models import Paper, SearchProfile
from .base import get_with_retry, user_agent

BASE_URL = "https://doaj.org/api/search/articles/"
# Checked live: the API answers at most 50 records per page and refuses (HTTP 400) any page
# past the first 1,000 records.
PAGE_SIZE = 50
MAX_RECORDS = 1000


def classify_record(record: dict) -> tuple[str, str]:
    """Every DOAJ article is in a journal DOAJ lists as peer reviewed and open access."""
    return "peer_reviewed", "article in a DOAJ-listed journal (DOAJ lists peer-reviewed " \
                            "open-access journals)"


def can_return(profile: SearchProfile) -> bool:
    types = profile.document_types
    return not types or "peer_reviewed" in types


def build_query(profile: SearchProfile) -> str:
    year = f"bibjson.year:[{profile.year_from} TO {profile.year_to}]"
    return f"({profile.query}) AND {year}" if profile.query else year


def parse_record(record: dict) -> Paper:
    bib = record.get("bibjson") or {}
    year = str(bib.get("year") or "")
    authors = [a.get("name", "").strip() for a in bib.get("author") or []
               if isinstance(a, dict) and (a.get("name") or "").strip()]
    doi = next((i.get("id") or "" for i in bib.get("identifier") or []
                if isinstance(i, dict) and (i.get("type") or "").lower() == "doi"), "")
    doi = doi.strip().lower().removeprefix("https://doi.org/")
    fulltext = next((x.get("url") or "" for x in bib.get("link") or []
                     if isinstance(x, dict) and x.get("type") == "fulltext"), "")
    category, basis = classify_record(record)
    return Paper(
        source="doaj",
        id=str(record.get("id") or ""),
        doi=doi,
        title=(bib.get("title") or "").strip(),
        abstract=(bib.get("abstract") or "").strip(),
        year=int(year) if year.isdigit() else None,
        native_type="article",
        category=category,
        category_basis=basis,
        # The journal's languages are not the article's language, so none is set.
        venue=(bib.get("journal") or {}).get("title") or "",
        first_author=authors[0] if authors else "",
        authors=authors,
        is_oa=True,
        oa_url=fulltext,
        # The publisher's page (DOI link, else the full-text link); DOAJ's own record page
        # could not be checked from here.
        landing_url=f"https://doi.org/{doi}" if doi else fulltext,
        keywords=[k for k in bib.get("keywords") or [] if isinstance(k, str) and k.strip()],
    )


class DoajConnector:
    name = "doaj"
    tier = "open"
    # Years go into the query (checked live). Languages are not filtered: DOAJ records the
    # journal's languages, not the article's.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset({"peer_reviewed"})
    supported_sorts = frozenset({"relevance"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self.warnings: list[str] = []
        self.truncated = False

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not can_return(profile):
            return
        url = BASE_URL + quote(build_query(profile), safe="")
        page = 1
        while True:
            response = get_with_retry(
                self._client, url, {"page": page, "pageSize": PAGE_SIZE}, "DOAJ",
                self._sleep, self._max_attempts, headers={"User-Agent": user_agent(self._email)},
            )
            data = response.json()
            records = data.get("results") or []
            for record in records:
                if record.get("id"):
                    yield parse_record(record)
            total = data.get("total")
            seen = page * PAGE_SIZE
            if len(records) < PAGE_SIZE or (isinstance(total, int) and seen >= total):
                return
            if seen >= MAX_RECORDS:
                self.truncated = True
                self.warnings.append(f"DOAJ returns at most its first {MAX_RECORDS} records per "
                                     "search; narrow the search to see the rest.")
                return
            page += 1
