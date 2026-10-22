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

DocumentType = Literal["article", "review", "report", "thesis", "manual"]
DOCUMENT_TYPES: tuple[str, ...] = ("article", "review", "report", "thesis", "manual")


class SearchProfile(BaseModel):
    """What the user asked for. Edited by the user before any search runs."""

    query: str = ""
    year_from: int = Field(default=1950, ge=1500, le=2100)
    year_to: int = Field(default_factory=lambda: date.today().year, ge=1500, le=2100)
    document_types: list[DocumentType] = Field(default_factory=lambda: ["article", "review"])
    languages: list[str] = Field(default_factory=lambda: ["en"])
    # ISO 3166-1 alpha-2 codes; matched against author-affiliation countries.
    countries: list[str] = Field(default_factory=list)

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
    document_type: str = ""
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
