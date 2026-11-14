"""IAEA INIS connector (InvenioRDM records API, no key)."""

import html
import re
import time
from collections.abc import Callable, Iterator
from urllib.parse import quote

import httpx

from ..models import Paper, SearchProfile
from .base import get_with_retry, user_agent

BASE_URL = "https://inis.iaea.org/api/records"
RECORD_URL = "https://inis.iaea.org/records/"
# The API refuses pages past the first 10,000 records (HTTP 400, checked live).
MAX_RECORDS = 10000
# `metadata.resource_type.id` -> category. INIS has no thesis resource type (theses are a
# literary type that the record does not expose), so theses fall under `other`.
INIS_TYPES = {
    "publication-article": "peer_reviewed",
    "publication-conferencepaper": "proceedings",
    "publication-report": "report",
}


def classify_record(record: dict) -> tuple[str, str]:
    """INIS record -> (category, basis), from `metadata.resource_type.id` only."""
    native = ((record.get("metadata") or {}).get("resource_type") or {}).get("id") or ""
    if native in INIS_TYPES:
        return INIS_TYPES[native], f"resource type '{native}'"
    if native:
        return "other", f"resource type '{native}' is not one of the four categories"
    return "other", "no resource type in the record"


def can_return(profile: SearchProfile) -> bool:
    """INIS yields no theses; skip it when theses are the only category asked for."""
    return set(profile.document_types) != {"thesis"}


def clean_text(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(text).split())


def _open_file(record: dict) -> str:
    """Download link of the record's first PDF when its files are public, else ''."""
    if (record.get("access") or {}).get("files") != "public":
        return ""
    entries = (record.get("files") or {}).get("entries") or {}
    for key, entry in entries.items():
        if isinstance(entry, dict) and (entry.get("ext") == "pdf"
                                        or entry.get("mimetype") == "application/pdf"):
            return f"{RECORD_URL}{quote(str(record.get('id')))}/files/{quote(key)}?download=1"
    return ""


def parse_record(record: dict) -> Paper:
    meta = record.get("metadata") or {}
    custom = record.get("custom_fields") or {}
    date = meta.get("publication_date") or ""
    authors = [(c.get("person_or_org") or {}).get("name") or "" for c in meta.get("creators") or []
               if (c.get("person_or_org") or {}).get("type") == "personal"]
    authors = [a.strip() for a in authors if a.strip()]
    doi = next((i.get("identifier") or "" for i in meta.get("identifiers") or []
                if isinstance(i, dict) and i.get("scheme") == "doi"), "")
    languages = [x.get("id") or "" for x in meta.get("languages") or [] if isinstance(x, dict)]
    oa_url = _open_file(record)
    category, basis = classify_record(record)
    return Paper(
        source="inis",
        id=str(record.get("id") or ""),
        doi=doi.lower().removeprefix("https://doi.org/"),
        title=(meta.get("title") or "").strip(),
        abstract=clean_text(meta.get("description")),
        year=int(date[:4]) if date[:4].isdigit() else None,
        native_type=(meta.get("resource_type") or {}).get("id") or "",
        category=category,
        category_basis=basis,
        language=languages[0] if languages and len(languages[0]) == 2 else "",
        venue=((custom.get("journal:journal") or {}).get("title")) or "",
        first_author=authors[0] if authors else "",
        authors=authors,
        is_oa=bool(oa_url),
        oa_url=oa_url,
        landing_url=(record.get("links") or {}).get("self_html") or "",
    )


def build_query(profile: SearchProfile) -> str:
    # Full dates: a bare year as the upper bound would stop at 1 January of that year.
    parts = [f"metadata.publication_date:[{profile.year_from}-01-01 TO {profile.year_to}-12-31]"]
    if profile.query:
        parts.insert(0, f"({profile.query})")
    if profile.languages:
        parts.append("metadata.languages.id:(" + " OR ".join(profile.languages) + ")")
    return " AND ".join(parts)


class InisConnector:
    name = "inis"
    tier = "open"
    # Years and languages are sent in the query (both checked live); document types come from
    # classifying every record. INIS's country field is the country of publication, not the
    # authors' affiliation, so the country filter is not applied.
    supported_filters = frozenset({"years", "languages", "document_types"})
    supported_categories = frozenset({"peer_reviewed", "proceedings", "report", "other"})
    supported_sorts = frozenset({"relevance"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        size: int = 100,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._size = size
        self.warnings: list[str] = []
        self.truncated = False

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not can_return(profile):
            return
        page = 1
        while True:
            params = {"q": build_query(profile), "size": self._size, "page": page}
            if profile.query:
                params["sort"] = "bestmatch"
            response = get_with_retry(
                self._client, BASE_URL, params, "INIS", self._sleep, self._max_attempts,
                headers={"Accept": "application/json", "User-Agent": user_agent(self._email)},
            )
            hits = response.json().get("hits") or {}
            records = hits.get("hits") or []
            for record in records:
                if record.get("id"):
                    yield parse_record(record)
            total = hits.get("total")
            seen = page * self._size
            if len(records) < self._size or (isinstance(total, int) and seen >= total):
                return
            if seen + self._size > MAX_RECORDS:
                self.truncated = True
                self.warnings.append(f"INIS returns at most its first {MAX_RECORDS} records "
                                     "per search; narrow the search to see the rest.")
                return
            page += 1
