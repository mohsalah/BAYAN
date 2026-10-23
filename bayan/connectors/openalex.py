"""OpenAlex connector, ported from the Reactor Physics downloader and generalised."""

import time
from collections.abc import Callable, Iterator

import httpx

from ..models import Paper, SearchProfile
from .base import ConnectorError

BASE_URL = "https://api.openalex.org/works"
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
        keywords=[k["display_name"] for k in (work.get("keywords") or [])],
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
    if profile.countries:
        parts.append("authorships.institutions.country_code:" + "|".join(profile.countries))
    return ",".join(parts)


class OpenAlexConnector:
    name = "openalex"
    tier = "open"
    supported_filters = frozenset({"years", "document_types", "languages", "countries"})

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

    def _get(self, params: dict) -> dict:
        status = 0
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(BASE_URL, params=params)
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
                waits = retry_after is not None and retry_after.isdigit()
                delay = float(retry_after) if waits else 2.0 * attempt
                self._sleep(delay)
        raise ConnectorError(f"OpenAlex request failed: {error}")
