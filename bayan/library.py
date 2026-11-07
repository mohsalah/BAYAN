"""The user's local library: the papers they reached through their own runs and manuals.

`papers` is shared by all users, so every local query joins through `library_keys` and never
reads another user's runs. This module also fills in the citation network of library papers
stored before references were kept (the backfill job); it looks works up by OpenAlex ID or
DOI, never by text search.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from .connectors.base import ConnectorError
from .connectors.openalex import LOOKUP_BATCH
from .db import connect, now_iso
from .downloads import claim_job, set_job
from .models import (
    MANUAL_KIND,
    QUERY_MAX_LENGTH,
    UNKNOWN_LANGUAGE,
    USER_SOURCE,
    Paper,
    SearchProfile,
)
from .retrieval import doi_key
from .search import store_references
from .textnorm import fts_query, tokenize

log = logging.getLogger(__name__)

BACKFILL_KIND = "library_backfill"
MAX_BACKFILL_REQUESTS = 200
# A DOI that can go into an OpenAlex OR filter: `,` and `|` would split it.
USABLE_DOI = re.compile(r"10\.\d{4,9}/[^\s,|]+")


def library_keys() -> str:
    """SQL (named parameter `:user`) for the (source, paper_id) pairs in the user's library."""
    return (
        "SELECT rp.source AS source, rp.paper_id AS paper_id FROM run_papers rp"
        " JOIN search_runs r ON r.id = rp.run_id WHERE r.user_id = :user"
        f" UNION SELECT '{USER_SOURCE}', m.paper_id FROM manuals m WHERE m.user_id = :user"
        " UNION SELECT cp.source, cp.paper_id FROM collection_papers cp"
        " JOIN collections c ON c.id = cp.collection_id WHERE c.user_id = :user"
    )


class WorkLookup(Protocol):
    def lookup(self, filter_: str) -> list[Paper]: ...


Groups = dict[str, list[tuple[str, str]]]


# papers.openalex_status: NULL = not looked up yet; RETRY_DOI = its W-id was not found (merged or
# withdrawn), so it is looked up once more by DOI; NOT_FOUND = OpenAlex has no record of it;
# NO_LIST = the record came without a reference list (citations stored, references unknown).
RETRY_DOI, NOT_FOUND, NO_LIST = "retry_doi", "not_found", "no_reference_list"


def backfill_candidates(conn: sqlite3.Connection, user_id: int) -> tuple[Groups, Groups, int]:
    """Library papers whose references were never stored and that are still worth looking up,
    grouped by W-id and (when there is none, or it was not found) by DOI. Also returns how many
    have neither, so they cannot be looked up."""
    rows = conn.execute(
        "SELECT p.source, p.id, p.doi, p.openalex_id, p.openalex_status FROM papers p"
        f" JOIN ({library_keys()}) k ON k.source = p.source AND k.paper_id = p.id"
        " WHERE p.references_count IS NULL AND p.source != :manual"
        " AND (p.openalex_status IS NULL OR p.openalex_status = :retry)"
        " ORDER BY p.source, p.id",
        {"user": user_id, "manual": USER_SOURCE, "retry": RETRY_DOI}).fetchall()
    by_id: Groups = {}
    by_doi: Groups = {}
    no_id = 0
    for r in rows:
        wid = r["openalex_id"] or (r["id"] if r["source"] == "openalex" else "")
        doi = (r["doi"] or "").lower()
        if wid and r["openalex_status"] is None:
            by_id.setdefault(wid, []).append((r["source"], r["id"]))
        elif USABLE_DOI.fullmatch(doi):
            by_doi.setdefault(doi, []).append((r["source"], r["id"]))
        else:
            no_id += 1
    return by_id, by_doi, no_id


def apply_work(conn: sqlite3.Connection, work: Paper, rows: list[tuple[str, str]]) -> None:
    """Write an OpenAlex record's network data onto the user's stored rows of that work.

    A record without a reference list leaves stored references alone (it is not "none")."""
    refs = work.referenced_works
    counts = json.dumps(work.counts_by_year) if work.counts_by_year is not None else None
    now = now_iso()
    for source, paper_id in rows:
        conn.execute(
            "UPDATE papers SET openalex_id = ?, counts_by_year = COALESCE(?, counts_by_year),"
            " citations = ?, citations_updated_at = ?,"
            " references_count = COALESCE(?, references_count), openalex_status = ?"
            " WHERE source = ? AND id = ?",
            (work.openalex_id, counts, work.citations, now,
             len(refs) if refs is not None else None, None if refs is not None else NO_LIST,
             source, paper_id))
    if refs is not None:
        store_references(conn, work.openalex_id, refs)


