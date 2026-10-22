"""Crossref connector (no key). Contact email goes in the documented `mailto` parameter."""

import html
import re
import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import get_with_retry, user_agent

BASE_URL = "https://api.crossref.org/works"
# App document type -> Crossref `type`. Crossref has no separate "review" type (reviews are
# journal articles) and does not index manuals, so those map to nothing.
CROSSREF_TYPES = {
    "article": ["journal-article", "proceedings-article"],
    "report": ["report"],
    "thesis": ["dissertation"],
}


def clean_abstract(jats: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", jats or "")
    return " ".join(html.unescape(text).split())


def parse_item(item: dict) -> Paper:
    doi = (item.get("DOI") or "").lower()
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0] or [None]
    authors = item.get("author") or []
    first = authors[0] if authors else {}
    first_author = " ".join(x for x in (first.get("given"), first.get("family")) if x) or (
        first.get("name") or "")
    return Paper(
        source="crossref",
        id=doi,
        doi=doi,
        title=((item.get("title") or [""])[0]) or "",
        abstract=clean_abstract(item.get("abstract")),
        year=parts[0] if isinstance(parts[0], int) else None,
        document_type=item.get("type") or "",
        language=item.get("language") or "",
        venue=((item.get("container-title") or [""])[0]) or "",
        first_author=first_author,
        citations=item.get("is-referenced-by-count") or 0,
        landing_url=item.get("URL") or "",
        keywords=list(item.get("subject") or []),
    )


def mapped_types(profile: SearchProfile) -> list[str]:
    return [t for d in profile.document_types for t in CROSSREF_TYPES.get(d, [])]


def build_filter(profile: SearchProfile) -> str:
    parts = [f"from-pub-date:{profile.year_from}", f"until-pub-date:{profile.year_to}"]
    parts += [f"type:{t}" for t in mapped_types(profile)]
    return ",".join(parts)


class CrossrefConnector:
    name = "crossref"
    tier = "open"
    # No language or affiliation-country filter exists in the works API.
    supported_filters = frozenset({"years", "document_types"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        rows: int = 200,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._rows = rows

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if profile.document_types and not mapped_types(profile):
            return  # only types Crossref cannot express were requested: nothing to ask for
        cursor = "*"
        while True:
            params = {"filter": build_filter(profile), "rows": self._rows, "cursor": cursor}
            if profile.query:
                params["query"] = profile.query
            if self._email:
                params["mailto"] = self._email
            response = get_with_retry(
                self._client, BASE_URL, params, "Crossref", self._sleep, self._max_attempts,
                headers={"User-Agent": user_agent(self._email)},
            )
            message = response.json().get("message") or {}
            items = message.get("items") or []
            for item in items:
                if item.get("DOI"):
                    yield parse_item(item)
            cursor = message.get("next-cursor")
            if not items or not cursor:
                return
