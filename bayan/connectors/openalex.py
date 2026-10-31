"""OpenAlex connector, ported from the Reactor Physics downloader and generalised."""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError, retry_wait

BASE_URL = "https://api.openalex.org/works"
TOPICS_URL = "https://api.openalex.org/topics"
TOPIC_SELECT = "id,display_name,description,subfield,field,domain,works_count"
SELECT = (
    "id,doi,title,publication_year,type,language,primary_location,open_access,"
    "cited_by_count,authorships,abstract_inverted_index,keywords,primary_topic"
)
# Category -> OpenAlex `type` values used as a coarse pre-filter. `other` has none: it is
# "everything else", so asking for it sends no type filter at all.
OPENALEX_TYPES = {
    "peer_reviewed": ["article", "review"],
    "proceedings": ["article", "review"],  # told apart from journals by the source type
    "thesis": ["dissertation"],
    "report": ["report"],
}


def classify_work(work: dict) -> tuple[str, str]:
    """OpenAlex work -> (category, basis). Uses only `type` and `primary_location.source.type`.

    article/review + journal source -> peer_reviewed; + conference source -> proceedings;
    dissertation -> thesis; report -> report; preprint, book, book-chapter, dataset,
    standard and every other or missing type -> other. An article whose source is not a
    journal or conference (repository, book series, none) -> other, with the reason.
    """
    native = work.get("type") or ""
    source_type = (((work.get("primary_location") or {}).get("source")) or {}).get("type") or ""
    if native in ("article", "review"):
        if source_type == "journal":
            return "peer_reviewed", f"type '{native}' in a journal source"
        if source_type == "conference":
            return "proceedings", f"type '{native}' in a conference source"
        shown = f"source type '{source_type}'" if source_type else "no source type"
        return "other", f"type '{native}' but {shown}: not a journal or conference"
    if native == "dissertation":
        return "thesis", "type 'dissertation'"
    if native == "report":
        return "report", "type 'report'"
    if native:
        return "other", f"type '{native}' is not one of the four categories"
    return "other", "no type in the record"


def rebuild_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions = {i: word for word, idxs in inverted.items() for i in idxs}
    return " ".join(positions[i] for i in sorted(positions))


def parse_work(work: dict) -> Paper:
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = work.get("open_access") or {}
    authors = work.get("authorships") or []
    doi = (work.get("doi") or "").removeprefix("https://doi.org/").lower()
    category, basis = classify_work(work)
    return Paper(
        source="openalex",
        id=work["id"].rsplit("/", 1)[-1],
        doi=doi,
        title=work.get("title") or "",
        abstract=rebuild_abstract(work.get("abstract_inverted_index")),
        year=work.get("publication_year"),
        native_type=work.get("type") or "",
        category=category,
        category_basis=basis,
        language=work.get("language") or "",
        venue=src.get("display_name") or "",
        first_author=(authors[0]["author"]["display_name"] if authors else ""),
        countries=sorted({c for a in authors for c in (a.get("countries") or [])}),
        citations=work.get("cited_by_count") or 0,
        is_oa=bool(oa.get("is_oa")),
        oa_url=oa.get("oa_url") or "",
        landing_url=loc.get("landing_page_url") or "",
        keywords=[k["display_name"] for k in work.get("keywords") or []
                  if isinstance(k, dict) and isinstance(k.get("display_name"), str)],
        topic=(work.get("primary_topic") or {}).get("display_name") or "",
    )