def new_backfill_payload(user_id: int, max_requests: int) -> dict:
    return {"user_id": user_id, "max_requests": max_requests, "requests_made": 0,
            "updated": 0, "not_found": 0}


def _save(conn: sqlite3.Connection, job_id: int, payload: dict) -> None:
    conn.execute("UPDATE jobs SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (json.dumps(payload), job_id))
    conn.commit()


def _lookup(conn: sqlite3.Connection, connector: WorkLookup, kind: str, groups: Groups,
            payload: dict, retry: Groups) -> None:
    """One request for up to LOOKUP_BATCH W-ids or DOIs, and its answers saved."""
    key_of = (lambda w: w.openalex_id) if kind == "openalex" else (lambda w: w.doi.lower())
    found: dict[str, Paper] = {}
    for work in connector.lookup(f"{kind}:" + "|".join(groups)):
        found.setdefault(key_of(work), work)
    for key, rows in groups.items():
        if key in found:
            apply_work(conn, found[key], rows)
            payload["updated"] += len(rows)
            continue
        if kind == "openalex":
            # A W-id OpenAlex no longer lists (merged into another work): try the DOI.
            dois = {r: d for r in rows if USABLE_DOI.fullmatch(d := _doi(conn, r))}
            for row, doi in dois.items():
                retry.setdefault(doi, []).append(row)
            conn.executemany("UPDATE papers SET openalex_status = ? WHERE source = ? AND id = ?",
                             [(RETRY_DOI, *r) for r in dois])
            rows = [r for r in rows if r not in dois]
        conn.executemany("UPDATE papers SET openalex_status = ? WHERE source = ? AND id = ?",
                         [(NOT_FOUND, *r) for r in rows])
        payload["not_found"] += len(rows)


def _doi(conn: sqlite3.Connection, row: tuple[str, str]) -> str:
    found = conn.execute("SELECT doi FROM papers WHERE source = ? AND id = ?", row).fetchone()
    return ((found["doi"] if found else "") or "").lower()


def run_backfill_job(db_path: Path, job_id: int, connector: WorkLookup,
                     claimed: bool = False) -> None:
    """Background job body. Resumable: each request is counted before it is sent and each
    answer is saved at once, so a restarted job carries on and never exceeds its budget.
    The papers to look up are listed once; W-ids go first, then DOIs."""
    conn = connect(db_path)
    try:
        if not claimed and not claim_job(conn, job_id, kind=BACKFILL_KIND):
            return
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (job_id,)).fetchone()["payload"])
        by_id, by_doi, _ = backfill_candidates(conn, payload["user_id"])
        ids = list(by_id)
        while payload["requests_made"] < payload["max_requests"] and (ids or by_doi):
            if ids:
                kind, keys, source = "openalex", ids[:LOOKUP_BATCH], by_id
                ids = ids[LOOKUP_BATCH:]
            else:
                kind, keys, source = "doi", list(by_doi)[:LOOKUP_BATCH], by_doi
            groups = {k: source.pop(k) for k in keys}
            payload["requests_made"] += 1
            _save(conn, job_id, payload)
            _lookup(conn, connector, kind, groups, payload, by_doi)
            _save(conn, job_id, payload)
        set_job(conn, job_id, "done")
    except ConnectorError as exc:
        log.warning("backfill job %s stopped: %s", job_id, exc)
        set_job(conn, job_id, "failed", str(exc))
    except Exception as exc:
        log.warning("backfill job %s failed: %s", job_id, type(exc).__name__)
        set_job(conn, job_id, "failed", type(exc).__name__)
    finally:
        conn.close()


def backfill_view(conn: sqlite3.Connection, job: sqlite3.Row | None, user_id: int) -> dict:
    """A job's counters plus what is still left to look up (counted now)."""
    by_id, by_doi, no_id = backfill_candidates(conn, user_id)
    payload = json.loads(job["payload"]) if job else new_backfill_payload(user_id, 0)
    return {
        "job_id": job["id"] if job else None,
        "status": job["status"] if job else "nothing_to_do",
        "error": job["error"] if job else None,
        "max_requests": payload["max_requests"],
        "requests_made": payload["requests_made"],
        "updated": payload["updated"],
        "not_found": payload["not_found"],
        "remaining": sum(len(v) for v in by_id.values()) + sum(len(v) for v in by_doi.values()),
        "no_identifier": no_id,
    }


