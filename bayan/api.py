"""FastAPI app: accounts, library/language reference data, search runs and a small web UI."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
from .export import to_csv, to_xlsx
from .libraries import LIBRARIES
from .models import CATEGORY_INFO, LANGUAGES, normalize_document_types
from .search import SearchRequest, paper_row_to_dict, run_search

COOKIE = "bayan_session"
STATIC = Path(__file__).parent / "static"

ConnectorFactory = Callable[[str], Connector]


class Credentials(BaseModel):
    email: str
    password: str


class PaperRef(BaseModel):
    source: str
    id: str


class ExportRequest(BaseModel):
    format: Literal["csv", "xlsx"] = "csv"
    # None exports every paper of the run; otherwise only these (they must belong to the run).
    papers: list[PaperRef] | None = Field(default=None, max_length=500)


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


def create_app(
    settings: Settings | None = None, connector_factory: ConnectorFactory | None = None
) -> FastAPI:
    settings = settings or load_settings()
    make_connector = connector_factory or default_connector_factory(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(settings.database_path)
        yield

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
        out = []
        for p in rows:
            d = paper_row_to_dict(p)
            match = matches.get((d["source"], d["id"]))
            d["doi_source"] = "crossref_lookup" if match else ""
            d["doi_match_score"] = match["score"] if match else None
            d["category_label"] = CATEGORY_LABELS.get(d["category"], d["category"])
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
        return {"id": run["id"], "created_at": run["created_at"],
                "request": request, "summary": summary, "papers": out}

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
        else:
            content, media = to_csv(papers, CATEGORY_LABELS), "text/csv; charset=utf-8"
        name = f"bayan-run-{run_id}.{body.format}"
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

    return app


app = create_app()
