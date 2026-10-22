"""Crossref connector (no key). Contact email goes in the documented `mailto` parameter."""

import html
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from difflib import SequenceMatcher
from typing import NamedTuple

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, get_with_retry, user_agent

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


TITLE_SIMILARITY_MIN = 0.95  # normalised title ratio needed to accept a DOI
YEAR_TOLERANCE = 1  # years of difference allowed between the paper and the Crossref record
LOOKUP_ROWS = 5  # candidates read per lookup; the best one that passes every check wins
LOOKUP_ATTEMPTS = 2  # lookups give up quickly (a failure is counted, never retried for long)


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces (for title/name comparison)."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[\W_]+", " ", plain).split())


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    return SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0


def family_matches(first_author: str, item: dict) -> bool:
    """True if the Crossref first author's family name is a whole-word part of `first_author`."""
    authors = item.get("author") or []
    if not authors:
        return False
    family = normalize_text(authors[0].get("family") or authors[0].get("name") or "")
    return bool(family) and f" {family} " in f" {normalize_text(first_author)} "


def accept_item(item: dict, title: str, first_author: str, year: int) -> float | None:
    """Return the title score if the Crossref record is a confident match, else None."""
    cr_title = (item.get("title") or [""])[0] or ""
    score = title_similarity(title, cr_title)
    if score < TITLE_SIMILARITY_MIN:
        return None
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0] or [None]
    cr_year = parts[0]
    if not isinstance(cr_year, int) or abs(cr_year - year) > YEAR_TOLERANCE:
        return None
    if first_author.strip() and not family_matches(first_author, item):
        return None
    return score


class DoiMatch(NamedTuple):
    doi: str
    score: float


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

    def lookup_doi(self, title: str, first_author: str, year: int) -> DoiMatch | None:
        """Find the DOI of a known paper, or None. Raises ConnectorError if Crossref fails.

        Accepts a record only if the title is >= 0.95 similar, the year is within +/-1 and,
        when `first_author` is given, the family name matches. Never guesses.
        """
        params = {
            "query.bibliographic": " ".join(x for x in (title, first_author, str(year)) if x),
            "rows": LOOKUP_ROWS,
            "select": "DOI,title,issued,author",
        }
        if self._email:
            params["mailto"] = self._email
        response = get_with_retry(
            self._client, BASE_URL, params, "Crossref", self._sleep,
            min(self._max_attempts, LOOKUP_ATTEMPTS),
            headers={"User-Agent": user_agent(self._email)},
        )
        try:
            items = (response.json().get("message") or {}).get("items") or []
        except ValueError as exc:
            raise ConnectorError("Crossref request failed: invalid response") from exc
        best: DoiMatch | None = None
        for item in items:
            doi = (item.get("DOI") or "").lower()
            score = accept_item(item, title, first_author, year) if doi else None
            if score is not None and (best is None or score > best.score):
                best = DoiMatch(doi, round(score, 4))
        return best

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
