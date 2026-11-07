"""Run a search: profile -> connectors -> dedupe/quotas -> stored run. Retrieval is plain code."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, Field

from .connectors.base import FILTERS, Connector
from .db import now_iso
from .doi_lookup import DoiLooker, DoiLookup
from .models import DOCUMENT_TYPES, MAX_AUTHORS, USER_SOURCE, Paper, SearchProfile, language_key
from .quotas import allocate_quotas
from .regions import REGIONS_VERSION
from .retrieval import collect
from .textnorm import index_text

SCAN_FACTOR = 20  # a run reads at most search_max_results * SCAN_FACTOR works before stopping


class SearchRequest(BaseModel):
    profile: SearchProfile
    libraries: list[str] = Field(default_factory=lambda: ["openalex"], min_length=1)
    # Optional per-year quotas over the chosen period. Without it, the first N results are kept.
    quota_total: int | None = Field(default=None, ge=1)
    weighting: Literal["equal", "recent"] = "equal"
    # remote: search the libraries (default). local: search only the papers you already have.
    # local_then_remote: local hits first, then the remote search, with new/known counts.
    mode: Literal["remote", "local", "local_then_remote"] = "remote"


def quota_weights(profile: SearchProfile, weighting: str) -> dict[int, int]:
    """Equal weights, or the reference project's 200:150:50 recency weighting (4:3:1)."""
    years = range(profile.year_from, profile.year_to + 1)
    if weighting == "equal":
        return {y: 1 for y in years}
    return {y: 4 if y >= 2010 else 3 if y >= 2000 else 1 for y in years}


NO_DOI_LOOKUP = {"attempted": 0, "matched": 0, "no_match": 0, "failed": 0,
                 "skipped_over_budget": 0, "skipped_over_time": 0, "cache_hits": 0}

_DONE = object()


def _interleave(streams: list[Iterator[Paper]]) -> Iterator[Paper]:
    """Round-robin over libraries so a full first library cannot starve the others.

    Each record is read only when it is wanted: reading a whole round ahead made every other
    library fetch its next page for records that a full result then threw away.
    """
    active = list(streams)
    while active:
        for stream in list(active):
            paper = next(stream, _DONE)
            if paper is _DONE:
                active.remove(stream)
            else:
                yield paper


def requested_filters(profile: SearchProfile) -> list[str]:
    asked = {"years": True, "document_types": bool(profile.document_types),
             "languages": bool(profile.languages), "countries": bool(profile.geo_countries),
             "topics": bool(profile.topics)}
    return [f for f in FILTERS if asked[f]]


def _splits_by_language(connector: Connector) -> bool:
    return "languages" in getattr(connector, "supported_filters", frozenset())


def per_language_plan(profile: SearchProfile, connectors: list[Connector]) -> dict:
    """What each language searched for, and which libraries could use it (for reruns).

    A library that cannot filter by language runs the main query once; that is noted.
    """
    pairs = profile.language_queries()
    return {
        "per_language": bool(pairs),
        "queries": dict(pairs),
        "main_query": profile.query,
        "run_per_language": [c.name for c in connectors if pairs and _splits_by_language(c)],
        "notes": [f"{c.name} cannot filter by language, so it ran the main query once, "
                  "not the per-language text."
                  for c in connectors if pairs and not _splits_by_language(c)],
    }


def _counted(connector: Connector, profile: SearchProfile, state: dict) -> Iterator[Paper]:
    """One library's records. With per-language text and a language filter, one query per
    chosen language, round-robin, so the combined stream feeds de-duplication and limits."""
    pairs = profile.language_queries()
    if pairs and _splits_by_language(connector):
        source = _interleave(
            [connector.search(profile.for_language(code, text)) for code, text in pairs])
    else:
        source = connector.search(profile)
    for paper in source:
        state["scanned_by_library"][connector.name] += 1
        yield paper


def _sorts(connector: Connector) -> frozenset[str]:
    return getattr(connector, "supported_sorts", frozenset({"relevance"}))


def _until_full(stream: Iterator[Paper], year: int, quotas: dict[int, int],
                counts: dict[int, int]) -> Iterator[Paper]:
    """One year's records until that year's quota is kept. Checked before each read, so a full
    year never asks its library for another record (or page)."""
    while counts.get(year, 0) < quotas[year]:
        paper = next(stream, _DONE)
        if paper is _DONE:
            return
        yield paper