def open_backfill(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """The user's backfill job that is still pending or running, if any."""
    return conn.execute(
        "SELECT id, payload, status, error FROM jobs WHERE kind = ? AND status IN"
        " ('pending', 'running') AND json_extract(payload, '$.user_id') = ? ORDER BY id DESC",
        (BACKFILL_KIND, user_id)).fetchone()


def resume_stale_backfills(db_path: Path, connector: Callable[[], WorkLookup],
                           stale_seconds: int) -> list[int]:
    """Continue backfill jobs interrupted by a restart or crash. The API runs this in its own
    thread, so a long backfill never delays the download-job checks."""
    with closing(connect(db_path)) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND status IN ('pending', 'running')"
            " AND COALESCE(updated_at, created_at) <= datetime('now', ?) ORDER BY id",
            (BACKFILL_KIND, f"-{int(stale_seconds)} seconds"))]
    resumed = []
    for job_id in ids:
        try:
            lookup = connector()  # before taking the job, so a failure cannot strand it
        except Exception as exc:
            log.warning("backfill job %s not resumed: %s", job_id, type(exc).__name__)
            return resumed
        with closing(connect(db_path)) as conn:
            if not claim_job(conn, job_id, stale_seconds, kind=BACKFILL_KIND):
                continue
        run_backfill_job(db_path, job_id, lookup, claimed=True)
        resumed.append(job_id)
    return resumed


