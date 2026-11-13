"""Incremental refresh of a collection (M32): new papers since the last refresh, and fresh
citation counts, without searching the whole track again.

A refresh is a resumable background job (the `jobs` table, kind `collection_refresh`) with a
request budget it never exceeds:

1. New papers: the collection's stored profile is run again on its libraries, restricted to
   records published or created since the last refresh minus OVERLAP_DAYS. Only libraries
   with a date filter take part; the others are reported as skipped. Papers already in the
   collection (same record, W-id or DOI) or removed by the owner are not added; the others
   are added with origin `refresh`. `last_refreshed_at` moves only when this step finished.
2. Citations: collection papers whose count is older than the maximum age get their count,
   yearly counts and references again by OpenAlex ID filter (LOOKUP_BATCH per request).

Each refresh is recorded in `collection_refreshes` with what it really did.
"""

import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from .connectors.base import ConnectorError, RequestBudget, build_connector
from .connectors.openalex import LOOKUP_BATCH
from .db import connect, now_iso
from .downloads import claim_job, set_job
from .library import (
    NO_LIST,
    NOT_FOUND,
    RETRY_DOI,
    USABLE_DOI,
    _lookup,
    current_year,
    in_library,
    library_key_set,
    stale_cutoff,
    work_keys,
)
from .models import USER_SOURCE, Paper, SearchProfile
from .search import SearchRequest, run_search

log = logging.getLogger(__name__)

REFRESH_KIND = "collection_refresh"
OVERLAP_DAYS = 30
MAX_REFRESH_REQUESTS = 200


class SinceSearch:
    """A connector seen through a date filter and a shared request budget, so `run_search`
    applies the profile, filters and de-duplication exactly as for a normal search."""

    def __init__(self, inner, since: str, budget: RequestBudget) -> None:
        self._inner, self._since, self._budget = inner, since, budget
        self.name = inner.name
        self.tier = getattr(inner, "tier", "open")
        self.supported_filters = getattr(inner, "supported_filters", frozenset())
        self.supported_sorts = getattr(inner, "supported_sorts", frozenset({"relevance"}))
        if hasattr(inner, "supported_categories"):
            self.supported_categories = inner.supported_categories

    @property
    def warnings(self) -> list[str]:
        return getattr(self._inner, "warnings", [])

    @property
    def truncated(self) -> bool:
        return bool(getattr(self._inner, "truncated", False))

    def search(self, profile: SearchProfile) -> Iterator[Paper]:
        return self._inner.search_since(profile, self._since, self._budget)


def refresh_since(conn: sqlite3.Connection, collection: sqlite3.Row) -> str:
    """The date (YYYY-MM-DD) new records are looked for from: the last refresh, else the
    newest run the collection was built from (not a refresh run), else its creation; minus
    OVERLAP_DAYS."""
    base = collection["last_refreshed_at"]
    if not base:
        base = conn.execute(
            "SELECT MAX(r.created_at) FROM collection_papers cp JOIN search_runs r"
            " ON r.id = cp.added_from_run WHERE cp.collection_id = ? AND cp.origin = 'run'",
            (collection["id"],)).fetchone()[0] or collection["created_at"]
    start = date.fromisoformat(str(base)[:10]) if base else date.today()
    return (start - timedelta(days=OVERLAP_DAYS)).isoformat()


def new_payload(user_id: int, collection_id: int, refresh_id: int, max_requests: int,
                citations: bool) -> dict:
    return {"user_id": user_id, "collection_id": collection_id, "refresh_id": refresh_id,
            "max_requests": max_requests, "citations": citations, "requests_made": 0,
            "phase": "new_papers", "since": None, "started": None, "run_id": None,
            "new_papers": 0, "already_in_collection": 0, "already_in_library": 0,
            "skipped_removed": 0, "citations_updated": 0, "citations_not_found": 0,
            "shortfalls": [], "skipped": {}}