def _library_stream(connector: Connector, profile: SearchProfile, state: dict,
                    quotas: dict[int, int] | None, counts: dict[int, int]) -> Iterator[Paper]:
    """A library's records. Most cited first with year quotas runs one query per year with a
    quota (round-robin), so recent years, with fewer citations so far, are not crowded out by
    older ones; each year's query stops once that year is full."""
    if not (profile.sort == "citations" and quotas and "citations" in _sorts(connector)):
        return _counted(connector, profile, state)
    years = [y for y, q in sorted(quotas.items()) if q > 0]
    state["sorted_by_year"].append(connector.name)
    return _interleave([
        _until_full(_counted(connector, profile.model_copy(
            update={"year_from": y, "year_to": y}), state), y, quotas, counts)
        for y in years])


def _scan(stream: Iterator[Paper], limit: int, state: dict) -> Iterator[Paper]:
    # Checked after each record, so reaching the limit never reads (and pages for) one more.
    if state["scanned"] >= limit:
        state["stopped"] = "scan_limit"
        return
    for paper in stream:
        state["scanned"] += 1
        yield paper
        if state["scanned"] >= limit:
            state["stopped"] = "scan_limit"
            return


def _type_filtered(stream: Iterator[Paper], profile: SearchProfile, state: dict) -> Iterator[Paper]:
    """Keep only records whose category was selected; count the rest. No selection = keep all."""
    selected = set(profile.document_types)
    for paper in stream:
        if selected and paper.category not in selected:
            state["filtered_by_type"] += 1
            continue
        yield paper


def geo_status(countries: list[str], codes: set[str], mode: str) -> str:
    """matched / unknown / outside from the countries a record itself reports (structured data).

    No reported country means "unknown": never a match and never a reason to exclude.
    """
    if not countries:
        return "unknown"
    hit = bool(codes.intersection(countries))
    if mode == "exclude":
        return "outside" if hit else "matched"
    return "matched" if hit else "outside"


def _geo_filtered(stream: Iterator[Paper], profile: SearchProfile, state: dict) -> Iterator[Paper]:
    """Drop records whose reported countries are outside the chosen area; keep unknown ones."""
    codes = set(profile.geo_countries)
    for paper in stream:
        if codes and geo_status(paper.countries, codes, profile.geo_mode) == "outside":
            state["geo_outside"] += 1
            continue
        yield paper


def _geography_summary(profile: SearchProfile, papers: list[Paper], state: dict) -> dict:
    """What was asked, what it expanded to (for reruns) and the matched/unknown/outside counts."""
    codes = profile.geo_countries
    counts: Counter = Counter()
    if codes:
        for paper in list(papers):
            status = geo_status(paper.countries, set(codes), profile.geo_mode)
            if status == "outside":
                # A merged duplicate revealed countries after the first check: drop it now.
                papers.remove(paper)
                state["geo_outside"] += 1
            else:
                counts[status] += 1
    return {
        "active": bool(codes),
        "basis": profile.geo_basis,
        "mode": profile.geo_mode,
        "regions": profile.regions,
        "countries": profile.countries,
        "expanded_countries": codes,
        "regions_version": REGIONS_VERSION,
        "matched": counts["matched"],
        "unknown": counts["unknown"],
        "outside": state["geo_outside"],
    }