def stale_cutoff(max_age_days: int) -> str:
    """Citation counts written before this time (now_iso format) are stale (M32)."""
    return (datetime.now(UTC) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def citations_stale(paper: sqlite3.Row | dict, cutoff: str) -> bool:
    """True when the citation count was never checked or was checked before `cutoff`. Papers
    you added yourself carry no count to refresh and are never stale."""
    if paper["source"] == USER_SOURCE:
        return False
    updated = paper["citations_updated_at"]
    return not updated or updated < cutoff


# ---------------------------------------------------------------------------------------------
# M30: search the local library first.

LOCAL_SORTS = ("relevance", "citations", "velocity", "local_citations")
COVERAGE_NOTE = ("Local library only: papers you have already searched. Run a remote search "
                 "before treating anything as new or missing.")
FETCHED_NOTE = (" {n} of these papers were fetched by ID as foundations of a collection (cited"
                " by its papers), not found by one of your searches.")
_QTOKEN = re.compile(r'"([^"]*)"|(\()|(\))|([^\s()"]+)')
_FTS_OPERATORS = {"AND": "AND", "OR": "OR", "NOT": "NOT"}


def _fts_term(text: str) -> str | None:
    """One user term or phrase as an FTS5 phrase of normalised tokens, or None if it has none."""
    tokens = tokenize(text)
    if not tokens:
        return None
    return '"' + " ".join(t.replace('"', '""') for t in tokens) + '"'


def library_match(text: str) -> str:
    """The user's words as an FTS5 MATCH expression, normalised like the stored text.

    Supports AND/OR/NOT, parentheses and "quoted phrases"; adjacent terms are joined with AND.
    Where a term is due, AND/OR/NOT is read as a word (as in "Operations Research (OR)").
    Anything that is not a well-formed query falls back to "every word" (fts_query). Returns
    "" when there are no words.
    """
    out: list[str] = []
    depth, expect_term = 0, True
    for phrase, opening, closer, word in _QTOKEN.findall(text or ""):
        if word in _FTS_OPERATORS and not expect_term:
            out.append(_FTS_OPERATORS[word])
            expect_term = True
            continue
        if word == "NOT" and out and out[-1] == "AND":
            # "a AND NOT b": FTS5's NOT already means "and not".
            out[-1] = "NOT"
            continue
        if word in ("AND", "OR") and out and out[-1] in ("AND", "OR", "NOT"):
            return fts_query(text)  # two operators in a row: not well formed
        if closer:
            depth -= 1
            if depth < 0 or expect_term:
                return fts_query(text)
            out.append(")")
            continue
        if opening:
            if not expect_term:
                out.append("AND")
            depth += 1
            out.append("(")
            continue
        term = _fts_term(phrase or word)
        if term is None:
            continue
        if not expect_term:
            out.append("AND")
        out.append(term)
        expect_term = False
    if depth or expect_term or not out:
        return fts_query(text)
    return " ".join(out)


class LibraryQuery(BaseModel):
    """Filters and order of a local-library search (the same for runs and collections)."""

    q: str = Field(default="", max_length=QUERY_MAX_LENGTH)
    year_from: int | None = Field(default=None, ge=1500, le=2100)
    year_to: int | None = Field(default=None, ge=1500, le=2100)
    # Reference types (the five categories) and "manual".
    types: list[str] = Field(default_factory=list)
    # Language codes as stored; "unknown" = no language reported.
    languages: list[str] = Field(default_factory=list)
    sort: Literal["relevance", "citations", "velocity", "local_citations"] = "relevance"
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _years_in_order(self) -> "LibraryQuery":
        if (self.year_from is not None and self.year_to is not None
                and self.year_from > self.year_to):
            raise ValueError("year_from must not be after year_to")
        return self


class ExportQuery(LibraryQuery):
    """A library query for an export, which may list a whole collection."""

    limit: int = Field(default=5000, ge=1, le=5000)


def current_year() -> int:
    return date.today().year


def _where(query: LibraryQuery, params: dict) -> list[str]:
    where = []
    if query.year_from is not None:
        where.append("p.year >= :year_from")
        params["year_from"] = query.year_from
    if query.year_to is not None:
        where.append("p.year <= :year_to")
        params["year_to"] = query.year_to
    if query.types:
        cats = [t for t in query.types if t != MANUAL_KIND]
        parts = []
        if cats:
            names = ",".join(f":type{i}" for i in range(len(cats)))
            parts.append(f"(COALESCE(p.category, 'other') IN ({names}) AND"
                         " COALESCE(p.document_kind, '') != :manual_kind)")
            params.update({f"type{i}": c for i, c in enumerate(cats)})
        if MANUAL_KIND in query.types:
            parts.append("p.document_kind = :manual_kind")
        params["manual_kind"] = MANUAL_KIND
        where.append("(" + " OR ".join(parts) + ")")
    if query.languages:
        codes = [c for c in query.languages if c != UNKNOWN_LANGUAGE]
        parts = []
        if codes:
            names = ",".join(f":lang{i}" for i in range(len(codes)))
            parts.append(f"LOWER(COALESCE(p.language, '')) IN ({names})")
            params.update({f"lang{i}": c.lower() for i, c in enumerate(codes)})
        if UNKNOWN_LANGUAGE in query.languages:
            parts.append("COALESCE(p.language, '') = ''")
        where.append("(" + " OR ".join(parts) + ")")
    return where


ORDER = {
    # Text match first (bm25: lower is better); without words, newest first.
    "relevance": "{rank}, p.year DESC, p.title",
    "citations": "COALESCE(p.citations, 0) DESC, p.year DESC, p.title",
    "velocity": "velocity IS NULL, velocity DESC, p.title",
    "local_citations": "local_citations DESC, COALESCE(p.citations, 0) DESC, p.title",
}


def search_library(conn: sqlite3.Connection, user_id: int, query: LibraryQuery,
                   keys_sql: str | None = None) -> dict:
    """Papers of the user's library matching `query`, with total, counts per year, velocity
    and local citations. `keys_sql` narrows the set (a collection); it must use `:user`."""
    params: dict = {"user": user_id, "year_now": current_year()}
    match = library_match(query.q)
    keys = keys_sql or library_keys()
    base = (f"WITH lib AS (SELECT DISTINCT source, paper_id FROM ({keys})),"
            " libw AS (SELECT DISTINCT p.openalex_id AS w FROM papers p JOIN lib"
            " ON lib.source = p.source AND lib.paper_id = p.id"
            " WHERE p.openalex_id IS NOT NULL AND p.openalex_id != '')")
    if match:
        frm = ("FROM paper_search JOIN papers p ON p.source = paper_search.source"
               " AND p.id = paper_search.id JOIN lib ON lib.source = p.source"
               " AND lib.paper_id = p.id")
        where = ["paper_search MATCH :match"]
        params["match"] = match
        rank = "bm25(paper_search)"
    else:
        frm = "FROM papers p JOIN lib ON lib.source = p.source AND lib.paper_id = p.id"
        where, rank = [], "NULL"  # a constant: newest first decides
    where += _where(query, params)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    velocity = ("CASE WHEN p.year IS NULL THEN NULL ELSE COALESCE(p.citations, 0) * 1.0 /"
                " MAX(1, :year_now - p.year + 1) END")
    local = ("(SELECT COUNT(DISTINCT pr.citing) FROM paper_references pr WHERE"
             " pr.cited = p.openalex_id AND pr.citing != p.openalex_id"
             " AND pr.citing IN (SELECT w FROM libw))")
    order = ORDER[query.sort].format(rank=rank)
    params.update(limit=query.limit, offset=query.offset)
    rows = conn.execute(
        f"{base} SELECT p.*, {velocity} AS velocity, {local} AS local_citations {frm}{cond}"
        f" ORDER BY {order} LIMIT :limit OFFSET :offset", params).fetchall()
    total = conn.execute(f"{base} SELECT COUNT(*) {frm}{cond}", params).fetchone()[0]
    by_year = {str(r[0]) if r[0] is not None else "unknown": r[1] for r in conn.execute(
        f"{base} SELECT p.year, COUNT(*) {frm}{cond} GROUP BY p.year ORDER BY p.year", params)}
    return {"rows": rows, "total": total, "by_year": by_year, "match": match,
            "coverage": coverage(conn, user_id, keys)}


def coverage(conn: sqlite3.Connection, user_id: int, keys_sql: str | None = None) -> dict:
    """What a local answer is based on. Every local response carries it. Stored run summaries
    keep its run list as a count and the latest ID (compact_runs), so they stay small."""
    keys = keys_sql or library_keys()
    row = conn.execute(
        f"SELECT COUNT(*), MIN(p.year), MAX(p.year) FROM (SELECT DISTINCT source, paper_id"
        f" FROM ({keys})) k JOIN papers p ON p.source = k.source AND p.id = k.paper_id",
        {"user": user_id}).fetchone()
    libraries = [r[0] for r in conn.execute(
        f"SELECT DISTINCT source FROM ({keys}) ORDER BY source", {"user": user_id})]
    runs = conn.execute("SELECT id, created_at FROM search_runs WHERE user_id = ?"
                        " AND json_extract(summary, '$.origin') IS NOT 'local_library'"
                        " ORDER BY id", (user_id,)).fetchall()
    # Works fetched by ID (collection foundations) were not found by any search of yours.
    by_id = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT source, paper_id FROM ({keys})) k"
        " WHERE EXISTS (SELECT 1 FROM collection_papers cp JOIN collections c"
        " ON c.id = cp.collection_id WHERE c.user_id = :user AND cp.origin = 'foundation'"
        " AND cp.source = k.source AND cp.paper_id = k.paper_id)"
        " AND NOT EXISTS (SELECT 1 FROM run_papers rp JOIN search_runs r ON r.id = rp.run_id"
        " WHERE r.user_id = :user AND rp.source = k.source AND rp.paper_id = k.paper_id)",
        {"user": user_id}).fetchone()[0]
    return {"scope": "local_library", "papers": row[0], "runs": [r["id"] for r in runs],
            "fetched_by_id": by_id,
            "libraries": libraries, "years": [row[1], row[2]],
            "last_refreshed": runs[-1]["created_at"] if runs else None,
            "note": COVERAGE_NOTE + (FETCHED_NOTE.format(n=by_id) if by_id else "")}


