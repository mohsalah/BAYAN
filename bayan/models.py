"""Core data models shared by connectors, retrieval and the API."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .regions import COUNTRY_NAMES, GEO_BASES, REGIONS, expand

# Search and interface languages. The translator (later milestone) is the only
# feature that accepts other input languages.
LANGUAGES: dict[str, dict[str, object]] = {
    "en": {"name": "English", "rtl": False},
    "ar": {"name": "Arabic", "rtl": True},
    "fr": {"name": "French", "rtl": False},
    "zh": {"name": "Chinese", "rtl": False},
    "de": {"name": "German", "rtl": False},
    "es": {"name": "Spanish", "rtl": False},
}

# Longest research-area text, for the main query and for each per-language query.
QUERY_MAX_LENGTH = 1000
# OpenAlex topic filter: at most this many topic IDs per profile.
MAX_TOPICS = 12
TOPIC_ID = re.compile(r"T[1-9][0-9]{0,8}")
# Names for language codes that libraries report on records. Only the six in LANGUAGES can be
# searched; the extra names exist so a paper is never shown as a bare code.
OTHER_LANGUAGE_NAMES: dict[str, str] = {
    "pt": "Portuguese", "it": "Italian", "ru": "Russian", "ja": "Japanese", "ko": "Korean",
    "nl": "Dutch", "tr": "Turkish", "fa": "Persian", "pl": "Polish", "sv": "Swedish",
    "he": "Hebrew", "hi": "Hindi", "id": "Indonesian", "uk": "Ukrainian", "cs": "Czech",
}
UNKNOWN_LANGUAGE = "unknown"


def language_key(code: str | None) -> str:
    """Grouping key for a paper's language: its code, or 'unknown' when the source gave none."""
    return (code or "").strip().lower() or UNKNOWN_LANGUAGE


def language_name(code: str | None) -> str:
    """Readable name for a language code. Unknown or unnamed codes are labelled, never hidden."""
    key = language_key(code)
    if key == UNKNOWN_LANGUAGE:
        return "Unknown language"
    if key in LANGUAGES:
        return str(LANGUAGES[key]["name"])
    return OTHER_LANGUAGE_NAMES.get(key) or f"Other language ({key})"


# One common set of reference types for every library (docs/BUILD_PLAN.md section 4b).
DocumentType = Literal["peer_reviewed", "proceedings", "thesis", "report", "other"]
DOCUMENT_TYPES: tuple[str, ...] = ("peer_reviewed", "proceedings", "thesis", "report", "other")
CATEGORY_INFO: tuple[dict[str, str], ...] = (
    {"id": "peer_reviewed", "label": "Peer-reviewed papers",
     "description": "The source metadata says journal article or review. This is not verified "
                    "peer review. A preprint later published as a journal article is not "
                    "upgraded automatically."},
    {"id": "proceedings", "label": "Proceedings papers",
     "description": "Conference or proceedings papers."},
    {"id": "thesis", "label": "Theses", "description": "Theses and dissertations."},
    {"id": "report", "label": "Reports",
     "description": "Technical, institutional and government reports."},
    {"id": "other", "label": "Other",
     "description": "Everything that is not one of the four above: preprints, books, book "
                    "chapters, datasets, standards, manuals and records whose type is unknown."},
)
# Library name stored for papers the user added themselves (manuals by URL or upload).
USER_SOURCE = "user"
MANUAL_KIND = "manual"
# Keys written by earlier versions; stored runs may still carry them.
LEGACY_DOCUMENT_TYPES = {"article": "peer_reviewed", "review": "peer_reviewed", "manual": "other"}


def normalize_document_types(values: object) -> object:
    """Map old stored keys to the five current ids (order kept, duplicates removed)."""
    if not isinstance(values, (list, tuple)):
        return values
    return list(dict.fromkeys(LEGACY_DOCUMENT_TYPES.get(v, v) for v in values))


