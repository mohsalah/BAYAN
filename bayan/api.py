"""FastAPI app: accounts, library/language reference data, search runs and a small web UI."""

import csv
import io
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader

from . import __version__
from .analysis import MAX_GAP_TEXT, build_packet, gap_prompt, proposal_prompt
from .auth import (
    check_password_policy,
    create_session,
    end_session,
    hash_password,
    normalize_email,
    user_for_token,
    verify_password,
)
from .citations import to_bibtex, to_ris
from .concepts import (
    CONCEPT_TYPES,
    TYPE_LABELS,
    build_nodes,
    merge_nodes,
    normalize,
    run_concept_job,
    split_member,
)
from .concepts import result_record as concept_record
from .config import Settings, load_settings
from .connectors import (
    ArxivConnector,
    ConnectorError,
    CrossrefConnector,
    EuropePMCConnector,
    OpenAlexConnector,
)
from .connectors.base import Connector
from .db import connect, init_db
from .doctypes import type_key, type_report
from .downloads import (
    DOWNLOADED,
    NEEDS_ACCESS,
    PENDING,
    REASONS,
    Fetcher,
    _plain,
    finalize_pdf,
    new_part,
    now_iso,
    pdf_path,
    pdf_sink,
    public_record,
    quarantine_path,
    record_verdict,
    remove_stale_parts,
    resume_stale_jobs,
    run_download_job,
)
from .export import safe_cell, to_csv, to_xlsx
from .libraries import LIBRARIES, key_publishers
from .manuals import (
    add_to_run,
    clean_title,
    new_manual_id,
    owns_manual,
    rename_manual,
    store_manual,
    title_from_url,
)
from .membership import (
    EXAMPLE_DOI,
    InvalidInput,
    SecretNotConfigured,
    build_link,
    clean_key,
    delete_key,
    delete_template,
    get_template,
    key_status,
    paper_proxy_link,
    save_key,
    save_template,
    secret_problem,
    validate_template,
)
from .models import (
    CATEGORY_INFO,
    LANGUAGES,
    USER_SOURCE,
    language_key,
    language_name,
    normalize_document_types,
)
from .pages import INDEX_NOTES, clear_pages, page_list, page_text, search_pages
from .planner import (
    PLANNER_NOTE,
    Plan,
    PlanRequest,
    TopicLookup,
    make_plan,
    plan_record,
    save_edit,
)
from .reader import (
    DIMENSION_LABELS,
    AnthropicReader,
    ReaderClient,
    ReaderError,
    evidence_rows,
    result_record,
    run_reader_job,
)
from .regions import regions_of, regions_payload
from .search import SearchRequest, geo_status, paper_row_to_dict, run_search
from .translate import (
    MACHINE_LABEL,
    NOT_CONFIGURED_MESSAGE,
    OCR_MISSING_MESSAGE,
    SCANNED_MIN_CHARS,
    TARGETS,
    Engine,
    EngineError,
    NotConfigured,
    RateLimited,
    TranslateError,
    cached_or_translate,
    charge,
    check_pair,
    delete_glossary_term,
    detect_language,
    engine_from_settings,
    glossary_for,
    list_glossary,
    ocr_available,
    ocr_page,
    parse_pages,
    save_glossary_term,
)

COOKIE = "bayan_session"
STATIC = Path(__file__).parent / "static"
LOCALES = Path(__file__).parent / "locales"

ConnectorFactory = Callable[[str], Connector]

log = logging.getLogger("bayan.api")


class Credentials(BaseModel):
    email: str
    password: str


class PaperRef(BaseModel):
    source: str
    id: str


class DownloadRequest(BaseModel):
    run_id: int
    papers: list[PaperRef] = Field(min_length=1, max_length=50)


class ReadRequest(BaseModel):
    run_id: int
    papers: list[PaperRef] = Field(min_length=1, max_length=20)


class ConceptMergeRequest(BaseModel):
    run_id: int
    keys: list[str] = Field(min_length=2, max_length=50)


class ConceptSplitRequest(BaseModel):
    run_id: int
    key: str = Field(min_length=1, max_length=200)


class NormalizeRequest(BaseModel):
    run_id: int
    fresh: bool = False


class ExportRequest(BaseModel):
    format: Literal["csv", "xlsx", "bibtex", "ris"] = "csv"
    # None exports every paper of the run; otherwise only these (they must belong to the run).
    papers: list[PaperRef] | None = Field(default=None, max_length=500)


class ManualUrl(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=300)
    run_id: int | None = None  # None = personal manuals collection only


class ManualTitle(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class ManualToRun(BaseModel):
    paper_id: str = Field(min_length=1, max_length=100)


class GapRequest(BaseModel):
    papers: list[PaperRef] = Field(min_length=1, max_length=500)
    gap_count: int = Field(default=3, ge=1, le=5)
    question: str = Field(default="", max_length=1000)
    run_id: int | None = None


class ProposalRequest(BaseModel):
    papers: list[PaperRef] = Field(min_length=1, max_length=500)
    proposal_count: int = Field(default=1, ge=1, le=5)
    gap_text: str = Field(min_length=1, max_length=MAX_GAP_TEXT)
    question: str = Field(default="", max_length=1000)
    run_id: int | None = None


class KeyBody(BaseModel):
    # Deliberately unconstrained: a pydantic length error would echo the key in its 422 body.
    key: Any = None


class TemplateBody(BaseModel):
    template: Any = None


class ProxyLinkRequest(BaseModel):
    doi: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=2048)
    template: Any = None  # preview with an unsaved template; default is the saved one


MAX_PDF_PAGES = 50  # pages per translate request


class TranslateText(BaseModel):
    text: str = Field(max_length=200000)  # the configured per-request limit is applied later
    source_language: str = Field(default="auto", max_length=10)
    target_language: str = Field(max_length=10)


class TranslatePaper(BaseModel):
    source: str
    id: str
    source_language: str = Field(default="auto", max_length=10)
    target_language: str = Field(max_length=10)


class TranslatePdf(TranslatePaper):
    pages: str | None = Field(default=None, max_length=100)  # e.g. "1-3,5"; None = all


class GlossaryTerm(BaseModel):
    source_language: str = Field(max_length=10)
    target_language: str = Field(max_length=10)
    source_term: str = Field(max_length=300)
    target_term: str = Field(max_length=300)


CATEGORY_LABELS = {c["id"]: c["label"] for c in CATEGORY_INFO}


def default_connector_factory(settings: Settings) -> ConnectorFactory:
    def make(library_id: str) -> Connector:
        if library_id == "openalex":
            return OpenAlexConnector(
                api_key=settings.openalex_api_key, contact_email=settings.contact_email
            )
        if library_id == "crossref":
            return CrossrefConnector(contact_email=settings.contact_email)
        if library_id == "arxiv":
            return ArxivConnector(contact_email=settings.contact_email)
        if library_id == "europepmc":
            return EuropePMCConnector(contact_email=settings.contact_email)
        raise KeyError(library_id)

    return make


def reader_key_ok(key: str | None) -> bool:
    """A real-looking key, not empty and not the .env.example placeholder."""
    return bool(key) and not key.lower().startswith(("your-", "change-me"))