def compact_runs(cov: dict) -> dict:
    """The run list of a coverage object as a count and the latest ID (stored summaries)."""
    runs = cov["runs"]
    if isinstance(runs, dict):
        return runs
    return {"count": len(runs), "latest": runs[-1] if runs else None}


def library_key_set(conn: sqlite3.Connection, user_id: int) -> set[tuple[str, str]]:
    """Every (source, id) in the user's library, and every W-id and DOI in it, as of now."""
    rows = conn.execute(
        f"SELECT p.source, p.id, p.openalex_id, p.doi FROM ({library_keys()}) k"
        " JOIN papers p ON p.source = k.source AND p.id = k.paper_id", {"user": user_id})
    out: set[tuple[str, str]] = set()
    for r in rows:
        out.add((r["source"], r["id"]))
        if r["openalex_id"]:
            out.add(("openalex_id", r["openalex_id"]))
        if doi_key(r["doi"]):
            out.add(("doi", doi_key(r["doi"])))
    return out


def in_library(paper: dict | Paper, keys: set[tuple[str, str]]) -> bool:
    """Whether a paper (any library's record of it) was in the library: by ID, W-id or DOI."""
    get = paper.get if isinstance(paper, dict) else lambda k, d="": getattr(paper, k, d)
    return ((get("source", ""), get("id", "")) in keys
            or bool(get("openalex_id", "")) and ("openalex_id", get("openalex_id", "")) in keys
            or bool(doi_key(get("doi", ""))) and ("doi", doi_key(get("doi", ""))) in keys)


