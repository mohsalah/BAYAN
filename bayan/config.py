"""Settings read from the environment. Secrets never appear in repr or logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_path: Path
    openalex_api_key: str | None = field(default=None, repr=False)
    contact_email: str | None = None
    app_secret_key: str | None = field(default=None, repr=False)
    model_reader: str = "claude-sonnet-5-5"
    model_screener: str = "claude-haiku-5-5"
    cookie_secure: bool = False
    session_days: int = 14
    searches_per_hour: int = 20
    search_max_results: int = 200
    doi_lookup_budget: int = 50  # Crossref DOI lookups per run (cache hits are free)


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_path=Path(os.environ.get("BAYAN_DATABASE_PATH", "data/app.sqlite")),
        openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
        contact_email=os.environ.get("CONTACT_EMAIL") or None,
        app_secret_key=os.environ.get("APP_SECRET_KEY") or None,
        model_reader=os.environ.get("BAYAN_MODEL_READER", "claude-sonnet-5-5"),
        model_screener=os.environ.get("BAYAN_MODEL_SCREENER", "claude-haiku-5-5"),
        doi_lookup_budget=int(os.environ.get("BAYAN_DOI_LOOKUP_BUDGET", "50")),
        cookie_secure=os.environ.get("BAYAN_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    )
