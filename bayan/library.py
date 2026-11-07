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
from pathlib import Path
from typing import Protocol

from .connectors.base import ConnectorError
from .connectors.openalex import LOOKUP_BATCH
from .db import connect, now_iso
from .downloads import claim_job, set_job
from .models import USER_SOURCE, Paper
from .search import store_references

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
