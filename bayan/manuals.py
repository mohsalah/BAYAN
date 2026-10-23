"""User-supplied manuals: added by public URL or PDF upload, never found by searching.

A manual is stored like any paper (source "user", category "other", document kind "manual")
so it flows through selection, export and PDF handling, but it is always labelled
user-supplied and is private to the user who added it (see the `manuals` table).
"""

import re
import sqlite3
import uuid
from urllib.parse import unquote, urlsplit

from .models import MANUAL_KIND, USER_SOURCE, Paper
from .search import _store_paper
from .textnorm import index_text

MAX_TITLE = 300
DEFAULT_TITLE = "Untitled manual"
BASIS = "user-supplied manual; the user gave the title, so identity was not checked"


def clean_title(raw: str, fallback: str = DEFAULT_TITLE) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", raw or "")
    return " ".join(text.split())[:MAX_TITLE] or fallback


def title_from_url(url: str) -> str:
    """A readable default title from the last path segment (query string is never used)."""
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE).replace("_", " ").replace("-", " ")
    return clean_title(name)


def new_manual_id() -> str:
    return "m-" + uuid.uuid4().hex[:16]


def store_manual(conn: sqlite3.Connection, user_id: int, paper_id: str, title: str,
                 origin: str, source_url: str, run_id: int | None) -> None:
    """Create the paper row, the ownership row and, when given, the run membership."""
    _store_paper(conn, Paper(
        source=USER_SOURCE, id=paper_id, title=title, native_type="manual", category="other",
        category_basis=BASIS, document_kind=MANUAL_KIND,
        landing_url=source_url if origin == "url" else ""))
    conn.execute("INSERT INTO manuals(user_id, paper_id, origin, source_url) VALUES(?,?,?,?)",
                 (user_id, paper_id, origin, source_url or None))
    if run_id is not None:
        add_to_run(conn, run_id, paper_id)
    conn.commit()


def add_to_run(conn: sqlite3.Connection, run_id: int, paper_id: str) -> None:
    rank = conn.execute("SELECT COALESCE(MAX(rank), -1) + 1 FROM run_papers WHERE run_id = ?",
                        (run_id,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO run_papers(run_id, source, paper_id, rank)"
                 " VALUES(?,?,?,?)", (run_id, USER_SOURCE, paper_id, rank))


def owns_manual(conn: sqlite3.Connection, user_id: int, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM manuals WHERE user_id = ? AND paper_id = ?",
                        (user_id, paper_id)).fetchone() is not None


def rename_manual(conn: sqlite3.Connection, paper_id: str, title: str) -> None:
    conn.execute("UPDATE papers SET title = ? WHERE source = ? AND id = ?",
                 (title, USER_SOURCE, paper_id))
    conn.execute("UPDATE paper_search SET title = ? WHERE source = ? AND id = ?",
                 (index_text(title), USER_SOURCE, paper_id))
    conn.commit()
