"""FastAPI app: accounts, library/language reference data, search runs and a small web UI."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
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
from .connectors import ConnectorError, OpenAlexConnector
from .connectors.base import Connector
from .db import connect, init_db
from .libraries import LIBRARIES
from .models import DOCUMENT_TYPES, LANGUAGES
from .search import SearchRequest, paper_row_to_dict, run_search

COOKIE = "bayan_session"
STATIC = Path(__file__).parent / "static"

ConnectorFactory = Callable[[str], Connector]


class Credentials(BaseModel):
    email: str
    password: str


def default_connector_factory(settings: Settings) -> ConnectorFactory:
    def make(library_id: str) -> Connector:
        if library_id == "openalex":
            return OpenAlexConnector(
                api_key=settings.openalex_api_key, contact_email=settings.contact_email
            )
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
    def document_types() -> list[str]:
        return list(DOCUMENT_TYPES)

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
            return run_search(conn, user["id"], body, connectors, settings.search_max_results)
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
        return {"id": run["id"], "created_at": run["created_at"],
                "request": json.loads(run["request"]), "summary": json.loads(run["summary"]),
                "papers": [paper_row_to_dict(p) for p in papers]}

    return app


app = create_app()
