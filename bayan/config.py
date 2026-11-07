"""Settings read from the environment. Secrets never appear in repr or logs."""

import math
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
    local_searches_per_hour: int = 120  # saved local-library runs (nothing is sent anywhere)
    lookups_per_hour: int = 20  # OpenAlex ID lookups per user per hour (collection foundations)
    search_max_results: int = 200
    citations_max_age_days: int = 30  # older citation counts are "stale" and refreshed (M32)
    auto_refresh_days: int = 0  # refresh collections older than this many days; 0 = off
    auto_refresh_max_requests: int = 20  # budget of each automatic refresh
    refresh_requests_per_hour: int = 200  # request budgets a user may start per hour (manual)
    doi_lookup_budget: int = 50  # Crossref DOI lookups per run (cache hits are free)
    doi_lookup_seconds: float = 15.0  # time a run may spend waiting on those lookups
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
    model_ideas: str = "claude-sonnet-5-5"  # idea proposer and proposal drafts (M10)
    ideas_effort: str = "high"
    idea_runs_per_hour: int = 3  # pipeline runs per user per hour (cached calls are free)
    idea_inspiration_papers: int = 12  # papers the model is asked for inspirations from


def _seconds(name: str, default: str) -> float:
    """A number of seconds >= 0 from the environment; anything else stops startup."""
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a number of seconds, 0 or more (got {raw!r})")
    return value


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
        model_ideas=os.environ.get("BAYAN_MODEL_IDEAS", "claude-sonnet-5-5"),
        ideas_effort=os.environ.get("BAYAN_IDEAS_EFFORT", "high"),
        idea_runs_per_hour=int(os.environ.get("BAYAN_IDEA_RUNS_PER_HOUR", "3")),
        idea_inspiration_papers=int(os.environ.get("BAYAN_IDEA_INSPIRATION_PAPERS", "12")),
        doi_lookup_budget=int(os.environ.get("BAYAN_DOI_LOOKUP_BUDGET", "50")),
        doi_lookup_seconds=_seconds("BAYAN_DOI_LOOKUP_SECONDS", "15"),
        pdf_root=Path(os.environ.get("BAYAN_PDF_ROOT", "pdfs")),
        downloads_per_hour=int(os.environ.get("BAYAN_DOWNLOADS_PER_HOUR", "100")),
        download_host_delay=float(os.environ.get("BAYAN_DOWNLOAD_HOST_DELAY", "1.0")),
        job_stale_seconds=int(os.environ.get("BAYAN_JOB_STALE_SECONDS", "900")),
        job_resume_interval=float(os.environ.get("BAYAN_JOB_RESUME_INTERVAL", "60")),
        translate_url=os.environ.get("BAYAN_TRANSLATE_URL") or None,
        translate_api_key=os.environ.get("BAYAN_TRANSLATE_API_KEY") or None,
        translate_max_chars=int(os.environ.get("BAYAN_TRANSLATE_MAX_CHARS", "20000")),
        translations_per_hour=int(os.environ.get("BAYAN_TRANSLATIONS_PER_HOUR", "30")),
        lookups_per_hour=int(os.environ.get("BAYAN_LOOKUPS_PER_HOUR", "20")),
        citations_max_age_days=int(os.environ.get("BAYAN_CITATIONS_MAX_AGE_DAYS", "30")),
        auto_refresh_days=int(os.environ.get("BAYAN_AUTO_REFRESH_DAYS", "0")),
        auto_refresh_max_requests=int(os.environ.get("BAYAN_AUTO_REFRESH_MAX_REQUESTS", "20")),
        refresh_requests_per_hour=int(os.environ.get("BAYAN_REFRESH_REQUESTS_PER_HOUR", "200")),
        local_searches_per_hour=int(os.environ.get("BAYAN_LOCAL_SEARCHES_PER_HOUR", "120")),
        cookie_secure=os.environ.get("BAYAN_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    )
