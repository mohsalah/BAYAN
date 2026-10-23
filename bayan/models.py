"""Core data models shared by connectors, retrieval and the API."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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

    query: str = ""
    year_from: int = Field(default=1950, ge=1500, le=2100)
    year_to: int = Field(default_factory=lambda: date.today().year, ge=1500, le=2100)
    document_types: list[DocumentType] = Field(default_factory=lambda: ["peer_reviewed"])
    languages: list[str] = Field(default_factory=lambda: ["en"])
    # ISO 3166-1 alpha-2 codes; matched against author-affiliation countries.
    countries: list[str] = Field(default_factory=list)

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

    @field_validator("countries")
    @classmethod
    def _country_codes(cls, v: list[str]) -> list[str]:
        out = []
        for code in v:
            code = code.strip().upper()
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"country must be an ISO 3166-1 alpha-2 code: {code!r}")
            out.append(code)
        return out

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