def queue_refresh(conn: sqlite3.Connection, collection_id: int, user_id: int,
                  max_requests: int, citations: bool, trigger: str = "manual") -> int:
    """Insert the job and its history row (the caller commits). Returns the job id."""
    rid = conn.execute("INSERT INTO collection_refreshes(collection_id, trigger)"
                       " VALUES(?, ?)", (collection_id, trigger)).lastrowid
    job = conn.execute(
        "INSERT INTO jobs(kind, payload, status) VALUES(?, ?, 'pending')",
        (REFRESH_KIND, json.dumps(new_payload(user_id, collection_id, rid, max_requests,
                                              citations)))).lastrowid
    conn.execute("UPDATE collection_refreshes SET job_id = ? WHERE id = ?", (job, rid))
    return job


def open_refresh(conn: sqlite3.Connection, collection_id: int) -> sqlite3.Row | None:
    """The collection's refresh job that is still pending or running, if any."""
    return conn.execute(
        "SELECT id FROM jobs WHERE kind = ? AND status IN ('pending', 'running')"
        " AND json_extract(payload, '$.collection_id') = ? ORDER BY id DESC",
        (REFRESH_KIND, collection_id)).fetchone()


def _save(conn: sqlite3.Connection, job_id: int, payload: dict, status: str | None = None,
          error: str | None = None, finished: bool = False) -> None:
    conn.execute("UPDATE jobs SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (json.dumps(payload), job_id))
    conn.execute(
        "UPDATE collection_refreshes SET since = ?, new_papers = ?, citations_updated = ?,"
        " requests_made = ?, run_id = ?, shortfalls = ?, skipped = ?,"
        " status = COALESCE(?, status), error = COALESCE(?, error),"
        " finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE id = ?",
        (payload["since"], payload["new_papers"], payload["citations_updated"],
         payload["requests_made"], payload["run_id"], json.dumps(payload["shortfalls"]),
         json.dumps(payload["skipped"]), status, error, finished, now_iso(),
         payload["refresh_id"]))
    conn.commit()


def _collection_keys(conn: sqlite3.Connection, collection_id: int) -> set[tuple[str, str]]:
    """Every (source, id), W-id and DOI of the collection's papers (as library_key_set)."""
    out: set[tuple[str, str]] = set()
    for r in conn.execute(
            "SELECT p.source, p.id, p.openalex_id, p.doi FROM collection_papers cp JOIN papers p"
            " ON p.source = cp.source AND p.id = cp.paper_id WHERE cp.collection_id = ?",
            (collection_id,)):
        out |= work_keys(r)
    return out


def _new_papers(conn: sqlite3.Connection, payload: dict, collection: sqlite3.Row,
                budget: RequestBudget, factory: Callable, max_results: int) -> None:
    profile = json.loads(collection["profile"]) if collection["profile"] else None
    libraries = json.loads(collection["libraries"]) if collection["libraries"] else []
    if not profile or not libraries:
        payload["shortfalls"].append({
            "step": "new_papers", "reason": "no_profile",
            "note": "The collection has no stored search profile: add a remote search to it "
                    "first. Nothing was searched."})
        return
    if payload["started"] is None:
        payload["started"] = now_iso()
        payload["since"] = refresh_since(conn, collection)
    connectors, without, unavailable = [], [], {}
    for lib in libraries:
        try:
            # With the collection owner's id: a membership library (Scopus) uses their key.
            inner = build_connector(factory, lib, payload["user_id"])
        except KeyError:
            without.append(lib)
            continue
        except ConnectorError as exc:  # e.g. no saved key: skipped with the reason
            unavailable[lib] = str(exc)
            continue
        if hasattr(inner, "search_since"):
            connectors.append(SinceSearch(inner, payload["since"], budget))
        else:
            without.append(lib)
    if without:
        payload["skipped"]["libraries_without_date_filter"] = without
    if unavailable:
        payload["skipped"]["libraries_unavailable"] = unavailable
    if not connectors:
        payload["shortfalls"].append({
            "step": "new_papers", "reason": "no_library_with_date_filter",
            "note": "None of the collection's libraries can be asked for records since a date. "
                    "Nothing was searched."})
        return
    before = library_key_set(conn, payload["user_id"])  # the library before this search
    stored = SearchProfile(**profile)
    # A track that reached the year of `since` was current then: it goes on to this year, or
    # every newer paper would be filtered out. A profile that ends earlier (a closed
    # historical period) keeps its end year.
    if int(payload["since"][:4]) <= stored.year_to < current_year():
        payload["year_to_extended"] = [stored.year_to, current_year()]
        stored = stored.model_copy(update={"year_to": current_year()})
    request = SearchRequest(profile=stored,
                            libraries=[c.name for c in connectors])
    result = run_search(conn, payload["user_id"], request, connectors, max_results)
    run_id, summary = result["id"], result["summary"]
    summary.update({"origin": "collection_refresh", "collection_id": collection["id"],
                    "since": payload["since"],
                    "year_to_extended": payload.get("year_to_extended")})
    conn.execute("UPDATE search_runs SET summary = ? WHERE id = ?", (json.dumps(summary), run_id))
    payload["run_id"] = run_id

    inside = _collection_keys(conn, collection["id"])
    removed = {(r[0], r[1]) for r in conn.execute(
        "SELECT source, paper_id FROM collection_removed WHERE collection_id = ?",
        (collection["id"],))}
    for r in conn.execute(
            "SELECT p.* FROM run_papers rp JOIN papers p ON p.source = rp.source"
            " AND p.id = rp.paper_id WHERE rp.run_id = ? ORDER BY rp.rank", (run_id,)):
        if (r["source"], r["id"]) in removed:
            payload["skipped_removed"] += 1
            continue
        if in_library(dict(r), inside):
            payload["already_in_collection"] += 1
            continue
        conn.execute("INSERT OR IGNORE INTO collection_papers(collection_id, source, paper_id,"
                     " added_from_run, origin) VALUES(?,?,?,?,'refresh')",
                     (collection["id"], r["source"], r["id"], run_id))
        payload["new_papers"] += 1
        if in_library(dict(r), before):
            payload["already_in_library"] += 1  # found earlier for another track
        inside |= work_keys(r)
    truncated = [c.name for c in connectors if c.truncated]
    complete = (not budget.exhausted and not truncated
                and summary["stopped"] not in ("limit", "scan_limit"))
    if budget.exhausted:
        payload["shortfalls"].append({
            "step": "new_papers", "reason": "budget",
            "note": "The request budget ran out before every new record was read. The next "
                    "refresh starts from the same date."})
    elif truncated:
        payload["shortfalls"].append({
            "step": "new_papers", "reason": "truncated", "libraries": truncated,
            "note": "A library returned only its first page (see the run's warnings). The next "
                    "refresh starts from the same date."})
    elif not complete:
        payload["shortfalls"].append({
            "step": "new_papers", "reason": summary["stopped"],
            "note": f"More new records than one search keeps ({max_results}). The next "
                    "refresh starts from the same date."})
    if complete:
        conn.execute("UPDATE collections SET last_refreshed_at = ? WHERE id = ?",
                     (payload["started"], collection["id"]))


def citation_candidates(conn: sqlite3.Connection, collection_id: int, cutoff: str
                        ) -> tuple[dict, dict, dict]:
    """Collection papers with a stale count, grouped by W-id and (when there is none, or it
    was not found) by DOI, and what cannot be refreshed."""
    rows = conn.execute(
        "SELECT p.source, p.id, p.doi, p.openalex_id, p.openalex_status FROM collection_papers cp"
        " JOIN papers p ON p.source = cp.source AND p.id = cp.paper_id"
        " WHERE cp.collection_id = ? AND p.source != ?"
        " AND (p.citations_updated_at IS NULL OR p.citations_updated_at < ?)"
        " ORDER BY p.source, p.id", (collection_id, USER_SOURCE, cutoff)).fetchall()
    by_id: dict = {}
    by_doi: dict = {}
    skipped = {"not_in_openalex": 0, "no_identifier": 0}
    for r in rows:
        wid = r["openalex_id"] or (r["id"] if r["source"] == "openalex" else "")
        doi = (r["doi"] or "").lower()
        status = r["openalex_status"]
        if wid and status in (None, NO_LIST):
            by_id.setdefault(wid, []).append((r["source"], r["id"]))
        elif status in (None, RETRY_DOI) and USABLE_DOI.fullmatch(doi):
            by_doi.setdefault(doi, []).append((r["source"], r["id"]))
        elif status == NOT_FOUND:
            skipped["not_in_openalex"] += 1
        else:
            skipped["no_identifier"] += 1
    return by_id, by_doi, skipped


def _citations(conn: sqlite3.Connection, job_id: int, payload: dict, budget: RequestBudget,
               lookup, max_age_days: int) -> None:
    by_id, by_doi, skipped = citation_candidates(conn, payload["collection_id"],
                                                 stale_cutoff(max_age_days))
    payload["skipped"].update({k: v for k, v in skipped.items() if v})
    counters = {"updated": payload["citations_updated"],
                "not_found": payload["citations_not_found"]}
    ids = list(by_id)
    while ids or by_doi:
        if not budget.take():
            left = sum(len(v) for v in by_id.values()) + sum(len(v) for v in by_doi.values())
            payload["shortfalls"].append({
                "step": "citations", "reason": "budget", "remaining": left,
                "note": f"The request budget ran out with {left} stale citation counts left."})
            return
        if ids:
            kind, keys, source = "openalex", ids[:LOOKUP_BATCH], by_id
            ids = ids[LOOKUP_BATCH:]
        else:
            kind, keys, source = "doi", list(by_doi)[:LOOKUP_BATCH], by_doi
        groups = {k: source.pop(k) for k in keys}
        _lookup(conn, lookup, kind, groups, counters, by_doi)
        payload["citations_updated"] = counters["updated"]
        payload["citations_not_found"] = counters["not_found"]
        _save(conn, job_id, payload)


def run_refresh_job(db_path: Path, job_id: int, factory: Callable, max_results: int,
                    max_age_days: int, claimed: bool = False) -> None:
    """Background job body. Resumable: the request count is saved before each request is
    sent, and a restarted job carries on from its phase with the budget left."""
    conn = connect(db_path)
    payload: dict | None = None
    try:
        if not claimed and not claim_job(conn, job_id, kind=REFRESH_KIND):
            return
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (job_id,)).fetchone()["payload"])

        def counted(used: int) -> None:
            payload["requests_made"] = used
            _save(conn, job_id, payload)

        budget = RequestBudget(payload["max_requests"], payload["requests_made"], counted)
        _save(conn, job_id, payload, status="running")
        collection = conn.execute("SELECT * FROM collections WHERE id = ? AND user_id = ?",
                                  (payload["collection_id"], payload["user_id"])).fetchone()
        if collection is None:
            set_job(conn, job_id, "failed", "collection deleted")
            return
        if payload["phase"] == "new_papers":
            _new_papers(conn, payload, collection, budget, factory, max_results)
            payload["phase"] = "citations"
            _save(conn, job_id, payload)
        if payload["phase"] == "citations":
            if payload["citations"]:
                lookup = factory("openalex")
                if not hasattr(lookup, "lookup"):
                    payload["shortfalls"].append({
                        "step": "citations", "reason": "no_lookup",
                        "note": "OpenAlex lookups are not available here."})
                else:
                    if hasattr(lookup, "budget"):
                        lookup.budget = budget  # retries count against the budget too
                    _citations(conn, job_id, payload, budget, lookup, max_age_days)
            payload["phase"] = "done"
        _save(conn, job_id, payload, status="partial" if payload["shortfalls"] else "done",
              finished=True)
        set_job(conn, job_id, "done")
    except Exception as exc:
        error = str(exc) if isinstance(exc, ConnectorError) else type(exc).__name__
        log.warning("refresh job %s stopped: %s", job_id, error)
        conn.rollback()
        if payload is not None:
            _save(conn, job_id, payload, status="failed", error=error, finished=True)
        set_job(conn, job_id, "failed", error)
    finally:
        conn.close()