def run_search(
    conn: sqlite3.Connection,
    user_id: int,
    request: SearchRequest,
    connectors: list[Connector],
    max_results: int,
    doi_lookup: DoiLooker | None = None,
    doi_budget: int = 0,
    doi_seconds: float | None = None,
) -> dict:
    profile = request.profile
    quotas = None
    if request.quota_total is not None:
        quotas = allocate_quotas(request.quota_total, quota_weights(profile, request.weighting))
    wanted = request.quota_total or max_results
    state = {"scanned": 0, "stopped": "exhausted", "filtered_by_type": 0, "geo_outside": 0,
             "scanned_by_library": {c.name: 0 for c in connectors}, "sorted_by_year": []}
    counts: dict[int, int] = {}
    stream = _scan(_interleave([_library_stream(c, profile, state, quotas, counts)
                                for c in connectors]),
                   max_results * SCAN_FACTOR, state)
    stream = _type_filtered(stream, profile, state)
    stream = _geo_filtered(stream, profile, state)
    lookup = (DoiLookup(conn, doi_lookup, doi_budget, doi_seconds)
              if doi_lookup is not None else None)
    # Without quotas the limit counts kept papers, after de-duplication, so a paper found in
    # two libraries does not use up two places.
    result = collect(stream, quotas, enrich=lookup, limit=None if quotas else wanted,
                     counts=counts)
    if quotas is not None and not result.shortfalls:
        state["stopped"] = "complete"
    elif quotas is None and len(result.papers) >= wanted:
        state["stopped"] = "limit"

    if profile.sort == "citations":
        # Most cited first across years for records from libraries that sorted by citations.
        # Other libraries report no citation count we can trust as 0 ("not reported" is not
        # zero), so their records follow, in their own order (the sort is stable).
        sorting = {c.name for c in connectors if "citations" in _sorts(c)}
        result.papers.sort(key=lambda p: (0, -p.citations) if p.source in sorting else (1, 0))
    geography = _geography_summary(profile, result.papers, state)
    by_category = Counter(p.category for p in result.papers)
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
        "sort": profile.sort,
        # Libraries that could not sort by citations: searched in their own order, and their
        # records are listed after the sorted ones.
        "sort_unapplied": [c.name for c in connectors if profile.sort not in _sorts(c)],
        # Libraries that ran one most-cited query per year with a quota.
        "sorted_by_year": state["sorted_by_year"],
        # Once each: a per-year search asks a library several times and may repeat a note.
        "warnings": list(dict.fromkeys(
            w for c in connectors for w in getattr(c, "warnings", []))),
        "duplicates": result.duplicates,
        "skipped_over_quota": result.skipped_over_quota,
        "skipped_no_year": result.skipped_no_year,
        # Scanned records dropped because their category was not selected.
        "filtered_by_type": state["filtered_by_type"],
        # Every selected category is listed, with 0 when nothing was found.
        "by_category": {c: by_category[c] for c in (profile.document_types or DOCUMENT_TYPES)},
        "geography": geography,
        "by_language": dict(Counter(language_key(p.language) for p in result.papers)),
        "language_queries": per_language_plan(profile, connectors),
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


# Libraries whose records carry a citation count. Others report none, so storing their record
# does not mean the count was checked (citations_updated_at stays unset).
CITATION_LIBRARIES = frozenset({"openalex", "crossref", "europepmc"})


