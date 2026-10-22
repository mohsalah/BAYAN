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
# App document type -> OpenAlex `type`. Manuals are not indexed by OpenAlex
# (user uploads and URLs only), so they map to nothing here.
OPENALEX_TYPES = {
    "article": "article",
    "review": "review",
    "report": "report",
    "thesis": "dissertation",
}


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
    return Paper(
        source="openalex",
        id=work["id"].rsplit("/", 1)[-1],
        doi=doi,
        title=work.get("title") or "",
        abstract=rebuild_abstract(work.get("abstract_inverted_index")),
        year=work.get("publication_year"),
        document_type=work.get("type") or "",
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
    types = [OPENALEX_TYPES[t] for t in profile.document_types if t in OPENALEX_TYPES]
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

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        cursor: str | None = "*"
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
            data = self._get(params)
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
