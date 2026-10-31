"""arXiv connector (no key). Atom API; arXiv asks for ~3 s between calls."""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, get_with_retry, user_agent

BASE_URL = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom",
      "o": "http://a9.com/-/spec/opensearch/1.1/"}


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path, NS)
    return " ".join((found.text or "").split()) if found is not None else ""


def classify_entry(entry: ET.Element) -> tuple[str, str]:
    """Every arXiv record is a preprint -> other. A journal_ref does not upgrade it."""
    return "other", "arXiv preprint"


def wants_preprints(profile: SearchProfile) -> bool:
    """arXiv can only return `other`; with other categories selected there is nothing to ask for."""
    return not profile.document_types or "other" in profile.document_types


def parse_entry(entry: ET.Element) -> Paper:
    url = _text(entry, "a:id")
    if "/api/errors" in url:
        raise ConnectorError("arXiv rejected the query")
    arxiv_id = re.sub(r"v\d+$", "", url.rsplit("/abs/", 1)[-1])
    published = _text(entry, "a:published")
    pdf = next((link.get("href", "") for link in entry.findall("a:link", NS)
                if link.get("title") == "pdf"), "")
    primary = entry.find("x:primary_category", NS)
    category, basis = classify_entry(entry)
    return Paper(
        source="arxiv",
        id=arxiv_id,
        doi=_text(entry, "x:doi").lower().removeprefix("https://doi.org/"),
        title=_text(entry, "a:title"),
        abstract=_text(entry, "a:summary"),
        year=int(published[:4]) if published[:4].isdigit() else None,
        native_type="preprint",
        category=category,
        category_basis=basis,
        venue=_text(entry, "x:journal_ref"),
        first_author=_text(entry, "a:author/a:name"),
        authors=[" ".join((n.text or "").split()) for n in entry.findall("a:author/a:name", NS)
                 if (n.text or "").strip()],
        is_oa=True,
        oa_url=pdf,
        landing_url=f"https://arxiv.org/abs/{arxiv_id}",
        keywords=[c.get("term", "") for c in entry.findall("a:category", NS) if c.get("term")],
        topic=primary.get("term", "") if primary is not None else "",
    )


_TOKEN = re.compile(r'"([^"]*)"|(\()|(\))|(\w+)')
_OPERATORS = {"AND": "AND", "OR": "OR", "NOT": "ANDNOT"}


def boolean_query(text: str) -> str | None:
    """A keyword query with AND/OR/NOT, parentheses and "phrases" in arXiv syntax.

    Adjacent terms are joined with AND, as before. Returns None when the text is not a
    well-formed query of that kind (unbalanced parentheses, an operator in the wrong place),
    so the caller can fall back to plain words.
    """
    out: list[str] = []
    depth, expect_term = 0, True
    for phrase, opening, closing, word in _TOKEN.findall(text):
        if word.upper() in _OPERATORS and word.isupper():
            if expect_term:
                return None
            out.append(_OPERATORS[word])
            expect_term = True
            continue
        if closing:
            depth -= 1
            if depth < 0 or expect_term:
                return None
            out.append(")")
            continue
        if not expect_term:  # two terms side by side
            out.append("AND")
        if opening:
            depth += 1
            out.append("(")
            continue
        words = re.findall(r"\w+", phrase) if phrase or not word else [word]
        if not words:
            return None
        out.append(f'all:"{" ".join(words)}"' if len(words) > 1 else f"all:{words[0]}")
        expect_term = False
    if depth or expect_term or not out:
        return None
    return " ".join(out).replace("( ", "(").replace(" )", ")")


def build_query(profile: SearchProfile) -> str:
    # submittedDate is the date of the first version.
    parts = [f"submittedDate:[{profile.year_from}01010000 TO {profile.year_to}12312359]"]
    query = boolean_query(profile.query)
    if query is None:
        words = re.findall(r"\w+", profile.query)
        query = " AND ".join(f"all:{w}" for w in words) if words else None
    if query:
        parts.insert(0, f"({query})")
    return " AND ".join(parts)


class ArxivConnector:
    name = "arxiv"
    tier = "open"
    # Everything on arXiv is a preprint (category `other`); there is no language or
    # affiliation-country field.
    supported_filters = frozenset({"years", "document_types"})
    supported_categories = frozenset({"other"})

    def __init__(
        self,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        page_size: int = 100,
        page_delay: float = 3.0,
    ) -> None:
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._page_size = page_size
        self._page_delay = page_delay

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        if not wants_preprints(profile):
            return
        start = 0
        while True:
            params = {"search_query": build_query(profile), "start": start,
                      "max_results": self._page_size, "sortBy": "relevance"}
            if start:
                self._sleep(self._page_delay)
            response = get_with_retry(
                self._client, BASE_URL, params, "arXiv", self._sleep, self._max_attempts,
                headers={"User-Agent": user_agent(self._email)},
            )
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as exc:
                raise ConnectorError("arXiv returned an unreadable response") from exc
            entries = root.findall("a:entry", NS)
            for entry in entries:
                yield parse_entry(entry)
            total = _text(root, "o:totalResults")
            start += len(entries)
            if len(entries) < self._page_size or (total.isdigit() and start >= int(total)):
                return