class SearchProfile(BaseModel):
    """What the user asked for. Edited by the user before any search runs."""

    query: str = Field(default="", max_length=QUERY_MAX_LENGTH)
    year_from: int = Field(default=1950, ge=1500, le=2100)
    year_to: int = Field(default_factory=lambda: date.today().year, ge=1500, le=2100)
    document_types: list[DocumentType] = Field(default_factory=lambda: ["peer_reviewed"])
    languages: list[str] = Field(default_factory=lambda: ["en"])
    # Optional keyword text per search language (keys are language codes). A chosen language
    # without an entry uses `query`. Typed by the user, or a planner suggestion the user
    # reviewed and applied (M3); a model never writes it into a profile directly.
    queries_by_language: dict[str, str] = Field(default_factory=dict)
    # OpenAlex topic IDs ("T10001"). A work matches when any of them is among its topics.
    # Only libraries that support the "topics" filter apply it; the others report it unapplied.
    topics: list[str] = Field(default_factory=list, max_length=MAX_TOPICS)
    # ISO 3166-1 alpha-2 codes; matched against author-affiliation countries.
    countries: list[str] = Field(default_factory=list)
    # Region ids from bayan.regions; they expand to country codes together with `countries`.
    regions: list[str] = Field(default_factory=list)
    geo_mode: Literal["include", "exclude"] = "include"
    # v1 supports only the authors' affiliation countries (study location is not inferred).
    geo_basis: str = "author_affiliation"

    @field_validator("document_types", mode="before")
    @classmethod
    def _legacy_document_types(cls, v: object) -> object:
        return normalize_document_types(v)

    @field_validator("languages")
    @classmethod
    def _known_languages(cls, v: list[str]) -> list[str]:
        unknown = [code for code in v if code not in LANGUAGES]
        if unknown:
            raise ValueError(f"unsupported language(s): {', '.join(unknown)}")
        return v

    @field_validator("queries_by_language")
    @classmethod
    def _per_language_queries(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = [code for code in v if code not in LANGUAGES]
        if unknown:
            raise ValueError(
                f"unsupported language(s) in queries_by_language: {', '.join(unknown)}")
        out = {}
        for code, text in v.items():
            if len(text) > QUERY_MAX_LENGTH:
                raise ValueError(
                    f"query for language {code!r} is longer than {QUERY_MAX_LENGTH} characters")
            if text.strip():
                out[code] = text.strip()
        return out

    @field_validator("topics")
    @classmethod
    def _topic_ids(cls, v: list[str]) -> list[str]:
        out = []
        for raw in v:
            topic = raw.strip().rsplit("/", 1)[-1].upper()  # accepts a full OpenAlex URL too
            if not TOPIC_ID.fullmatch(topic):
                raise ValueError(f"topic must be an OpenAlex topic ID such as T10001: {raw!r}")
            out.append(topic)
        return list(dict.fromkeys(out))

    @field_validator("countries")
    @classmethod
    def _country_codes(cls, v: list[str]) -> list[str]:
        out = []
        for code in v:
            code = code.strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"country must be an ISO 3166-1 alpha-2 code: {code!r}")
            if code not in COUNTRY_NAMES:
                raise ValueError(f"unknown country code: {code!r}")
            out.append(code)
        return list(dict.fromkeys(out))

    @field_validator("regions")
    @classmethod
    def _known_regions(cls, v: list[str]) -> list[str]:
        unknown = [r for r in v if r not in REGIONS]
        if unknown:
            raise ValueError(f"unknown region(s): {', '.join(unknown)}")
        return list(dict.fromkeys(v))

    @field_validator("geo_basis")
    @classmethod
    def _supported_basis(cls, v: str) -> str:
        if v not in GEO_BASES:
            raise ValueError(
                f"geography basis {v!r} is not supported; v1 supports only "
                f"{', '.join(GEO_BASES)} (study location is not inferred)")
        return v

    @property
    def geo_countries(self) -> list[str]:
        """Chosen regions and explicit countries as one sorted list of ISO codes."""
        return expand(self.regions, self.countries)

    @model_validator(mode="after")
    def _queries_match_languages(self) -> "SearchProfile":
        extra = [code for code in self.queries_by_language if code not in self.languages]
        if extra:
            raise ValueError("queries_by_language has text for language(s) that are not chosen "
                             f"for search: {', '.join(extra)}")
        return self

    def language_queries(self) -> list[tuple[str, str]]:
        """(language, query text) per chosen language when per-language text was given.

        Empty when there is none, meaning one search with the main query. A chosen language
        without its own text uses the main query.
        """
        if not self.queries_by_language:
            return []
        return [(code, self.queries_by_language.get(code, self.query)) for code in self.languages]

    def for_language(self, code: str, text: str) -> "SearchProfile":
        """A copy that searches one language with that language's text."""
        return self.model_copy(
            update={"languages": [code], "query": text, "queries_by_language": {}})

    def model_post_init(self, __context: object) -> None:
        if self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")


class Paper(BaseModel):
    source: str
    id: str
    doi: str = ""
    title: str = ""
    abstract: str = ""
    year: int | None = None
    # The library's own type label, kept as reported; `category` is the common one of five.
    native_type: str = ""
    category: DocumentType = "other"
    # Why the record got this category (always set when the metadata did not decide it).
    category_basis: str = ""
    # Finer kind inside a category; only "manual" is used (user-supplied manuals, never searched).
    document_kind: str = ""
    language: str = ""
    venue: str = ""
    first_author: str = ""
    countries: list[str] = Field(default_factory=list)
    citations: int = 0
    is_oa: bool = False
    oa_url: str = ""
    landing_url: str = ""
    keywords: list[str] = Field(default_factory=list)
    topic: str = ""
    # Set only when the DOI was found by the automatic Crossref lookup (never for source DOIs).
    doi_source: str = ""
    doi_match_score: float | None = None
