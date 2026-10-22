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


def parse_entry(entry: ET.Element) -> Paper:
    url = _text(entry, "a:id")
    if "/api/errors" in url:
        raise ConnectorError("arXiv rejected the query")
    arxiv_id = re.sub(r"v\d+$", "", url.rsplit("/abs/", 1)[-1])
    published = _text(entry, "a:published")
    pdf = next((link.get("href", "") for link in entry.findall("a:link", NS)
                if link.get("title") == "pdf"), "")
    primary = entry.find("x:primary_category", NS)
    return Paper(
        source="arxiv",
        id=arxiv_id,
        doi=_text(entry, "x:doi").lower().removeprefix("https://doi.org/"),
        title=_text(entry, "a:title"),
        abstract=_text(entry, "a:summary"),
        year=int(published[:4]) if published[:4].isdigit() else None,
        document_type="preprint",
        venue=_text(entry, "x:journal_ref"),
        first_author=_text(entry, "a:author/a:name"),
        is_oa=True,
        oa_url=pdf,
        landing_url=f"https://arxiv.org/abs/{arxiv_id}",
        keywords=[c.get("term", "") for c in entry.findall("a:category", NS) if c.get("term")],
        topic=primary.get("term", "") if primary is not None else "",
    )


def build_query(profile: SearchProfile) -> str:
    # submittedDate is the date of the first version.
    parts = [f"submittedDate:[{profile.year_from}01010000 TO {profile.year_to}12312359]"]
    words = re.findall(r"\w+", profile.query)
    if words:
        parts.insert(0, "(" + " AND ".join(f"all:{w}" for w in words) + ")")
    return " AND ".join(parts)


class ArxivConnector:
    name = "arxiv"
    tier = "open"
    # Everything on arXiv is a preprint; there is no language or affiliation-country field.
    supported_filters = frozenset({"years"})

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