def _store_paper(conn: sqlite3.Connection, p: Paper) -> None:
    openalex_id = p.openalex_id or (p.id if p.source == "openalex" else "")
    if not openalex_id and p.doi:
        # Another library's record of a work already stored from OpenAlex.
        row = conn.execute("SELECT openalex_id FROM papers WHERE doi = ? AND openalex_id"
                           " IS NOT NULL AND openalex_id != '' LIMIT 1", (p.doi,)).fetchone()
        openalex_id = row["openalex_id"] if row else ""
    refs = p.referenced_works if openalex_id else None
    # How many references are stored for this work: from this record's list, or from an
    # earlier record of the same work (so backfill does not look it up again).
    known_refs = len(refs) if refs is not None else None
    if openalex_id and known_refs is None:
        known_refs = conn.execute(
            "SELECT MAX(references_count) FROM papers WHERE openalex_id = ?",
            (openalex_id,)).fetchone()[0]
    # An upsert, not a replace: a later record without OpenAlex data (no W-id, no yearly counts,
    # no reference list) keeps what an earlier one stored.
    conn.execute(
        "INSERT INTO papers(source, id, doi, title, abstract, year, native_type,"
        " category, category_basis, document_kind, language, venue, first_author, countries,"
        " citations, is_oa, oa_url, landing_url, keywords, topic, openalex_id, counts_by_year,"
        " citations_updated_at, references_count, authors)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source, id) DO UPDATE SET doi = excluded.doi, title = excluded.title,"
        " abstract = excluded.abstract, year = excluded.year,"
        " native_type = excluded.native_type, category = excluded.category,"
        " category_basis = excluded.category_basis, document_kind = excluded.document_kind,"
        " language = excluded.language, venue = excluded.venue,"
        " first_author = excluded.first_author, countries = excluded.countries,"
        " citations = excluded.citations, is_oa = excluded.is_oa, oa_url = excluded.oa_url,"
        " landing_url = excluded.landing_url, keywords = excluded.keywords,"
        " topic = excluded.topic,"
        " openalex_id = COALESCE(excluded.openalex_id, papers.openalex_id),"
        " counts_by_year = COALESCE(excluded.counts_by_year, papers.counts_by_year),"
        " citations_updated_at = COALESCE(excluded.citations_updated_at,"
        " papers.citations_updated_at),"
        " references_count = COALESCE(excluded.references_count, papers.references_count),"
        # A record that lists no authors keeps the authors stored from an earlier one.
        " authors = CASE WHEN excluded.authors = '[]' THEN papers.authors"
        " ELSE excluded.authors END",
        (p.source, p.id, p.doi, p.title, p.abstract, p.year, p.native_type, p.category,
         p.category_basis, p.document_kind or None, p.language, p.venue, p.first_author,
         json.dumps(p.countries), p.citations, int(p.is_oa), p.oa_url, p.landing_url,
         json.dumps(p.keywords), p.topic, openalex_id or None,
         json.dumps(p.counts_by_year) if p.counts_by_year is not None else None,
         now_iso() if p.source in CITATION_LIBRARIES else None,
         known_refs, json.dumps(p.authors[:MAX_AUTHORS], ensure_ascii=False)),
    )
    if openalex_id:
        if refs is not None:
            store_references(conn, openalex_id, refs)
        if p.doi:
            # Records of the same work from other libraries now share its W-id and its stored
            # references; an earlier failed lookup no longer applies to them.
            conn.execute("UPDATE papers SET openalex_id = ?, openalex_status = NULL,"
                         " references_count = COALESCE(references_count, ?) WHERE doi = ? AND"
                         " (openalex_id IS NULL OR openalex_id = '')",
                         (openalex_id, known_refs, p.doi))
    conn.execute("DELETE FROM paper_search WHERE source=? AND id=?", (p.source, p.id))
    conn.execute(
        "INSERT INTO paper_search(source, id, title, abstract, keywords, topic)"
        " VALUES(?,?,?,?,?,?)",
        (p.source, p.id, index_text(p.title), index_text(p.abstract),
         index_text(" ".join(p.keywords)), index_text(p.topic)),
    )


def store_references(conn: sqlite3.Connection, citing: str, cited: list[str]) -> None:
    """Replace one work's reference rows. Only call it with a list a library supplied."""
    conn.execute("DELETE FROM paper_references WHERE citing = ?", (citing,))
    conn.executemany("INSERT OR IGNORE INTO paper_references(citing, cited) VALUES(?,?)",
                     [(citing, c) for c in cited if c and c != citing])


def paper_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["countries"] = json.loads(d["countries"] or "[]")
    d["keywords"] = json.loads(d["keywords"] or "[]")
    # Rows stored before the author list existed only have the first author.
    d["authors"] = json.loads(d.get("authors") or "[]") or (
        [d["first_author"]] if d.get("first_author") else [])
    d["is_oa"] = bool(d["is_oa"])
    # Rows stored before categories existed only have the old `document_type` column.
    legacy = d.pop("document_type", None) or ""
    if not d.get("category"):
        d["category"] = "other"
        d["category_basis"] = ("stored before reference categories existed; "
                               + (f"native type '{legacy}'" if legacy else "no type stored"))
    d["native_type"] = d.get("native_type") or legacy
    d["category_basis"] = d.get("category_basis") or ""
    d["document_kind"] = d.get("document_kind") or ""
    d["user_supplied"] = d["source"] == USER_SOURCE
    # Honest wording: a listed open link is not proof that a valid PDF can be downloaded.
    d["access"] = "open_copy_listed" if d["oa_url"] else "no_open_copy_listed"
    d["abstract"] = d.get("abstract") or ""
    d["abstract_status"] = "supplied" if d["abstract"] else "not_supplied_by_source"
    d["openalex_id"] = d.get("openalex_id") or ""
    raw = d.get("counts_by_year")
    d["counts_by_year"] = json.loads(raw) if raw else None
    d.pop("openalex_status", None)
    return d
