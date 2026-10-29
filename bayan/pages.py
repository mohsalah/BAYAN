"""Page index: the text of every page of a validated PDF, stored per user with FTS5 search.

Built from the file only after it has passed validation (see `downloads.finalize_pdf`), so
the reader agent (M8) can cite real page numbers. `page` is the physical page (1-based);
`label` is the page label printed in the PDF when it defines one (for example "iv" or "213").
The stored text is what pypdf extracted, unchanged; the FTS table holds `index_text(text)`.

A page with no extractable text (a scan) is kept with empty text, never dropped, and the
file is marked `no_text` so it is clear that OCR is needed before it can be read.
"""

import re
import sqlite3
import unicodedata
from pathlib import Path

from pypdf import PdfReader

from .textnorm import fts_query, index_text, normalize_text

MAX_PAGES = 2000
MAX_PAGE_CHARS = 200_000

INDEXED = "indexed"  # at least one page has text
NO_TEXT = "no_text"  # every page is empty: a scan or image-only PDF, OCR needed
FAILED = "failed"  # the text could not be read; the file itself stays valid
INDEX_NOTES = {
    INDEXED: "Page text indexed.",
    NO_TEXT: "No page has extractable text (scanned or image-only PDF); OCR is needed.",
    FAILED: "The page text could not be extracted.",
}


def extract_pages(path: Path) -> list[tuple[str, str]]:
    """[(label, text)] for each page, at most MAX_PAGES. An unreadable page gives ""."""
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        reader.decrypt("")
    try:
        labels = list(reader.page_labels)
    except Exception:  # broken label tree: fall back to physical numbers
        labels = []
    out = []
    for i, page in enumerate(reader.pages[:MAX_PAGES]):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = unicodedata.normalize("NFC", text)[:MAX_PAGE_CHARS]
        label = labels[i] if i < len(labels) else str(i + 1)
        out.append((label, text))
    return out


def clear_pages(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str) -> None:
    key = (user_id, source, paper_id)
    conn.execute("DELETE FROM pages WHERE user_id = ? AND source = ? AND paper_id = ?", key)
    conn.execute("DELETE FROM page_search WHERE user_id = ? AND source = ? AND paper_id = ?", key)


def _set_status(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str,
                count: int | None, with_text: int | None, status: str | None) -> None:
    conn.execute("UPDATE downloads SET page_count = ?, pages_with_text = ?, index_status = ?"
                 " WHERE user_id = ? AND source = ? AND paper_id = ?",
                 (count, with_text, status, user_id, source, paper_id))


def index_pdf(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str,
              path: Path, sha256: str) -> str:
    """(Re)build the page index of one file and record the outcome on its download row.

    Skipped when the same file (same SHA-256) is already indexed. Returns the index status.
    """
    have = conn.execute(
        "SELECT sha256 FROM pages WHERE user_id = ? AND source = ? AND paper_id = ? LIMIT 1",
        (user_id, source, paper_id)).fetchone()
    if have and have["sha256"] == sha256:
        row = conn.execute(
            "SELECT index_status FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
            (user_id, source, paper_id)).fetchone()
        if row and row["index_status"]:
            return row["index_status"]
    clear_pages(conn, user_id, source, paper_id)
    try:
        pages = extract_pages(path)
    except Exception:
        _set_status(conn, user_id, source, paper_id, None, None, FAILED)
        conn.commit()
        return FAILED
    for number, (label, text) in enumerate(pages, 1):
        conn.execute("INSERT INTO pages(user_id, source, paper_id, page, label, text, chars,"
                     " sha256) VALUES(?,?,?,?,?,?,?,?)",
                     (user_id, source, paper_id, number, label, text, len(text), sha256))
        if text.strip():
            conn.execute("INSERT INTO page_search(user_id, source, paper_id, page, body)"
                         " VALUES(?,?,?,?,?)",
                         (user_id, source, paper_id, number, index_text(text)))
    with_text = sum(1 for _, text in pages if text.strip())
    status = INDEXED if with_text else NO_TEXT
    _set_status(conn, user_id, source, paper_id, len(pages), with_text, status)
    conn.commit()
    return status


def page_list(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str) -> list[dict]:
    return [{"page": r["page"], "label": r["label"], "chars": r["chars"]}
            for r in conn.execute(
                "SELECT page, label, chars FROM pages WHERE user_id = ? AND source = ?"
                " AND paper_id = ? ORDER BY page", (user_id, source, paper_id))]


def page_text(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str,
              page: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT page, label, text, chars, sha256 FROM pages WHERE user_id = ? AND source = ?"
        " AND paper_id = ? AND page = ?", (user_id, source, paper_id, page)).fetchone()


def excerpt(text: str, query: str, width: int = 240) -> str:
    """A stretch of the original text around the first query word found (case/accent-blind)."""
    words = [w for w in re.findall(r"\w+", query) if w]
    folded = normalize_text(text)
    at = -1
    if len(folded) == len(text):  # positions line up, so a match in `folded` maps back
        for word in words:
            at = folded.find(normalize_text(word))
            if at >= 0:
                break
    start = max(0, at - width // 3) if at >= 0 else 0
    snippet = " ".join(text[start:start + width].split())
    return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")


def search_pages(conn: sqlite3.Connection, user_id: int, query: str,
                 papers: list[tuple[str, str]] | None = None, limit: int = 20) -> list[dict]:
    """Pages of this user's files matching every word of `query`, best match first."""
    match = fts_query(query)
    if not match:
        return []
    sql = ("SELECT s.source, s.paper_id, s.page, p.label, p.text FROM page_search s"
           " JOIN pages p ON p.user_id = s.user_id AND p.source = s.source"
           " AND p.paper_id = s.paper_id AND p.page = s.page"
           " WHERE page_search MATCH ? AND s.user_id = ?")
    args: list[object] = [match, user_id]
    if papers is not None:
        if not papers:
            return []
        marks = ",".join("?" * len(papers))
        sql += f" AND (s.source || char(31) || s.paper_id) IN ({marks})"
        args += [f"{source}\x1f{paper_id}" for source, paper_id in papers]
    sql += " ORDER BY bm25(page_search) LIMIT ?"
    args.append(limit)
    return [{"source": r["source"], "id": r["paper_id"], "page": int(r["page"]),
             "label": r["label"], "excerpt": excerpt(r["text"], query)}
            for r in conn.execute(sql, args)]