def build_filter(profile: SearchProfile) -> str:
    parts = [f"publication_year:{profile.year_from}-{profile.year_to}", "is_retracted:false"]
    types: list[str] = []
    if "other" not in profile.document_types:
        types = list(dict.fromkeys(
            t for c in profile.document_types for t in OPENALEX_TYPES.get(c, [])))
    if types:
        parts.append("type:" + "|".join(types))
    if profile.languages:
        parts.append("language:" + "|".join(profile.languages))
    codes = profile.geo_countries
    if codes:
        # Exclude mode negates the whole OR list. The run also re-checks the countries each
        # record reports, so a different reading of "!" cannot let excluded records in.
        negate = "!" if profile.geo_mode == "exclude" else ""
        parts.append("authorships.institutions.country_code:" + negate + "|".join(codes))
    if profile.topics:
        # Any of a work's (up to three) topics may match, not only its primary topic.
        parts.append("topics.id:" + "|".join(profile.topics))
    return ",".join(parts)


def parse_topic(item: dict) -> dict:
    """An OpenAlex topic record -> the fields shown when a topic is suggested."""
    return {
        "id": (item.get("id") or "").rsplit("/", 1)[-1],
        "name": item.get("display_name") or "",
        "description": item.get("description") or "",
        "field": ((item.get("field") or {}).get("display_name")) or "",
        "subfield": ((item.get("subfield") or {}).get("display_name")) or "",
        "works_count": item.get("works_count") or 0,
    }


class OpenAlexConnector:
    name = "openalex"
    tier = "open"
    supported_filters = frozenset({"years", "document_types", "languages", "countries", "topics"})
    # Reference types classify_work can return (see OPENALEX_TYPES and the `other` fallback).
    supported_categories = frozenset(
        {"peer_reviewed", "proceedings", "thesis", "report", "other"})

    def __init__(
        self,
        api_key: str | None = None,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        per_page: int = 200,
    ) -> None:
        self._api_key = api_key
        self._email = contact_email
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._per_page = per_page
        # Plain-language notes for the run summary (for example a paging limit that was hit).
        self.warnings: list[str] = []

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        cursor: str | None = "*"
        first_page = True
        while cursor:
            params = {
                "filter": build_filter(profile),
                "per-page": self._per_page,
                "cursor": cursor,
                "select": SELECT,
            }
            if profile.query:
                params["search"] = profile.query
            if self._email:
                params["mailto"] = self._email
            if self._api_key:
                params["api_key"] = self._api_key
            try:
                data = self._get(params)
            except ConnectorError as exc:
                # OpenAlex needs a free API key to page past the first page of results.
                if not first_page and not self._api_key and "HTTP 401" in str(exc):
                    self.warnings.append(
                        f"OpenAlex returned only its first {self._per_page} results. Paging "
                        "further needs a free OpenAlex API key (set OPENALEX_API_KEY).")
                    return
                raise
            first_page = False
            for work in data.get("results", []):
                yield parse_work(work)
            cursor = (data.get("meta") or {}).get("next_cursor")

    def search_topics(self, text: str, limit: int = 5) -> list[dict]:
        """Topics whose name or description matches `text`, as OpenAlex ranks them."""
        params: dict = {"search": text, "per-page": limit, "select": TOPIC_SELECT}
        if self._email:
            params["mailto"] = self._email
        if self._api_key:
            params["api_key"] = self._api_key
        data = self._get(params, TOPICS_URL)
        return [parse_topic(item) for item in data.get("results", []) if item.get("id")]

    def _get(self, params: dict, url: str = BASE_URL) -> dict:
        status = 0
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                status = 0
                error = type(exc).__name__
            else:
                status = response.status_code
                if status == 200:
                    return response.json()
                error = f"HTTP {status}"
                if status not in (429, 500, 502, 503, 504):
                    break
            if attempt < self._max_attempts:
                retry_after = response.headers.get("Retry-After") if status else None
                try:
                    delay = retry_wait(retry_after, attempt, "OpenAlex", error)
                except ConnectorError as exc:
                    if self._api_key:
                        raise
                    raise ConnectorError(
                        f"{exc} Without an API key OpenAlex shares one free daily budget "
                        "between everyone on this network; a free key (OPENALEX_API_KEY) "
                        "gives its own.") from None
                self._sleep(delay)
        raise ConnectorError(f"OpenAlex request failed: {error}")
