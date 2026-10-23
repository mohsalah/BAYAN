"""Run a search: profile -> connectors -> dedupe/quotas -> stored run. Retrieval is plain code."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from itertools import zip_longest
from typing import Literal

from pydantic import BaseModel, Field

from .connectors.base import FILTERS, Connector
from .doi_lookup import DoiLooker, DoiLookup
from .models import Paper, SearchProfile
from .quotas import allocate_quotas
from .retrieval import collect

SCAN_FACTOR = 20  # a run reads at most search_max_results * SCAN_FACTOR works before stopping


class SearchRequest(BaseModel):
    profile: SearchProfile
    libraries: list[str] = Field(default_factory=lambda: ["openalex"], min_length=1)
    # Optional per-year quotas over the chosen period. Without it, the first N results are kept.
    quota_total: int | None = Field(default=None, ge=1)
    weighting: Literal["equal", "recent"] = "equal"


def quota_weights(profile: SearchProfile, weighting: str) -> dict[int, int]:
    """Equal weights, or the reference project's 200:150:50 recency weighting (4:3:1)."""
    years = range(profile.year_from, profile.year_to + 1)
    if weighting == "equal":
        return {y: 1 for y in years}
    return {y: 4 if y >= 2010 else 3 if y >= 2000 else 1 for y in years}


NO_DOI_LOOKUP = {"attempted": 0, "matched": 0, "no_match": 0, "failed": 0,
                 "skipped_over_budget": 0, "cache_hits": 0}

_DONE = object()


def _interleave(streams: list[Iterator[Paper]]) -> Iterator[Paper]:
    """Round-robin over libraries so a full first library cannot starve the others."""
    for round_ in zip_longest(*streams, fillvalue=_DONE):
        for paper in round_:
            if paper is not _DONE:
                yield paper


def requested_filters(profile: SearchProfile) -> list[str]:
    asked = {"years": True, "document_types": bool(profile.document_types),
             "languages": bool(profile.languages), "countries": bool(profile.countries)}
    return [f for f in FILTERS if asked[f]]


def _counted(connector: Connector, profile: SearchProfile, state: dict) -> Iterator[Paper]:
    for paper in connector.search(profile):
        state["scanned_by_library"][connector.name] += 1
        yield paper


def _scan(stream: Iterator[Paper], limit: int, state: dict) -> Iterator[Paper]:
    for paper in stream:
        if state["scanned"] >= limit:
            state["stopped"] = "scan_limit"
            return
        state["scanned"] += 1
        yield paper


def run_search(
    conn: sqlite3.Connection,
    user_id: int,
    request: SearchRequest,
    connectors: list[Connector],
    max_results: int,
    doi_lookup: DoiLooker | None = None,
    doi_budget: int = 0,
) -> dict:
    profile = request.profile
    quotas = None
    if request.quota_total is not None:
        quotas = allocate_quotas(request.quota_total, quota_weights(profile, request.weighting))
    wanted = request.quota_total or max_results
    state = {"scanned": 0, "stopped": "exhausted",
             "scanned_by_library": {c.name: 0 for c in connectors}}
    stream = _scan(_interleave([_counted(c, profile, state) for c in connectors]),
                   max_results * SCAN_FACTOR, state)
    if quotas is None:
        stream = _first(stream, wanted, state)
    lookup = DoiLookup(conn, doi_lookup, doi_budget) if doi_lookup is not None else None
    result = collect(stream, quotas, enrich=lookup)
    if quotas is not None and not result.shortfalls:
        state["stopped"] = "complete"

    summary = {
        "libraries": [c.name for c in connectors],
        "found": len(result.papers),
        "scanned": state["scanned"],
        "scanned_by_library": state["scanned_by_library"],
        # Stored papers per library; a work found by several libraries counts for the first one.
        "by_library": {c.name: sum(1 for p in result.papers if p.source == c.name)
                       for c in connectors},
        # Requested filters each library cannot apply. Results are not post-filtered by guessing.
        "unapplied_filters": {
            c.name: [f for f in requested_filters(profile)
                     if f not in getattr(c, "supported_filters", frozenset())]
            for c in connectors},
        "stopped": state["stopped"],
        "duplicates": result.duplicates,
        "skipped_over_quota": result.skipped_over_quota,
        "skipped_no_year": result.skipped_no_year,
        "by_document_type": dict(Counter(p.document_type or "unknown" for p in result.papers)),
        "by_language": dict(Counter(p.language or "unknown" for p in result.papers)),
        # Crossref DOI lookup for papers found without a DOI (cache hits are not "attempted").
        "doi_lookup": lookup.counts if lookup else dict(NO_DOI_LOOKUP),
        # DOIs filled in by the lookup in this run, with the title-match score.
        "doi_matches": [{"source": p.source, "id": p.id, "doi": p.doi,
                         "score": p.doi_match_score}
                        for p in result.papers if p.doi_source],
        "shortfalls": {str(y): {"quota": q, "collected": c}
                       for y, (q, c) in result.shortfalls.items()},
    }
    cur = conn.execute(
        "INSERT INTO search_runs(user_id, request, summary) VALUES(?,?,?)",
        (user_id, request.model_dump_json(), json.dumps(summary)),
    )
    run_id = cur.lastrowid
    for rank, paper in enumerate(result.papers):
        _store_paper(conn, paper)
        conn.execute(
            "INSERT OR IGNORE INTO run_papers(run_id, source, paper_id, rank) VALUES(?,?,?,?)",
            (run_id, paper.source, paper.id, rank),
        )
    conn.commit()
    return {"id": run_id, "summary": summary}


def _first(stream: Iterator[Paper], n: int, state: dict) -> Iterator[Paper]:
    count = 0
    for paper in stream:
        if count >= n:
            state["stopped"] = "limit"
            return
        count += 1
        yield paper


def _store_paper(conn: sqlite3.Connection, p: Paper) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO papers(source, id, doi, title, abstract, year, document_type,"
        " language, venue, first_author, countries, citations, is_oa, oa_url, landing_url,"
        " keywords, topic) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.source, p.id, p.doi, p.title, p.abstract, p.year, p.document_type, p.language,
         p.venue, p.first_author, json.dumps(p.countries), p.citations, int(p.is_oa), p.oa_url,
         p.landing_url, json.dumps(p.keywords), p.topic),
    )
    conn.execute("DELETE FROM paper_search WHERE source=? AND id=?", (p.source, p.id))
    conn.execute(
        "INSERT INTO paper_search(source, id, title, abstract, keywords, topic)"
        " VALUES(?,?,?,?,?,?)",
        (p.source, p.id, p.title, p.abstract, " ".join(p.keywords), p.topic),
    )


def paper_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["countries"] = json.loads(d["countries"] or "[]")
    d["keywords"] = json.loads(d["keywords"] or "[]")
    d["is_oa"] = bool(d["is_oa"])
    # Honest wording: a listed open link is not proof that a valid PDF can be downloaded.
    d["access"] = "open_copy_listed" if d["oa_url"] else "no_open_copy_listed"
    d.pop("abstract", None)
    return d