def refresh_view(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "job_id": row["job_id"], "trigger": row["trigger"],
            "status": row["status"], "since": row["since"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "new_papers": row["new_papers"],
            "citations_updated": row["citations_updated"],
            "requests_made": row["requests_made"], "run_id": row["run_id"],
            "shortfalls": json.loads(row["shortfalls"]) if row["shortfalls"] else [],
            "skipped": json.loads(row["skipped"]) if row["skipped"] else {},
            "error": row["error"]}


def queue_auto_refreshes(conn: sqlite3.Connection, days: int, max_requests: int) -> list[int]:
    """Queue a refresh for each collection with a profile whose last refresh (or creation) is
    older than `days` and that had no refresh attempt in that time. Off when days <= 0. The
    budget is kept within 1..MAX_REFRESH_REQUESTS."""
    if days <= 0:
        return []
    window = f"-{int(days)} days"
    rows = conn.execute(
        "SELECT c.id, c.user_id FROM collections c WHERE c.profile IS NOT NULL"
        " AND COALESCE(c.libraries, '[]') != '[]'"
        " AND julianday(COALESCE(c.last_refreshed_at, c.created_at)) <= julianday('now', ?)"
        " AND NOT EXISTS (SELECT 1 FROM collection_refreshes r WHERE r.collection_id = c.id"
        " AND julianday(r.started_at) > julianday('now', ?)) ORDER BY c.id",
        (window, window)).fetchall()
    budget = max(1, min(max_requests, MAX_REFRESH_REQUESTS))
    jobs = []
    for r in rows:
        # The same lock as POST /refresh, so a manual refresh cannot slip in between.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if open_refresh(conn, r["id"]) is None:
                jobs.append(queue_refresh(conn, r["id"], r["user_id"], budget, True, "auto"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return jobs


def requests_reserved(conn: sqlite3.Connection, user_id: int) -> int:
    """Request budgets of the user's manual refreshes started in the last hour."""
    return conn.execute(
        "SELECT COALESCE(SUM(json_extract(j.payload, '$.max_requests')), 0)"
        " FROM collection_refreshes r JOIN jobs j ON j.id = r.job_id"
        " JOIN collections c ON c.id = r.collection_id WHERE c.user_id = ?"
        " AND r.trigger = 'manual' AND julianday(r.started_at) > julianday('now', '-1 hour')",
        (user_id,)).fetchone()[0]


def run_waiting_refreshes(db_path: Path, factory: Callable, max_results: int,
                          max_age_days: int, stale_seconds: int) -> list[int]:
    """Run queued automatic refreshes and continue interrupted ones."""
    with closing(connect(db_path)) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND (status = 'pending' AND"
            " json_extract(payload, '$.phase') = 'new_papers' AND"
            " EXISTS (SELECT 1 FROM collection_refreshes r WHERE r.job_id = jobs.id"
            " AND r.trigger = 'auto') OR status IN ('pending', 'running') AND"
            " COALESCE(updated_at, created_at) <= datetime('now', ?)) ORDER BY id",
            (REFRESH_KIND, f"-{int(stale_seconds)} seconds"))]
    done = []
    for job_id in ids:
        with closing(connect(db_path)) as conn:
            if not (claim_job(conn, job_id, kind=REFRESH_KIND)
                    or claim_job(conn, job_id, stale_seconds, kind=REFRESH_KIND)):
                continue
        run_refresh_job(db_path, job_id, factory, max_results, max_age_days, claimed=True)
        done.append(job_id)
    return done
