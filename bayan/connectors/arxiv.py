"""arXiv connector (no key). Atom API; arXiv asks for ~3 s between calls."""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, RequestBudget, get_with_retry, user_agent

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


_TOKEN = re.compile(r'"([^"]*)"|(\()|(\))|(\w+(?:[-\u2010\u2011]\w+)*)')  # MERRA-2: one term
_OPERATORS = {"AND": "AND", "OR": "OR", "NOT": "ANDNOT"}


def boolean_query(text: str) -> str | None:
    """A keyword query with AND/OR/NOT, parentheses and "phrases" in arXiv syntax.

    Adjacent terms are joined with AND, as before. Returns None when the text is not a
    well-formed query of that kind (unbalanced parentheses, an empty group, an operator
    with nothing after it), so the caller can fall back to plain words. Where a term is
    due, OR/AND/NOT is read as a word; empty "" phrases are skipped.
    """
    out: list[str] = []
    depth, expect_term = 0, True
    for phrase, opening, closing, word in _TOKEN.findall(text):
        # where a term is due, OR/AND/NOT is a word, as in "Operations Research (OR)"
        if word in _OPERATORS and not expect_term:
            out.append(_OPERATORS[word])
            expect_term = True
            continue
        if closing:
            depth -= 1
            if depth < 0 or expect_term:
                return None
            out.append(")")
            continue
        words = re.findall(r"\w+", phrase or word)
        if not opening and not words:  # "" or a phrase of punctuation: skip it
            continue
        if not expect_term:  # two terms side by side
            out.append("AND")
        if opening:
            depth += 1
            out.append("(")
            expect_term = True  # a group starts with a term, never with AND
            continue
        out.append(f'all:"{" ".join(words)}"' if len(words) > 1 else f"all:{words[0]}")
        expect_term = False
    if depth or expect_term or not out:
        return None
    return " ".join(out).replace("( ", "(").replace(" )", ")")


def build_query(profile: SearchProfile, since: str | None = None) -> str:
    # submittedDate is the date of the first version. A refresh (M32) starts at `since`
    # (YYYY-MM-DD) when that is later than the profile's first year.
    start = f"{profile.year_from}0101"
    if since and since.replace("-", "") > start:
        start = since.replace("-", "")
    parts = [f"submittedDate:[{start}0000 TO {profile.year_to}12312359]"]
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
        yield from self._pages(profile)

    def search_since(self, profile: SearchProfile, since: str,
                     budget: RequestBudget) -> Iterator[Paper]:
        """Preprints first submitted on or after `since` (YYYY-MM-DD); each page takes one
        request from `budget`."""
        yield from self._pages(profile, since, budget)

    def _pages(self, profile: SearchProfile, since: str | None = None,
               budget: RequestBudget | None = None) -> Iterator[Paper]:
        if not wants_preprints(profile):
            return
        start = 0
        while True:
            if budget is not None and not budget.take():
                return
            params = {"search_query": build_query(profile, since), "start": start,
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