def create_app(
    settings: Settings | None = None, connector_factory: ConnectorFactory | None = None,
    fetcher: Fetcher | None = None, translator: Engine | None = None,
    reader: ReaderClient | None = None, planner: ReaderClient | None = None,
    topic_lookup: TopicLookup | None = None, concept_client: ReaderClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if reader is None and reader_key_ok(settings.anthropic_api_key):
        reader = AnthropicReader(settings.anthropic_api_key, settings.model_reader,
                                 settings.reader_effort)
    if planner is None and reader_key_ok(settings.anthropic_api_key):
        planner = AnthropicReader(settings.anthropic_api_key, settings.model_planner,
                                  settings.planner_effort, max_tokens=16000)
    if concept_client is None and reader_key_ok(settings.anthropic_api_key):
        concept_client = AnthropicReader(settings.anthropic_api_key, settings.model_concepts,
                                         settings.concepts_effort)
    topic_lookup = topic_lookup or OpenAlexConnector(
        api_key=settings.openalex_api_key, contact_email=settings.contact_email)
    translator = translator or engine_from_settings(settings.translate_url,
                                                    settings.translate_api_key)
    fetcher = fetcher or Fetcher(host_delay=settings.download_host_delay,
                                 max_retries=settings.download_retries,
                                 contact_email=settings.contact_email)
    make_connector = connector_factory or default_connector_factory(settings)

    def resume_loop(stop: threading.Event) -> None:
        """Continue interrupted download jobs and clear their leftover `.part` files."""
        while True:
            try:
                remove_stale_parts(settings.pdf_root)
                resume_stale_jobs(settings.database_path, fetcher, settings)
            except Exception as exc:  # keep checking; a failed pass is retried next time
                log.warning("job resume pass failed: %s", type(exc).__name__)
            if stop.wait(settings.job_resume_interval):
                return

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(settings.database_path)
        stop = threading.Event()
        worker = None
        if settings.job_resume_interval > 0:
            worker = threading.Thread(target=resume_loop, args=(stop,), daemon=True,
                                      name="bayan-job-resume")
            worker.start()
        yield
        stop.set()
        if worker is not None:
            worker.join(timeout=5)

    app = FastAPI(title="BAYAN", version=__version__, lifespan=lifespan)

    def get_conn():
        conn = connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def session_token(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return request.cookies.get(COOKIE)

    def current_user(request: Request, conn: Conn) -> sqlite3.Row:
        token = session_token(request)
        user = user_for_token(conn, token) if token else None
        if user is None:
            raise HTTPException(401, "Not signed in")
        return user

    User = Annotated[sqlite3.Row, Depends(current_user)]

    def start_session(response: Response, conn: sqlite3.Connection, user_id: int) -> None:
        token = create_session(conn, user_id, settings.session_days)
        response.set_cookie(
            COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
            samesite="lax", secure=settings.cookie_secure,
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/libraries")
    def libraries() -> list[dict]:
        return list(LIBRARIES)

    @app.get("/api/languages")
    def languages() -> dict:
        return LANGUAGES

    @app.get("/api/locales/{code}")
    def locale(code: str) -> dict:
        """Interface strings for one of the six languages (en is the source of truth)."""
        if code not in LANGUAGES:
            raise HTTPException(404, f"Unsupported interface language: {code}")
        return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))

    @app.get("/api/regions")
    def regions() -> dict:
        return regions_payload()

    @app.get("/api/document-types")
    def document_types() -> list[dict]:
        return list(CATEGORY_INFO)

    @app.post("/api/auth/register", status_code=201)
    def register(body: Credentials, response: Response, conn: Conn) -> dict:
        try:
            email = normalize_email(body.email)
            check_password_policy(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            cur = conn.execute(
                "INSERT INTO users(email, password_hash) VALUES(?,?)",
                (email, hash_password(body.password)),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "An account with this email already exists") from exc
        start_session(response, conn, cur.lastrowid)
        return {"email": email}

    @app.post("/api/auth/login")
    def login(body: Credentials, response: Response, conn: Conn) -> dict:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?",
            (body.email.strip().lower(),),
        ).fetchone()
        # Same message for unknown email and wrong password.
        if row is None or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "Wrong email or password")
        start_session(response, conn, row["id"])
        return {"email": row["email"]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, conn: Conn) -> dict:
        token = session_token(request)
        if token:
            end_session(conn, token)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User) -> dict:
        return {"email": user["email"]}

    @app.post("/api/search")
    def search(body: SearchRequest, user: User, conn: Conn) -> dict:
        available = {lib["id"]: lib for lib in LIBRARIES}
        for lib_id in body.libraries:
            lib = available.get(lib_id)
            if lib is None:
                raise HTTPException(422, f"Unknown library: {lib_id}")
            if lib["status"] != "available":
                raise HTTPException(
                    422, f"{lib['name']} is not available yet (status: {lib['status']})"
                )
            if lib["role"] != "search":
                raise HTTPException(
                    422, f"{lib['name']} is used automatically to find missing DOIs, "
                    "not as a search source"
                )
        if body.quota_total and body.quota_total > settings.search_max_results:
            raise HTTPException(
                422, f"quota_total is limited to {settings.search_max_results} per search"
            )
        recent = conn.execute(
            "SELECT COUNT(*) FROM search_runs WHERE user_id = ?"
            " AND created_at > datetime('now', '-1 hour')",
            (user["id"],),
        ).fetchone()[0]
        if recent >= settings.searches_per_hour:
            raise HTTPException(429, "Search limit reached for this hour; try again later")
        try:
            connectors = [make_connector(lib_id) for lib_id in body.libraries]
            return run_search(
                conn, user["id"], body, connectors, settings.search_max_results,
                doi_lookup=make_connector("crossref"), doi_budget=settings.doi_lookup_budget,
            )
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/runs")
    def runs(user: User, conn: Conn) -> list[dict]:
        rows = conn.execute(
            "SELECT id, summary, created_at FROM search_runs WHERE user_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"],
                 "summary": json.loads(r["summary"])} for r in rows]

    def decorate(rows: list[sqlite3.Row], summary: dict) -> list[dict]:
        matches = {(m["source"], m["id"]): m for m in summary.get("doi_matches", [])}
        geo = summary.get("geography") or {}
        geo_codes = set(geo.get("expanded_countries") or [])
        out = []
        for p in rows:
            d = paper_row_to_dict(p)
            match = matches.get((d["source"], d["id"]))
            d["doi_source"] = "crossref_lookup" if match else ""
            d["doi_match_score"] = match["score"] if match else None
            d["category_label"] = CATEGORY_LABELS.get(d["category"], d["category"])
            d["language_key"] = language_key(d["language"])
            d["language_name"] = language_name(d["language"])
            d["type_key"] = type_key(d)
            d["regions"] = regions_of(d["countries"])
            d["geo_status"] = (geo_status(d["countries"], geo_codes, geo.get("mode", "include"))
                               if geo_codes else "")
            d["type_label"] = ("Manual (user-supplied)" if d["type_key"] == "manual"
                               else d["category_label"])
            out.append(d)
        return out

    def owned_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> sqlite3.Row:
        run = conn.execute(
            "SELECT id, request, summary, created_at FROM search_runs WHERE id = ? AND user_id = ?",
            (run_id, user_id),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Run not found")
        return run

    def run_papers(conn: sqlite3.Connection, run_id: int, summary: dict) -> list[dict]:
        rows = conn.execute(
            "SELECT p.* FROM run_papers rp"
            " JOIN papers p ON p.source = rp.source AND p.id = rp.paper_id"
            " WHERE rp.run_id = ? ORDER BY rp.rank",
            (run_id,),
        ).fetchall()
        return decorate(rows, summary)

    def user_papers(conn: sqlite3.Connection, user_id: int, refs: list[PaperRef]) -> list[dict]:
        """Papers the user found in their own runs, in the order given. Anything else is a 404."""
        wanted = list(dict.fromkeys((r.source, r.id) for r in refs))
        out = []
        for source, paper_id in wanted:
            row = conn.execute(
                "SELECT p.* FROM papers p WHERE p.source = ? AND p.id = ? AND EXISTS ("
                " SELECT 1 FROM run_papers rp JOIN search_runs r ON r.id = rp.run_id"
                " WHERE r.user_id = ? AND rp.source = p.source AND rp.paper_id = p.id)",
                (source, paper_id, user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "One or more papers are not in your search runs")
            out.append(row)
        return decorate(out, {})

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int, user: User, conn: Conn) -> dict:
        run = conn.execute(
            "SELECT id, request, summary, created_at FROM search_runs WHERE id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Run not found")
        papers = conn.execute(
            "SELECT p.* FROM run_papers rp"
            " JOIN papers p ON p.source = rp.source AND p.id = rp.paper_id"
            " WHERE rp.run_id = ? ORDER BY rp.rank",
            (run_id,),
        ).fetchall()
        summary = json.loads(run["summary"])
        out = decorate(papers, summary)
        request = json.loads(run["request"])
        profile = request.get("profile")
        if isinstance(profile, dict) and "document_types" in profile:
            # Runs stored before the five categories used article/review/manual.
            profile["document_types"] = normalize_document_types(profile["document_types"])
        selected = (profile or {}).get("document_types", []) if isinstance(profile, dict) else []
        proxy = get_template(conn, user["id"])
        if proxy is not None:
            for d in out:
                d["proxy_link"] = paper_proxy_link(proxy["template"], d)
        return {"id": run["id"], "created_at": run["created_at"],
                "request": request, "summary": summary, "papers": out,
                "type_report": type_report(selected, out, request.get("libraries", []))}

    @app.post("/api/runs/{run_id}/export")
    def export_run(run_id: int, body: ExportRequest, user: User, conn: Conn) -> Response:
        run = owned_run(conn, run_id, user["id"])
        summary = json.loads(run["summary"])
        papers = run_papers(conn, run_id, summary)
        if body.papers is not None:
            in_run = {(p["source"], p["id"]): p for p in papers}
            chosen = list(dict.fromkeys((r.source, r.id) for r in body.papers))
            missing = [k for k in chosen if k not in in_run]
            if missing:
                raise HTTPException(404, "One or more papers are not in this run")
            papers = [in_run[k] for k in chosen]
        if body.format == "xlsx":
            content = to_xlsx(papers, CATEGORY_LABELS, {
                "id": run_id, "request": json.loads(run["request"]), "summary": summary})
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif body.format == "bibtex":
            content, media = to_bibtex(papers).encode(), "application/x-bibtex; charset=utf-8"
        elif body.format == "ris":
            content = to_ris(papers).encode()
            media = "application/x-research-info-systems; charset=utf-8"
        else:
            content, media = to_csv(papers, CATEGORY_LABELS), "text/csv; charset=utf-8"
        ext = {"bibtex": "bib"}.get(body.format, body.format)
        name = f"bayan-run-{run_id}.{ext}"
        return Response(content, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def save_analysis(conn: sqlite3.Connection, user_id: int, kind: str, run_id: int | None,
                      request: dict, prompt: str, packet: dict) -> dict:
        cur = conn.execute(
            "INSERT INTO analysis_requests(user_id, kind, run_id, request, prompt, packet)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, kind, run_id, json.dumps(request), prompt, json.dumps(packet)),
        )
        conn.commit()
        return {"id": cur.lastrowid, "kind": kind, "mode": "prompt", "prompt": prompt,
                "packet": packet,
                "note": "This prepares a prompt and an evidence packet. No analysis has run: "
                        "submit the prompt to an AI assistant. Candidate gaps and proposals "
                        "need checking against full texts and a wider literature search."}

    @app.post("/api/analysis/gaps")
    def analysis_gaps(body: GapRequest, user: User, conn: Conn) -> dict:
        papers = user_papers(conn, user["id"], body.papers)
        request = {"gap_count": body.gap_count, "question": body.question}
        packet = build_packet("research_gaps", papers, request, len(papers))
        prompt = gap_prompt(packet, papers, body.gap_count, body.question)
        return save_analysis(
            conn, user["id"], "research_gaps", body.run_id, request, prompt, packet)

    @app.post("/api/analysis/proposals")
    def analysis_proposals(body: ProposalRequest, user: User, conn: Conn) -> dict:
        papers = user_papers(conn, user["id"], body.papers)
        request = {"proposal_count": body.proposal_count, "question": body.question,
                   "gap_text": body.gap_text}
        packet = build_packet("proposals", papers, request, len(papers))
        prompt = proposal_prompt(packet, papers, body.proposal_count, body.gap_text, body.question)
        return save_analysis(conn, user["id"], "proposals", body.run_id, request, prompt, packet)

    @app.get("/api/analysis/{analysis_id}")
    def analysis_detail(analysis_id: int, user: User, conn: Conn) -> dict:
        row = conn.execute(
            "SELECT id, kind, run_id, request, prompt, packet, created_at FROM analysis_requests"
            " WHERE id = ? AND user_id = ?", (analysis_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Request not found")
        return {"id": row["id"], "kind": row["kind"], "run_id": row["run_id"],
                "created_at": row["created_at"], "request": json.loads(row["request"]),
                "prompt": row["prompt"], "packet": json.loads(row["packet"])}

    # ---- PDF downloads: selection, background job, status, upload, owner-only file ----

    def download_rows(conn: sqlite3.Connection, user_id: int,
                      keys: list[tuple[str, str]]) -> list[dict]:
        out = []
        for source, paper_id in keys:
            row = conn.execute(
                "SELECT * FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
                (user_id, source, paper_id)).fetchone()
            out.append(public_record(row) if row else
                       {"source": source, "id": paper_id, "status": "not_requested"})
        return out

    @app.post("/api/downloads", status_code=202)
    def start_downloads(body: DownloadRequest, background: BackgroundTasks, user: User,
                        conn: Conn) -> dict:
        run = owned_run(conn, body.run_id, user["id"])
        in_run = {(p["source"], p["id"]) for p in run_papers(conn, run["id"], {})}
        wanted = list(dict.fromkeys((r.source, r.id) for r in body.papers))
        if any(k not in in_run for k in wanted):
            raise HTTPException(404, "One or more papers are not in this run")
        have = {(r["source"], r["paper_id"]) for r in conn.execute(
            "SELECT source, paper_id FROM downloads WHERE user_id = ? AND status IN (?, ?)",
            (user["id"], *DOWNLOADED))}
        queue = [k for k in wanted if k not in have]
        already = [k for k in wanted if k in have]
        recent = conn.execute(
            "SELECT COUNT(*) FROM download_attempts WHERE user_id = ?"
            " AND created_at > datetime('now', '-1 hour')", (user["id"],)).fetchone()[0]
        if recent + len(queue) > settings.downloads_per_hour:
            raise HTTPException(
                429, f"Download limit reached: {settings.downloads_per_hour} papers per hour "
                f"({max(0, settings.downloads_per_hour - recent)} left). Try again later.")
        job_id = None
        if queue:
            conn.executemany("INSERT INTO download_attempts(user_id) VALUES(?)",
                             [(user["id"],)] * len(queue))
            for source, paper_id in queue:
                conn.execute(
                    "INSERT INTO downloads(user_id, source, paper_id, status) VALUES(?,?,?,?)"
                    " ON CONFLICT(user_id, source, paper_id) DO UPDATE SET status = ?,"
                    " reason = NULL, detail = NULL, updated_at = CURRENT_TIMESTAMP",
                    (user["id"], source, paper_id, PENDING, PENDING))
            cur = conn.execute(
                "INSERT INTO jobs(kind, payload, status) VALUES('pdf_download', ?, 'pending')",
                (json.dumps({"user_id": user["id"], "run_id": run["id"], "papers": queue}),))
            job_id = cur.lastrowid
            conn.commit()
            background.add_task(run_download_job, settings.database_path, job_id, fetcher,
                                settings)
        return {"job_id": job_id,
                "queued": [{"source": s, "id": i} for s, i in queue],
                "already_downloaded": [{"source": s, "id": i} for s, i in already]}

    @app.get("/api/downloads/jobs/{job_id}")
    def download_job(job_id: int, user: User, conn: Conn) -> dict:
        job = conn.execute("SELECT id, kind, payload, status, error FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
        payload = json.loads(job["payload"]) if job and job["kind"] == "pdf_download" else {}
        if not payload or payload.get("user_id") != user["id"]:
            raise HTTPException(404, "Job not found")
        papers = download_rows(conn, user["id"], [tuple(p) for p in payload["papers"]])
        return {"id": job["id"], "status": job["status"], "error": job["error"],
                "total": len(papers),
                "finished": sum(p["status"] != PENDING for p in papers), "papers": papers}

    MANIFEST_COLUMNS = (
        "source", "id", "title", "doi", "year", "status", "reason", "reason_text", "detail",
        "origin", "size", "sha256", "source_url", "final_url", "http_status", "retrieved_at",
        "license", "version", "identity_score", "identity_basis", "page_count",
        "pages_with_text", "index_status")

    @app.get("/api/downloads/manifest")
    def download_manifest(run_id: int, user: User, conn: Conn,
                          format: Literal["json", "csv"] = "json") -> Response:
        """Every paper of the run with its download outcome. Papers never requested are listed
        as `not_requested`, so the manifest always accounts for the whole run."""
        run = owned_run(conn, run_id, user["id"])
        papers = run_papers(conn, run["id"], {})
        records = download_rows(conn, user["id"], [(p["source"], p["id"]) for p in papers])
        rows = []
        for paper, record in zip(papers, records, strict=True):
            row = {c: record.get(c) for c in MANIFEST_COLUMNS}
            row.update(source=paper["source"], id=paper["id"], title=paper.get("title"),
                       doi=paper.get("doi"), year=paper.get("year"))
            row["reason_text"] = record.get("reason_text") or None
            rows.append(row)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(MANIFEST_COLUMNS)
            for row in rows:
                writer.writerow(["" if row[c] is None else safe_cell(row[c])
                                 for c in MANIFEST_COLUMNS])
            return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
                "Content-Disposition": f'attachment; filename="bayan-run-{run_id}-manifest.csv"'})
        body = {"run_id": run["id"], "generated_at": now_iso(), "total": len(rows),
                "by_status": counts, "papers": rows}
        return Response(json.dumps(body, ensure_ascii=False), media_type="application/json")

    # ---- Page index of the user's validated PDFs (private to the owner) ----

    def indexed_download(conn: sqlite3.Connection, user_id: int, source: str,
                         paper_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT status, page_count, pages_with_text, index_status FROM downloads"
            " WHERE user_id = ? AND source = ? AND paper_id = ?",
            (user_id, source, paper_id)).fetchone()
        if row is None or row["status"] not in DOWNLOADED:
            raise HTTPException(404, "No validated PDF for this paper")
        return row

    @app.get("/api/pages")
    def list_pages(source: str, paper_id: str, user: User, conn: Conn) -> dict:
        row = indexed_download(conn, user["id"], source, paper_id)
        status = row["index_status"]
        return {"source": source, "id": paper_id, "index_status": status,
                "note": INDEX_NOTES.get(status or "", "The page index has not been built."),
                "page_count": row["page_count"], "pages_with_text": row["pages_with_text"],
                "pages": page_list(conn, user["id"], source, paper_id)}

    @app.get("/api/pages/text")
    def get_page_text(source: str, paper_id: str, page: int, user: User, conn: Conn) -> dict:
        indexed_download(conn, user["id"], source, paper_id)
        row = page_text(conn, user["id"], source, paper_id, page)
        if row is None:
            raise HTTPException(404, "Page not found")
        return {"source": source, "id": paper_id, "page": row["page"], "label": row["label"],
                "text": row["text"], "chars": row["chars"], "sha256": row["sha256"],
                "has_text": bool(row["text"].strip())}

    @app.get("/api/pages/search")
    def page_search(q: str, user: User, conn: Conn, run_id: int | None = None,
                    limit: int = 20) -> list[dict]:
        papers = None
        if run_id is not None:
            run = owned_run(conn, run_id, user["id"])
            papers = [(p["source"], p["id"]) for p in run_papers(conn, run["id"], {})]
        return search_pages(conn, user["id"], q, papers, max(1, min(limit, 100)))

    @app.get("/api/downloads")
    def download_status(run_id: int, user: User, conn: Conn) -> list[dict]:
        run = owned_run(conn, run_id, user["id"])
        keys = [(p["source"], p["id"]) for p in run_papers(conn, run["id"], {})]
        return [r for r in download_rows(conn, user["id"], keys)
                if r["status"] != "not_requested"]

    async def receive_pdf(request: Request, part: Path) -> None:
        """Check the upload headers, then stream the body to `part` within the size limit."""
        if not request.headers.get("content-type", "").lower().startswith("application/pdf"):
            raise HTTPException(415, "Upload a PDF (Content-Type: application/pdf)")
        limit = settings.pdf_max_bytes
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, "The PDF is larger than the allowed size")
        part.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with open(part, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "The PDF is larger than the allowed size")
                    out.write(chunk)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    @app.post("/api/downloads/upload")
    async def upload_pdf(source: str, paper_id: str, request: Request, user: User) -> dict:
        # The request-scoped connection belongs to another thread, so open short-lived ones here.
        def lookup() -> sqlite3.Row:
            with closing(connect(settings.database_path)) as c:
                return user_papers(c, user["id"], [PaperRef(source=source, id=paper_id)])[0]

        paper = await run_in_threadpool(lookup)
        part = new_part(settings.pdf_root, user["id"], source, paper_id)
        await receive_pdf(request, part)

        def finish() -> dict:
            verdict = finalize_pdf(part, settings.pdf_root, user["id"], source, paper_id,
                                   paper["title"] or "", paper["doi"] or "",
                                   settings.pdf_max_bytes)
            if verdict.status == "needs_access":
                raise HTTPException(422, verdict.detail)
            with closing(connect(settings.database_path)) as c:
                record_verdict(c, user["id"], source, paper_id, verdict, origin="upload",
                               source_url="upload", retrieved_at=now_iso())
                return download_rows(c, user["id"], [(source, paper_id)])[0]

        return await run_in_threadpool(finish)

    @app.get("/api/downloads/file")
    def download_file(source: str, paper_id: str, user: User, conn: Conn) -> FileResponse:
        row = conn.execute(
            "SELECT status FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
            (user["id"], source, paper_id)).fetchone()
        path = pdf_path(settings.pdf_root, user["id"], source, paper_id)
        if row is None or row["status"] not in DOWNLOADED or not path.is_file():
            raise HTTPException(404, "No validated PDF for this paper")
        return FileResponse(path, media_type="application/pdf",
                            headers={"Cache-Control": "private, no-store",
                                     "X-Content-Type-Options": "nosniff"})

    # ---- Manuals: added by URL or upload, never searched for; private to the owner ----

    def manual_view(conn: sqlite3.Connection, user_id: int, paper_id: str,
                    run_id: int | None = None) -> dict:
        row = conn.execute("SELECT * FROM papers WHERE source = ? AND id = ?",
                           (USER_SOURCE, paper_id)).fetchone()
        return {"paper": decorate([row], {})[0],
                "download": download_rows(conn, user_id, [(USER_SOURCE, paper_id)])[0],
                "origin": conn.execute(
                    "SELECT origin FROM manuals WHERE user_id = ? AND paper_id = ?",
                    (user_id, paper_id)).fetchone()["origin"],
                "run_id": run_id}

    def finish_manual(conn: sqlite3.Connection, user_id: int, paper_id: str, part: Path,
                      title: str, origin: str, source_url: str, run_id: int | None,
                      **meta: object) -> dict:
        verdict = finalize_pdf(part, settings.pdf_root, user_id, USER_SOURCE, paper_id,
                               "", "", settings.pdf_max_bytes)
        if verdict.status == NEEDS_ACCESS:
            raise HTTPException(422, verdict.detail or REASONS.get(verdict.reason, "Not a PDF."))
        same = conn.execute(
            "SELECT p.title FROM downloads d JOIN papers p ON p.source = d.source AND"
            " p.id = d.paper_id WHERE d.user_id = ? AND d.source = ? AND d.sha256 = ?",
            (user_id, USER_SOURCE, verdict.sha256)).fetchone()
        if same is not None:
            if verdict.path:
                verdict.path.unlink(missing_ok=True)
            raise HTTPException(409, f"You already added this file as '{same['title']}'")
        # The title is the user's own words, so the file was not matched against a record.
        verdict.detail = ("The title is user-supplied; the file's identity was not checked "
                          "against a library record.")
        verdict.basis, verdict.score = "user_supplied", None
        record_verdict(conn, user_id, USER_SOURCE, paper_id, verdict, origin=origin,
                       source_url=source_url or origin, retrieved_at=now_iso(), **meta)
        store_manual(conn, user_id, paper_id, title, origin, source_url, run_id)
        return manual_view(conn, user_id, paper_id, run_id)

    @app.post("/api/manuals/url", status_code=201)
    async def add_manual_url(body: ManualUrl, user: User) -> dict:
        def work() -> dict:
            with closing(connect(settings.database_path)) as c:
                if body.run_id is not None:
                    owned_run(c, body.run_id, user["id"])
                recent = c.execute(
                    "SELECT COUNT(*) FROM download_attempts WHERE user_id = ?"
                    " AND created_at > datetime('now', '-1 hour')", (user["id"],)).fetchone()[0]
                if recent + 1 > settings.downloads_per_hour:
                    raise HTTPException(
                        429, f"Download limit reached: {settings.downloads_per_hour} files per "
                        "hour. Try again later.")
                c.execute("INSERT INTO download_attempts(user_id) VALUES(?)", (user["id"],))
                c.commit()
                url = body.url.strip()
                paper_id = new_manual_id()
                part = new_part(settings.pdf_root, user["id"], USER_SOURCE, paper_id)
                res = fetcher.fetch(url, pdf_sink(part, settings.pdf_max_bytes))
                if not res.ok:
                    part.unlink(missing_ok=True)
                    status = {"unsafe_url": 422, "html_returned": 422, "not_pdf": 422,
                              "too_large": 413}.get(res.code, 502)
                    raise HTTPException(status, res.detail or REASONS.get(res.code, "Failed."))
                title = clean_title(body.title, title_from_url(url))
                return finish_manual(c, user["id"], paper_id, part, title, "url", _plain(url),
                                     body.run_id, final_url=res.final_url,
                                     http_status=res.status)

        return await run_in_threadpool(work)

    @app.post("/api/manuals/upload", status_code=201)
    async def add_manual_upload(request: Request, user: User, title: str = "",
                                run_id: int | None = None) -> dict:
        def check_run() -> None:
            if run_id is not None:
                with closing(connect(settings.database_path)) as c:
                    owned_run(c, run_id, user["id"])

        await run_in_threadpool(check_run)
        paper_id = new_manual_id()
        part = new_part(settings.pdf_root, user["id"], USER_SOURCE, paper_id)
        await receive_pdf(request, part)

        def work() -> dict:
            with closing(connect(settings.database_path)) as c:
                return finish_manual(c, user["id"], paper_id, part, clean_title(title),
                                     "upload", "", run_id)

        return await run_in_threadpool(work)

    @app.get("/api/manuals")
    def list_manuals(user: User, conn: Conn) -> list[dict]:
        ids = [r["paper_id"] for r in conn.execute(
            "SELECT paper_id FROM manuals WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
            (user["id"],))]
        return [manual_view(conn, user["id"], i) for i in ids]

    @app.patch("/api/manuals/{paper_id}")
    def edit_manual(paper_id: str, body: ManualTitle, user: User, conn: Conn) -> dict:
        if not owns_manual(conn, user["id"], paper_id):
            raise HTTPException(404, "Manual not found")
        rename_manual(conn, paper_id, clean_title(body.title))
        return manual_view(conn, user["id"], paper_id)

    @app.post("/api/runs/{run_id}/manuals", status_code=201)
    def add_manual_to_run(run_id: int, body: ManualToRun, user: User, conn: Conn) -> dict:
        owned_run(conn, run_id, user["id"])
        if not owns_manual(conn, user["id"], body.paper_id):
            raise HTTPException(404, "Manual not found")
        add_to_run(conn, run_id, body.paper_id)
        conn.commit()
        return manual_view(conn, user["id"], body.paper_id, run_id)

    # ---- Membership: encrypted keys, institutional proxy link, private uploads ----

    publisher_names = {lib["id"]: lib["name"] for lib in key_publishers()}

    def known_publisher(publisher: str) -> str:
        if publisher not in publisher_names:
            raise HTTPException(404, "Unknown publisher")
        return publisher

    def membership_view(conn: sqlite3.Connection, user_id: int) -> dict:
        problem = secret_problem(settings.app_secret_key)
        proxy = get_template(conn, user_id)
        return {
            "keys_enabled": not problem,
            "keys_disabled_reason": problem or None,
            "publishers": [
                {"id": pid, "name": name,
                 **key_status(conn, settings.app_secret_key, user_id, pid)}
                for pid, name in publisher_names.items()],
            "proxy": ({"template": proxy["template"], "updated_at": proxy["updated_at"],
                       "example": build_link(proxy["template"], doi=EXAMPLE_DOI)}
                      if proxy else None),
            "note": "An email address alone gives no access. Saved keys are stored encrypted "
                    "and no connector uses them yet.",
        }

    @app.get("/api/membership")
    def membership(user: User, conn: Conn) -> dict:
        return membership_view(conn, user["id"])

    @app.put("/api/membership/proxy")
    def put_proxy(body: TemplateBody, user: User, conn: Conn) -> dict:
        try:
            save_template(conn, user["id"], validate_template(body.template))
        except InvalidInput as exc:
            raise HTTPException(422, str(exc)) from exc
        return membership_view(conn, user["id"])

    @app.delete("/api/membership/proxy")
    def remove_proxy(user: User, conn: Conn) -> dict:
        delete_template(conn, user["id"])
        return membership_view(conn, user["id"])

    @app.post("/api/membership/proxy-link")
    def proxy_link(body: ProxyLinkRequest, user: User, conn: Conn) -> dict:
        try:
            if body.template is not None:
                template = validate_template(body.template)
            else:
                saved = get_template(conn, user["id"])
                if saved is None:
                    raise HTTPException(404, "Save an institutional proxy template first")
                template = saved["template"]
            link = build_link(template, doi=body.doi, url=body.url)
        except InvalidInput as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"link": link,
                "note": "Open this link in your browser while signed in to your institution. "
                        "BAYAN does not log in or fetch through the proxy."}

    @app.put("/api/membership/{publisher}/key")
    def put_key(publisher: str, body: KeyBody, user: User, conn: Conn) -> dict:
        known_publisher(publisher)
        try:
            save_key(conn, settings.app_secret_key, user["id"], publisher, clean_key(body.key))
        except SecretNotConfigured as exc:
            raise HTTPException(503, str(exc)) from exc
        except InvalidInput as exc:
            raise HTTPException(422, str(exc)) from exc
        return membership_view(conn, user["id"])

    @app.delete("/api/membership/{publisher}/key")
    def remove_key(publisher: str, user: User, conn: Conn) -> dict:
        delete_key(conn, user["id"], known_publisher(publisher))
        return membership_view(conn, user["id"])

    def upload_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT d.*, p.title, p.document_kind FROM downloads d"
            " LEFT JOIN papers p ON p.source = d.source AND p.id = d.paper_id"
            " WHERE d.user_id = ? AND d.origin = 'upload' ORDER BY d.updated_at DESC, d.rowid DESC",
            (user_id,)).fetchall()

    @app.get("/api/uploads")
    def my_uploads(user: User, conn: Conn) -> list[dict]:
        return [{**public_record(r), "title": r["title"] or "",
                 "kind": "manual" if r["document_kind"] == "manual" else "paper"}
                for r in upload_rows(conn, user["id"])]

    @app.delete("/api/uploads")
    def delete_upload(source: str, paper_id: str, user: User, conn: Conn) -> dict:
        mine = {(r["source"], r["paper_id"]) for r in upload_rows(conn, user["id"])}
        if (source, paper_id) not in mine:
            raise HTTPException(404, "Upload not found")
        for path in (pdf_path(settings.pdf_root, user["id"], source, paper_id),
                     quarantine_path(settings.pdf_root, user["id"], source, paper_id)):
            path.unlink(missing_ok=True)
        clear_pages(conn, user["id"], source, paper_id)
        conn.execute("DELETE FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
                     (user["id"], source, paper_id))
        if source == USER_SOURCE and owns_manual(conn, user["id"], paper_id):
            # A manual is nothing without its file: remove the record and its run entries too.
            conn.execute("DELETE FROM manuals WHERE user_id = ? AND paper_id = ?",
                         (user["id"], paper_id))
            conn.execute("DELETE FROM run_papers WHERE source = ? AND paper_id = ?",
                         (USER_SOURCE, paper_id))
            conn.execute("DELETE FROM paper_search WHERE source = ? AND id = ?",
                         (USER_SOURCE, paper_id))
            conn.execute("DELETE FROM papers WHERE source = ? AND id = ?",
                         (USER_SOURCE, paper_id))
        conn.commit()
        return {"deleted": {"source": source, "id": paper_id}}

    # ---- Translator (M24): machine translation to en/ar; never evidence ----

    def translator_error(exc: TranslateError) -> HTTPException:
        code = 503 if isinstance(exc, NotConfigured) else 429 if isinstance(exc, RateLimited) \
            else 502 if isinstance(exc, EngineError) else 422
        return HTTPException(code, str(exc))

    def resolve_source(source: str, text: str, fallback: str | None = None) -> str:
        if source != "auto":
            return source
        guess = detect_language(text) or (fallback if fallback in LANGUAGES else None)
        if guess is None:
            raise HTTPException(
                422, "Could not detect the source language; choose it from the list.")
        return guess

    def run_translation(conn: sqlite3.Connection, user_id: int, text: str, source: str,
                        target: str, origin: str = "text") -> dict:
        try:
            check_pair(source, target)
            result, cached = cached_or_translate(
                conn, user_id, translator, text, source, target,
                glossary_for(conn, user_id, source, target), origin)
        except TranslateError as exc:
            raise translator_error(exc) from exc
        return {**result.to_dict(), "cached": cached}

    def spend(conn: sqlite3.Connection, user_id: int, chars: int) -> None:
        if translator is None:
            raise translator_error(NotConfigured(NOT_CONFIGURED_MESSAGE))
        try:
            charge(conn, user_id, chars, settings.translations_per_hour,
                   settings.translate_max_chars)
        except TranslateError as exc:
            raise translator_error(exc) from exc

    @app.get("/api/translate/status")
    def translate_status(user: User) -> dict:
        return {
            "status": "ready" if translator else "not_configured",
            "message": "" if translator else NOT_CONFIGURED_MESSAGE,
            "engine": translator.name if translator else None,
            "targets": list(TARGETS), "sources": list(LANGUAGES),
            "max_chars": settings.translate_max_chars,
            "per_hour": settings.translations_per_hour,
            "ocr": {"available": ocr_available(),
                    "message": "" if ocr_available() else OCR_MISSING_MESSAGE},
        }

    @app.post("/api/translate")
    def translate_pasted(body: TranslateText, user: User, conn: Conn) -> dict:
        if not body.text.strip():
            raise HTTPException(422, "There is no text to translate.")
        spend(conn, user["id"], len(body.text))
        source = resolve_source(body.source_language, body.text)
        return run_translation(conn, user["id"], body.text, source, body.target_language)

    def own_paper(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str) -> dict:
        if source == USER_SOURCE:
            if not owns_manual(conn, user_id, paper_id):
                raise HTTPException(404, "Paper not found")
            row = conn.execute("SELECT * FROM papers WHERE source = ? AND id = ?",
                               (source, paper_id)).fetchone()
            if row is None:
                raise HTTPException(404, "Paper not found")
            return decorate([row], {})[0]
        return user_papers(conn, user_id, [PaperRef(source=source, id=paper_id)])[0]

    @app.post("/api/translate/paper")
    def translate_paper(body: TranslatePaper, user: User, conn: Conn) -> dict:
        paper = own_paper(conn, user["id"], body.source, body.id)
        title, abstract = paper.get("title") or "", paper.get("abstract") or ""
        if not (title or abstract):
            raise HTTPException(422, "This paper has no title or abstract to translate.")
        spend(conn, user["id"], len(title) + len(abstract))
        sample = f"{title}\n{abstract}"
        source = resolve_source(
            body.source_language if body.source_language != "auto" else
            (paper.get("language") if paper.get("language") in LANGUAGES else "auto"),
            sample)
        out = {"paper": {"source": body.source, "id": body.id},
               "title": None, "abstract": None}
        if title:
            out["title"] = run_translation(conn, user["id"], title, source, body.target_language)
        if abstract:
            out["abstract"] = run_translation(conn, user["id"], abstract, source,
                                              body.target_language)
        return out

    @app.post("/api/translate/pdf")
    def translate_pdf(body: TranslatePdf, user: User, conn: Conn) -> dict:
        row = conn.execute(
            "SELECT status FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
            (user["id"], body.source, body.id)).fetchone()
        path = pdf_path(settings.pdf_root, user["id"], body.source, body.id)
        if row is None or row["status"] not in DOWNLOADED or not path.is_file():
            raise HTTPException(404, "No validated PDF of yours for this paper")
        if translator is None:
            raise translator_error(NotConfigured(NOT_CONFIGURED_MESSAGE))
        try:
            check_pair(body.source_language if body.source_language != "auto" else "en",
                       body.target_language)
            reader = PdfReader(str(path))
            pages = parse_pages(body.pages, len(reader.pages))
        except TranslateError as exc:
            raise translator_error(exc) from exc
        except Exception as exc:  # pypdf raises many types on damaged files
            raise HTTPException(422, "The PDF could not be read.") from exc
        if len(pages) > MAX_PDF_PAGES:
            raise HTTPException(422, f"Choose at most {MAX_PDF_PAGES} pages per request.")
        texts: dict[int, tuple[str, str]] = {}  # page -> (text, origin)
        scanned: list[int] = []
        for number in pages:
            try:
                text = (reader.pages[number - 1].extract_text() or "").strip()
            except Exception:
                text = ""
            if len(text) >= SCANNED_MIN_CHARS:
                texts[number] = (text, "pdf_text")
            else:
                scanned.append(number)
        notes: list[str] = []
        if scanned:
            if not ocr_available():
                if not texts:
                    raise HTTPException(422, OCR_MISSING_MESSAGE)
                notes.append(f"Pages {scanned} have no selectable text and OCR is not installed; "
                             "they were skipped.")
            else:
                lang_hint = body.source_language if body.source_language != "auto" else "en"
                for number in scanned:
                    text = ocr_page(path, number, lang_hint).strip()
                    if text:
                        texts[number] = (text, "ocr")
                    else:
                        notes.append(f"Page {number}: OCR found no text.")
        if not texts:
            raise HTTPException(422, "No text could be read from the chosen pages.")
        spend(conn, user["id"], sum(len(t) for t, _ in texts.values()))
        first = texts[min(texts)][0]
        source = resolve_source(body.source_language, first)
        out_pages = []
        for number in sorted(texts):
            text, origin = texts[number]
            res = run_translation(conn, user["id"], text, source, body.target_language, origin)
            out_pages.append({"page": number, **res})
        return {"paper": {"source": body.source, "id": body.id}, "source_language": source,
                "target_language": body.target_language, "engine": translator.name,
                "label": MACHINE_LABEL, "machine_translated": True, "evidence_quote": False,
                "pages": out_pages, "notes": notes}

    @app.get("/api/translate/glossary")
    def get_glossary(user: User, conn: Conn) -> list[dict]:
        return list_glossary(conn, user["id"])

    @app.put("/api/translate/glossary")
    def put_glossary(body: GlossaryTerm, user: User, conn: Conn) -> list[dict]:
        try:
            save_glossary_term(conn, user["id"], body.source_language, body.target_language,
                               body.source_term, body.target_term)
        except TranslateError as exc:
            raise translator_error(exc) from exc
        return list_glossary(conn, user["id"])

    @app.delete("/api/translate/glossary")
    def remove_glossary(source_language: str, target_language: str, source_term: str,
                        user: User, conn: Conn) -> list[dict]:
        if not delete_glossary_term(conn, user["id"], source_language, target_language,
                                    source_term):
            raise HTTPException(404, "Glossary term not found")
        return list_glossary(conn, user["id"])

    # ---- Planner agent (M3): area text -> a draft profile the user edits before searching ----

    def owned_plan(conn: sqlite3.Connection, plan_id: int, user_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM plans WHERE id = ? AND user_id = ?",
                           (plan_id, user_id)).fetchone()
        if not row:
            raise HTTPException(404, "Plan not found")
        return row

    @app.get("/api/planner")
    def planner_status(user: User) -> dict:
        return {"available": planner is not None,
                "model": planner.model if planner else None, "note": PLANNER_NOTE,
                "per_hour": settings.plans_per_hour}

    @app.post("/api/plans", status_code=201)
    def create_plan(body: PlanRequest, user: User, conn: Conn) -> dict:
        if planner is None:
            raise HTTPException(503, "The planner is not configured: ANTHROPIC_API_KEY is not "
                                     "set on the server.")
        recent = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE user_id = ? AND cached = 0"
            " AND created_at > datetime('now', '-1 hour')", (user["id"],)).fetchone()[0]
        if recent >= settings.plans_per_hour:
            raise HTTPException(429, f"Planning limit reached: {settings.plans_per_hour} plans "
                                     "per hour. Try again later.")
        try:
            plan_id = make_plan(conn, planner, topic_lookup, user["id"], body)
        except ReaderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return plan_record(owned_plan(conn, plan_id, user["id"]))

    @app.get("/api/plans")
    def list_plans(user: User, conn: Conn) -> list[dict]:
        return [{"id": r["id"], "area": r["area"], "edited": bool(r["edited"]),
                 "created_at": r["created_at"]}
                for r in conn.execute("SELECT id, area, edited, created_at FROM plans"
                                      " WHERE user_id = ? ORDER BY id DESC LIMIT 20",
                                      (user["id"],))]

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: int, user: User, conn: Conn) -> dict:
        return plan_record(owned_plan(conn, plan_id, user["id"]))

    @app.put("/api/plans/{plan_id}")
    def edit_plan(plan_id: int, body: Plan, user: User, conn: Conn) -> dict:
        row = owned_plan(conn, plan_id, user["id"])
        save_edit(conn, row, body)
        return plan_record(owned_plan(conn, plan_id, user["id"]))

    @app.get("/api/plans/{plan_id}/profile")
    def plan_profile(plan_id: int, user: User, conn: Conn, track: int | None = None) -> dict:
        """The plan as a search profile, ready for POST /api/search (main query or a track)."""
        row = owned_plan(conn, plan_id, user["id"])
        request = json.loads(row["request"])
        years = {k: request[k] for k in ("year_from", "year_to") if request.get(k)}
        try:
            profile = Plan.model_validate_json(row["plan"]).to_profile(track, **years)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return profile.model_dump()

    # ---- Reader agent (M8): page-cited evidence, verified quotes only ----

    READER_NOTE = ("The paper text is sent to the Anthropic API for analysis. Only quotes found "
                   "on the cited page are shown as evidence; statements are machine summaries.")

    @app.post("/api/reader", status_code=202)
    def start_reading(body: ReadRequest, background: BackgroundTasks, user: User,
                      conn: Conn) -> dict:
        if reader is None:
            raise HTTPException(503, "The reader is not configured: ANTHROPIC_API_KEY is not set "
                                     "on the server.")
        run = owned_run(conn, body.run_id, user["id"])
        in_run = {(p["source"], p["id"]) for p in run_papers(conn, run["id"], {})}
        wanted = list(dict.fromkeys((r.source, r.id) for r in body.papers))
        if any(k not in in_run for k in wanted):
            raise HTTPException(404, "One or more papers are not in this run")
        recent = conn.execute(
            "SELECT COUNT(*) FROM reader_attempts WHERE user_id = ?"
            " AND created_at > datetime('now', '-1 hour')", (user["id"],)).fetchone()[0]
        if recent + len(wanted) > settings.readings_per_hour:
            raise HTTPException(
                429, f"Reading limit reached: {settings.readings_per_hour} papers per hour "
                f"({max(0, settings.readings_per_hour - recent)} left). Try again later.")
        conn.executemany("INSERT INTO reader_attempts(user_id) VALUES(?)",
                         [(user["id"],)] * len(wanted))
        cur = conn.execute(
            "INSERT INTO jobs(kind, payload, status) VALUES('reader', ?, 'pending')",
            (json.dumps({"user_id": user["id"], "run_id": run["id"], "papers": wanted}),))
        conn.commit()
        background.add_task(run_reader_job, settings.database_path, cur.lastrowid, reader)
        return {"job_id": cur.lastrowid, "queued": [{"source": s, "id": i} for s, i in wanted],
                "model": reader.model, "note": READER_NOTE}

    def reading_rows(conn: sqlite3.Connection, user_id: int,
                     keys: list[tuple[str, str]]) -> list[dict]:
        out = []
        for source, paper_id in keys:
            row = conn.execute("SELECT * FROM reader_results WHERE user_id = ? AND source = ?"
                               " AND paper_id = ?", (user_id, source, paper_id)).fetchone()
            out.append(result_record(row, source, paper_id))
        return out

    @app.get("/api/reader/jobs/{job_id}")
    def reader_job(job_id: int, user: User, conn: Conn) -> dict:
        job = conn.execute("SELECT id, kind, payload, status, error FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
        payload = json.loads(job["payload"]) if job and job["kind"] == "reader" else {}
        if not payload or payload.get("user_id") != user["id"]:
            raise HTTPException(404, "Job not found")
        papers = reading_rows(conn, user["id"], [tuple(p) for p in payload["papers"]])
        return {"id": job["id"], "status": job["status"], "error": job["error"],
                "total": len(papers), "papers": papers}

    @app.get("/api/evidence")
    def get_evidence(run_id: int, user: User, conn: Conn,
                     include_rejected: bool = False) -> dict:
        """Verified evidence for the papers of a run that have been read, with the outcome of
        every dimension. Rejected items appear only with include_rejected=true."""
        run = owned_run(conn, run_id, user["id"])
        keys = [(p["source"], p["id"]) for p in run_papers(conn, run["id"], {})]
        papers = []
        for record in reading_rows(conn, user["id"], keys):
            if record["status"] == "not_requested":
                continue
            record["evidence"] = evidence_rows(conn, user["id"], record["source"],
                                               record["id"], include_rejected)
            papers.append(record)
        return {"run_id": run["id"], "dimensions": DIMENSION_LABELS, "note": READER_NOTE,
                "papers": papers}

    # ---- Concept mapper (M17): verified concept mentions, merged across papers and languages ----

    CONCEPT_NOTE = ("The paper text is sent to the Anthropic API. A concept is shown only with "
                    "quotes found on the cited page that contain its label. Merges by English "
                    "name, by the model or by you are listed on each node; the original labels "
                    "are kept.")

    def concept_service() -> ReaderClient:
        if concept_client is None:
            raise HTTPException(503, "The concept mapper is not configured: ANTHROPIC_API_KEY is "
                                     "not set on the server.")
        return concept_client

    def concept_attempts(conn: sqlite3.Connection, user_id: int, kind: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM concept_attempts WHERE user_id = ? AND kind = ?"
            " AND created_at > datetime('now', '-1 hour')", (user_id, kind)).fetchone()[0]

    def run_keys(conn: sqlite3.Connection, run_id: int) -> list[tuple[str, str]]:
        return [(p["source"], p["id"]) for p in run_papers(conn, run_id, {})]

    def concept_rows(conn: sqlite3.Connection, user_id: int,
                     keys: list[tuple[str, str]]) -> list[dict]:
        out = []
        for source, paper_id in keys:
            row = conn.execute("SELECT * FROM concept_results WHERE user_id = ? AND source = ?"
                               " AND paper_id = ?", (user_id, source, paper_id)).fetchone()
            out.append(concept_record(row, source, paper_id))
        return out

    def done_keys(conn: sqlite3.Connection, user_id: int, run_id: int) -> list[tuple[str, str]]:
        return [(r["source"], r["id"]) for r in concept_rows(conn, user_id, run_keys(conn, run_id))
                if r["status"] == "done"]

    @app.post("/api/concepts", status_code=202)
    def start_concepts(body: ReadRequest, background: BackgroundTasks, user: User,
                       conn: Conn) -> dict:
        client = concept_service()
        run = owned_run(conn, body.run_id, user["id"])
        in_run = set(run_keys(conn, run["id"]))
        wanted = list(dict.fromkeys((r.source, r.id) for r in body.papers))
        if any(k not in in_run for k in wanted):
            raise HTTPException(404, "One or more papers are not in this run")
        recent = concept_attempts(conn, user["id"], "extract")
        limit = settings.concept_readings_per_hour
        if recent + len(wanted) > limit:
            raise HTTPException(429, f"Concept extraction limit reached: {limit} papers per hour "
                                     f"({max(0, limit - recent)} left). Try again later.")
        conn.executemany("INSERT INTO concept_attempts(user_id, kind) VALUES(?, 'extract')",
                         [(user["id"],)] * len(wanted))
        cur = conn.execute(
            "INSERT INTO jobs(kind, payload, status) VALUES('concepts', ?, 'pending')",
            (json.dumps({"user_id": user["id"], "run_id": run["id"], "papers": wanted}),))
        conn.commit()
        background.add_task(run_concept_job, settings.database_path, cur.lastrowid, client)
        return {"job_id": cur.lastrowid, "queued": [{"source": s, "id": i} for s, i in wanted],
                "model": client.model, "note": CONCEPT_NOTE}

    @app.get("/api/concepts/jobs/{job_id}")
    def concept_job(job_id: int, user: User, conn: Conn) -> dict:
        job = conn.execute("SELECT id, kind, payload, status, error FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
        payload = json.loads(job["payload"]) if job and job["kind"] == "concepts" else {}
        if not payload or payload.get("user_id") != user["id"]:
            raise HTTPException(404, "Job not found")
        papers = concept_rows(conn, user["id"], [tuple(p) for p in payload["papers"]])
        return {"id": job["id"], "status": job["status"], "error": job["error"],
                "total": len(papers), "papers": papers}

    @app.get("/api/concepts")
    def get_concepts(run_id: int, user: User, conn: Conn) -> dict:
        """The run's concept nodes from the papers whose extraction is done. Only verified
        mentions are included; every node lists its papers, pages, quotes and labels."""
        run = owned_run(conn, run_id, user["id"])
        papers = [r for r in concept_rows(conn, user["id"], run_keys(conn, run["id"]))
                  if r["status"] != "not_requested"]
        done = [(r["source"], r["id"]) for r in papers if r["status"] == "done"]
        nodes = build_nodes(conn, user["id"], run["id"], done)
        return {"run_id": run["id"], "types": {t: TYPE_LABELS[t] for t in CONCEPT_TYPES},
                "note": CONCEPT_NOTE, "papers": papers, "nodes": nodes}

    @app.post("/api/concepts/normalize")
    def normalize_concepts(body: NormalizeRequest, user: User, conn: Conn) -> dict:
        client = concept_service()
        run = owned_run(conn, body.run_id, user["id"])
        if concept_attempts(conn, user["id"], "normalize") >= settings.concept_merges_per_hour:
            raise HTTPException(429, f"Merge limit reached: {settings.concept_merges_per_hour} "
                                     "synonym passes per hour. Try again later.")
        conn.execute("INSERT INTO concept_attempts(user_id, kind) VALUES(?, 'normalize')",
                     (user["id"],))
        conn.commit()
        try:
            result = normalize(conn, client, user["id"], run["id"],
                               done_keys(conn, user["id"], run["id"]), body.fresh)
        except ReaderError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {**result, "model": client.model}

    @app.post("/api/concepts/merge")
    def merge_concepts(body: ConceptMergeRequest, user: User, conn: Conn) -> dict:
        run = owned_run(conn, body.run_id, user["id"])
        nodes = build_nodes(conn, user["id"], run["id"], done_keys(conn, user["id"], run["id"]))
        try:
            key = merge_nodes(conn, user["id"], run["id"], nodes, body.keys)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"key": key}

    @app.post("/api/concepts/split")
    def split_concept(body: ConceptSplitRequest, user: User, conn: Conn) -> dict:
        run = owned_run(conn, body.run_id, user["id"])
        nodes = build_nodes(conn, user["id"], run["id"], done_keys(conn, user["id"], run["id"]))
        try:
            split_member(conn, user["id"], run["id"], nodes, body.key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"key": body.key}

    return app


app = create_app()
