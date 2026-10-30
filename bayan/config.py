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
    anthropic_api_key: str | None = field(default=None, repr=False)  # reader agent (M8)
    model_reader: str = "claude-sonnet-5-5"
    model_screener: str = "claude-haiku-5-5"
    cookie_secure: bool = False
    session_days: int = 14
    searches_per_hour: int = 20
    search_max_results: int = 200
    doi_lookup_budget: int = 50  # Crossref DOI lookups per run (cache hits are free)
    pdf_root: Path = Path("pdfs")  # pdfs/<user_id>/<source>/<paper_id>.pdf
    downloads_per_hour: int = 100  # papers a user may queue for download per hour
    download_host_delay: float = 1.0  # minimum seconds between requests to the same host
    download_retries: int = 2
    pdf_max_bytes: int = 100 * 1024 * 1024
    job_stale_seconds: int = 900  # a job with no heartbeat for this long was interrupted
    job_resume_interval: float = 60.0  # seconds between checks for interrupted jobs; 0 = off
    translate_url: str | None = None  # LibreTranslate-compatible server; None = not configured
    translate_api_key: str | None = field(default=None, repr=False)  # optional
    translate_max_chars: int = 20000  # per request
    translations_per_hour: int = 30  # requests per user
    reader_effort: str = "high"  # output_config.effort for the reader
    readings_per_hour: int = 20  # papers a user may queue for reading per hour
    model_planner: str = "claude-sonnet-5-5"  # planner agent (M3)
    planner_effort: str = "medium"
    plans_per_hour: int = 10  # planner calls per user per hour (cached answers are free)
    model_concepts: str = "claude-sonnet-5-5"  # concept mapper (M17)
    concepts_effort: str = "high"
    concept_readings_per_hour: int = 20  # papers a user may queue for concept extraction
    concept_merges_per_hour: int = 10  # synonym passes per user per hour


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_path=Path(os.environ.get("BAYAN_DATABASE_PATH", "data/app.sqlite")),
        openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
        contact_email=os.environ.get("CONTACT_EMAIL") or None,
        app_secret_key=os.environ.get("APP_SECRET_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        reader_effort=os.environ.get("BAYAN_READER_EFFORT", "high"),
        readings_per_hour=int(os.environ.get("BAYAN_READINGS_PER_HOUR", "20")),
        model_reader=os.environ.get("BAYAN_MODEL_READER", "claude-sonnet-5-5"),
        model_screener=os.environ.get("BAYAN_MODEL_SCREENER", "claude-haiku-5-5"),
        model_planner=os.environ.get("BAYAN_MODEL_PLANNER", "claude-sonnet-5-5"),
        planner_effort=os.environ.get("BAYAN_PLANNER_EFFORT", "medium"),
        plans_per_hour=int(os.environ.get("BAYAN_PLANS_PER_HOUR", "10")),
        model_concepts=os.environ.get("BAYAN_MODEL_CONCEPTS", "claude-sonnet-5-5"),
        concepts_effort=os.environ.get("BAYAN_CONCEPTS_EFFORT", "high"),
        concept_readings_per_hour=int(os.environ.get("BAYAN_CONCEPT_READINGS_PER_HOUR", "20")),
        concept_merges_per_hour=int(os.environ.get("BAYAN_CONCEPT_MERGES_PER_HOUR", "10")),
        doi_lookup_budget=int(os.environ.get("BAYAN_DOI_LOOKUP_BUDGET", "50")),
        pdf_root=Path(os.environ.get("BAYAN_PDF_ROOT", "pdfs")),
        downloads_per_hour=int(os.environ.get("BAYAN_DOWNLOADS_PER_HOUR", "100")),
        download_host_delay=float(os.environ.get("BAYAN_DOWNLOAD_HOST_DELAY", "1.0")),
        job_stale_seconds=int(os.environ.get("BAYAN_JOB_STALE_SECONDS", "900")),
        job_resume_interval=float(os.environ.get("BAYAN_JOB_RESUME_INTERVAL", "60")),
        translate_url=os.environ.get("BAYAN_TRANSLATE_URL") or None,
        translate_api_key=os.environ.get("BAYAN_TRANSLATE_API_KEY") or None,
        translate_max_chars=int(os.environ.get("BAYAN_TRANSLATE_MAX_CHARS", "20000")),
        translations_per_hour=int(os.environ.get("BAYAN_TRANSLATIONS_PER_HOUR", "30")),
        cookie_secure=os.environ.get("BAYAN_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    )
