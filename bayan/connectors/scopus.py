"""Scopus connector (Elsevier Scopus Search API) with the user's own key.

The key (and an optional institutional token) are the user's, saved encrypted under
Membership access. They go only into request headers, never into errors, logs or results.
Field names follow Elsevier's documented STANDARD view; no live call could be made without
an entitled key, so the parsing is checked against fixtures only.
"""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, get_with_retry

BASE_URL = "https://api.elsevier.com/content/search/scopus"
PAGE_SIZE = 25  # the STANDARD view's largest `count`
MAX_RECORDS = 5000  # start + count limit without a campus entitlement
REFUSED = ("Scopus refused the key: it needs an Elsevier API key from dev.elsevier.com and "
           "institutional access (campus network/VPN or an institutional token)")
SCOPUS_TYPES = {"ar": "peer_reviewed", "re": "peer_reviewed", "cp": "proceedings"}


def classify_entry(entry: dict) -> tuple[str, str]:
    """Scopus entry -> (category, basis), from `subtype` only: ar (article) and re (review)
    -> peer_reviewed, cp (conference paper) -> proceedings, anything else -> other."""
    code = entry.get("subtype") or ""
    label = entry.get("subtypeDescription") or code
    if code in SCOPUS_TYPES:
        return SCOPUS_TYPES[code], f"subtype '{label}'"
    if code:
        return "other", f"subtype '{label}' is not one of the four categories"
    return "other", "no subtype in the record"


def can_return(profile: SearchProfile) -> bool:
    types = profile.document_types
    return not types or bool({"peer_reviewed", "proceedings", "other"} & set(types))


def build_query(profile: SearchProfile) -> str:
    years = f"PUBYEAR > {profile.year_from - 1} AND PUBYEAR < {profile.year_to + 1}"
    return f"TITLE-ABS-KEY({profile.query}) AND {years}" if profile.query.strip() else years


def _flag(value: object) -> bool:
    return value is True or str(value).lower() == "true"


def parse_entry(entry: dict) -> Paper:
    scopus_id = (entry.get("dc:identifier") or "").removeprefix("SCOPUS_ID:")
    date = entry.get("prism:coverDate") or ""
    creator = (entry.get("dc:creator") or "").strip()
    cited = str(entry.get("citedby-count") or "")
    landing = next((x.get("@href") or "" for x in entry.get("link") or []
                    if isinstance(x, dict) and x.get("@ref") == "scopus"), "")
    category, basis = classify_entry(entry)
    return Paper(
        source="scopus",
        id=scopus_id or entry.get("eid") or "",
        doi=(entry.get("prism:doi") or "").lower(),
        title=(entry.get("dc:title") or "").strip(),
        year=int(date[:4]) if date[:4].isdigit() else None,
        native_type=entry.get("subtypeDescription") or entry.get("subtype") or "",
        category=category,
        category_basis=basis,
        venue=entry.get("prism:publicationName") or "",
        first_author=creator,
        authors=[creator] if creator else [],  # the STANDARD view lists the first author only
        citations=int(cited) if cited.isdigit() else 0,
        is_oa=_flag(entry.get("openaccessFlag")),
        landing_url=landing,
    )


class ScopusConnector:
    name = "scopus"
    tier = "membership"
    # Years go into the query; document types come from classifying every entry.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset({"peer_reviewed", "proceedings", "other"})
    supported_sorts = frozenset({"relevance"})

    def __init__(
        self,
        api_key: str,
        insttoken: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
    ) -> None:
        self._api_key = api_key
        self._insttoken = insttoken
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self.warnings: list[str] = []
        self.truncated = False

    def __repr__(self) -> str:  # never show the key
        return "ScopusConnector()"

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not can_return(profile):
            return
        headers = {"X-ELS-APIKey": self._api_key, "Accept": "application/json"}
        if self._insttoken:
            headers["X-ELS-Insttoken"] = self._insttoken
        start = 0
        while True:
            count = min(PAGE_SIZE, MAX_RECORDS - start)
            params = {"query": build_query(profile), "start": start, "count": count,
                      "view": "STANDARD"}
            try:
                response = get_with_retry(self._client, BASE_URL, params, "Scopus",
                                          self._sleep, self._max_attempts, headers=headers)
            except ConnectorError as exc:
                if any(f"HTTP {c}" in str(exc) for c in (401, 403)):
                    raise ConnectorError(REFUSED) from None
                raise
            results = response.json().get("search-results") or {}
            # An empty result set comes back as one entry carrying an "error" field.
            entries = [e for e in results.get("entry") or []
                       if isinstance(e, dict) and "error" not in e]
            for entry in entries:
                if entry.get("dc:identifier") or entry.get("eid"):
                    yield parse_entry(entry)
            start += len(entries)
            total = str(results.get("opensearch:totalResults") or "")
            if not entries or (total.isdigit() and start >= int(total)):
                return
            if start >= MAX_RECORDS:
                self.truncated = True
                self.warnings.append(f"Scopus returns at most {MAX_RECORDS} results per search "
                                     "without a campus entitlement; narrow the search.")
                return