# Profile settings a local search cannot apply (the stored papers carry no topic IDs per search,
# and per-language text is a remote-search setting). Reported, never guessed.
def local_unapplied(profile: SearchProfile) -> list[str]:
    out = []
    if profile.topics:
        out.append("topics")
    if profile.geo_countries:
        out.append("countries")
    if profile.queries_by_language:
        out.append("queries_by_language")
    return out


def query_from_profile(profile: SearchProfile, limit: int) -> LibraryQuery:
    # Search languages choose the text sent to libraries; many libraries cannot filter by
    # language, so the stored papers of such a run have other or no language. The local search
    # therefore matches papers of any language (as the remote run kept them).
    return LibraryQuery(q=profile.query, year_from=profile.year_from, year_to=profile.year_to,
                        types=list(profile.document_types), sort=profile.sort, limit=limit)


def _key(row: sqlite3.Row | dict) -> tuple[str, str]:
    return (row["source"], row["id"])


def save_local_run(conn: sqlite3.Connection, user_id: int, request: dict, query: LibraryQuery,
                   result: dict, unapplied: list[str]) -> dict:
    """Store a local search as a normal run (origin local_library), so downloads, the reader,
    concepts, export and gap packets work on it unchanged."""
    rows = result["rows"]
    summary = {
        "origin": "local_library", "mode": "local", "found": len(rows),
        "total_matches": result["total"], "by_year": result["by_year"], "sort": query.sort,
        "local_query": query.model_dump(), "match": result["match"],
        "unapplied_filters": {"local_library": unapplied},
        "libraries": sorted({r["source"] for r in rows}),
        "local_hits": len(rows), "remote_new": 0, "remote_known": 0,
        "library_before": [list(_key(r)) for r in rows],
        "coverage": {**result["coverage"], "runs": compact_runs(result["coverage"])},
    }
    cur = conn.execute("INSERT INTO search_runs(user_id, request, summary) VALUES(?,?,?)",
                       (user_id, json.dumps(request), json.dumps(summary)))
    conn.executemany("INSERT OR IGNORE INTO run_papers(run_id, source, paper_id, rank)"
                     " VALUES(?,?,?,?)",
                     [(cur.lastrowid, r["source"], r["id"], i) for i, r in enumerate(rows)])
    conn.commit()
    return {"id": cur.lastrowid, "summary": summary}


def put_local_first(conn: sqlite3.Connection, run_id: int, local_rows: list[sqlite3.Row],
                    before: set[tuple[str, str]], coverage_: dict,
                    unapplied: list[str]) -> dict:
    """Reorder a remote run: local hits first, then the remote papers not among them, and
    count the remote papers that were already in the library (by ID, W-id or DOI)."""
    remote = conn.execute(
        "SELECT p.* FROM run_papers rp JOIN papers p ON p.source = rp.source"
        " AND p.id = rp.paper_id WHERE rp.run_id = ? ORDER BY rp.rank", (run_id,)).fetchall()
    local_keys = {_key(r) for r in local_rows}
    rest = [r for r in remote if _key(r) not in local_keys]
    known = [r for r in rest if in_library(dict(r), before)]
    conn.execute("DELETE FROM run_papers WHERE run_id = ?", (run_id,))
    conn.executemany("INSERT INTO run_papers(run_id, source, paper_id, rank) VALUES(?,?,?,?)",
                     [(run_id, r["source"], r["id"], i)
                      for i, r in enumerate([*local_rows, *rest])])
    summary = json.loads(conn.execute("SELECT summary FROM search_runs WHERE id = ?",
                                      (run_id,)).fetchone()["summary"])
    summary.update({
        "mode": "local_then_remote",
        "local_hits": len(local_rows),
        "remote_new": len(rest) - len(known),
        "remote_known": len(known),
        # Remote papers already found locally are counted once, as local hits.
        "remote_also_local": len(remote) - len(rest),
        "library_before": [list(_key(r)) for r in [*local_rows, *known]],
        "found": len(local_rows) + len(rest),
        "coverage": {**coverage_, "runs": compact_runs(coverage_)},
    })
    summary.setdefault("unapplied_filters", {})["local_library"] = unapplied
    conn.execute("UPDATE search_runs SET summary = ? WHERE id = ?", (json.dumps(summary), run_id))
    conn.commit()
    return {"id": run_id, "summary": summary}
