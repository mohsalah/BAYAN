"""CORE connector (API v3 works search). A free server key (CORE_API_KEY) is sent when set;
without one the API still answered (checked live), under CORE's lower anonymous rate limit."""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, get_with_retry, user_agent

# The trailing slash matters: without it the API answers with a redirect.
BASE_URL = "https://api.core.ac.uk/v3/search/works/"
PAGE_SIZE = 100
# CORE `documentType` -> category (all four values seen in live answers). 'research article',
# 'review' and 'text' stay `other`: CORE harvests repositories, where they may be preprints.
CORE_TYPES = {
    "journal article": "peer_reviewed",
    "conference paper": "proceedings",
    "thesis": "thesis",
    "report": "report",
}


def classify_record(record: dict) -> tuple[str, str]:
    """CORE work -> (category, basis), from `documentType` only."""
    native = (record.get("documentType") or "").strip()
    if native.lower() in CORE_TYPES:
        return CORE_TYPES[native.lower()], f"document type '{native}'"
    if native:
        return "other", f"document type '{native}' is not one of the four categories"
    return "other", "no document type in the record"


def build_query(profile: SearchProfile) -> str:
    years = f"yearPublished>={profile.year_from} AND yearPublished<={profile.year_to}"
    return f"({profile.query}) AND {years}" if profile.query else years


def parse_record(record: dict) -> Paper:
    authors = [a.get("name", "").strip() for a in record.get("authors") or []
               if isinstance(a, dict) and (a.get("name") or "").strip()]
    links = {x.get("type"): x.get("url") for x in record.get("links") or []
             if isinstance(x, dict) and x.get("url")}
    full = [u for u in record.get("sourceFulltextUrls") or [] if isinstance(u, str) and u]
    oa_url = record.get("downloadUrl") or (full[0] if full else "")
    year = record.get("yearPublished")
    category, basis = classify_record(record)
    return Paper(
        source="core",
        id=str(record.get("id") or ""),
        doi=(record.get("doi") or "").lower().removeprefix("https://doi.org/"),
        title=(record.get("title") or "").strip(),
        abstract=(record.get("abstract") or "").strip(),
        year=year if isinstance(year, int) else None,
        native_type=record.get("documentType") or "",
        category=category,
        category_basis=basis,
        language=((record.get("language") or {}).get("code") or ""),
        first_author=authors[0] if authors else "",
        authors=authors,
        is_oa=bool(oa_url),
        oa_url=oa_url,
        landing_url=links.get("display") or f"https://core.ac.uk/works/{record.get('id')}",
    )


class CoreConnector:
    name = "core"
    tier = "open"
    # Years go into the query (checked live); document types come from classifying records.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset(
        {"peer_reviewed", "proceedings", "thesis", "report", "other"})
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
        self._page_size = page_size

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        headers = {"User-Agent": user_agent(self._email)}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        offset = 0
        while True:
            params = {"q": build_query(profile), "limit": self._page_size, "offset": offset}
            try:
                response = get_with_retry(self._client, BASE_URL, params, "CORE", self._sleep,
                                          self._max_attempts, headers=headers)
            except ConnectorError as exc:
                if not self._api_key and any(f"HTTP {c}" in str(exc) for c in (401, 429)):
                    raise ConnectorError(
                        f"{exc}. CORE limits requests without a key; a free server key "
                        "(CORE_API_KEY) raises the limit.") from None
                raise
            data = response.json()
            records = data.get("results") or []
            for record in records:
                if record.get("id") is not None:
                    yield parse_record(record)
            offset += len(records)
            total = data.get("totalHits")
            if not records or (isinstance(total, int) and offset >= total):
                return
