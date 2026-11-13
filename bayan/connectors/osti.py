"""OSTI.gov connector (US DOE, no key). JSON records API; the total is in a response header."""

import re
import time
from collections.abc import Callable, Iterator

import httpx

from ..models import LANGUAGES, OTHER_LANGUAGE_NAMES, Paper, SearchProfile
from .base import get_with_retry, user_agent

BASE_URL = "https://www.osti.gov/api/v1/records"
# OSTI `product_type` -> category. Anything else (books, datasets, patents, ...) is `other`.
OSTI_TYPES = {
    "Journal Article": "peer_reviewed",
    "Conference": "proceedings",
    "Technical Report": "report",
    "Thesis/Dissertation": "thesis",
}
# OSTI gives the language as an English name ("English"); map it back to a code.
_LANGUAGE_CODES = {str(v["name"]).lower(): k for k, v in LANGUAGES.items()} | {
    name.lower(): code for code, name in OTHER_LANGUAGE_NAMES.items()}
# "Last, First [Org] (ORCID:0000...)": the organisation and ORCID parts are not the name.
_AUTHOR_EXTRAS = re.compile(r"\[[^\]]*\]|\(ORCID:[^)]*\)")


def clean_author(raw: str) -> str:
    return " ".join(_AUTHOR_EXTRAS.sub(" ", raw or "").split())


def classify_record(record: dict) -> tuple[str, str]:
    """OSTI record -> (category, basis), from `product_type` only."""
    native = record.get("product_type") or ""
    if native in OSTI_TYPES:
        return OSTI_TYPES[native], f"product type '{native}'"
    if native:
        return "other", f"product type '{native}' is not one of the four categories"
    return "other", "no product type in the record"


def _link(record: dict, rel: str) -> str:
    return next((x.get("href") or "" for x in record.get("links") or []
                 if isinstance(x, dict) and x.get("rel") == rel), "")


def parse_record(record: dict) -> Paper:
    date = record.get("publication_date") or ""
    authors = [a for a in (clean_author(x) for x in record.get("authors") or []
                           if isinstance(x, str)) if a]
    fulltext = _link(record, "fulltext")
    category, basis = classify_record(record)
    return Paper(
        source="osti",
        id=str(record.get("osti_id") or ""),
        doi=(record.get("doi") or "").lower().removeprefix("https://doi.org/"),
        title=(record.get("title") or "").strip(),
        abstract=(record.get("description") or "").strip(),
        year=int(date[:4]) if date[:4].isdigit() else None,
        native_type=record.get("product_type") or "",
        category=category,
        category_basis=basis,
        language=_LANGUAGE_CODES.get((record.get("language") or "").strip().lower(), ""),
        venue=record.get("journal_name") or "",
        first_author=authors[0] if authors else "",
        authors=authors,
        is_oa=bool(fulltext),  # OSTI full text is free to read
        oa_url=fulltext,
        landing_url=_link(record, "citation"),
        keywords=[s for s in record.get("subjects") or [] if isinstance(s, str) and s.strip()],
    )


def build_params(profile: SearchProfile, page: int, rows: int) -> dict:
    # Publication-date bounds use MM/DD/YYYY (confirmed against the live API).
    params: dict = {"rows": rows, "page": page,
                    "publication_date_start": f"01/01/{profile.year_from}",
                    "publication_date_end": f"12/31/{profile.year_to}"}
    if profile.query:
        params["q"] = profile.query
    return params


class OstiConnector:
    name = "osti"
    tier = "open"
    # Years are sent; document types come from classifying every record. No language or
    # affiliation-country filter is used.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset(
        {"peer_reviewed", "proceedings", "thesis", "report", "other"})
    supported_sorts = frozenset({"relevance"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        rows: int = 100,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._rows = rows

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        page = 1
        while True:
            response = get_with_retry(
                self._client, BASE_URL, build_params(profile, page, self._rows), "OSTI",
                self._sleep, self._max_attempts,
                headers={"Accept": "application/json", "User-Agent": user_agent(self._email)},
            )
            records = response.json()
            if not isinstance(records, list):
                return
            for record in records:
                if isinstance(record, dict) and record.get("osti_id"):
                    yield parse_record(record)
            total = response.headers.get("x-total-count", "")
            if len(records) < self._rows or (total.isdigit() and page * self._rows >= int(total)):
                return
            page += 1
