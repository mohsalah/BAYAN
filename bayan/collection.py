"""Collections per research track (M31): one corpus per track, built once and grown over time,
with counts and citation-network milestones computed from the database.

Collections are owner-only. Their papers join the user's library (bayan.library), so the local
search, backfill and the coverage rule apply to them too.
"""

import json
import math
import sqlite3
from collections import Counter

from .library import coverage, current_year, library_keys
from .models import USER_SOURCE, Paper
from .network import build_graph, local_citations, main_paths, recent_velocity, velocity
from .retrieval import doi_key
from .search import _store_paper

REFERENCES_NOTE_BELOW = 0.8
BACKFILL_NOTE = ("References are stored for only {pct}% of this collection's papers, so these "
                 "network results are incomplete. Run the backfill (POST /api/library/backfill) "
                 "and look again.")
TOP_SHARE = 0.05  # milestones: the top 5% by local citation score


def collection_keys(collection_id: int) -> str:
    """SQL (parameter `:user`) for a collection's (source, paper_id) pairs, owner-checked."""
    return ("SELECT cp.source AS source, cp.paper_id AS paper_id FROM collection_papers cp"
            " JOIN collections c ON c.id = cp.collection_id"
            f" WHERE c.user_id = :user AND c.id = {int(collection_id)}")


def owned(conn: sqlite3.Connection, collection_id: int, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM collections WHERE id = ? AND user_id = ?",
                        (collection_id, user_id)).fetchone()


def view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM collection_papers WHERE collection_id = ?",
                     (row["id"],)).fetchone()[0]
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "profile": json.loads(row["profile"]) if row["profile"] else None,
            "libraries": json.loads(row["libraries"]) if row["libraries"] else [],
            "created_at": row["created_at"], "last_refreshed_at": row["last_refreshed_at"],
            "papers": n}


def add_run(conn: sqlite3.Connection, collection: sqlite3.Row, run: sqlite3.Row,
            commit: bool = True) -> dict:
    """Add a run's papers (idempotent). Papers the owner removed stay out. A collection without
    a stored search profile takes the run's, so it can be refreshed later (M32)."""
    removed = {(r[0], r[1]) for r in conn.execute(
        "SELECT source, paper_id FROM collection_removed WHERE collection_id = ?",
        (collection["id"],))}
    rows = [r for r in conn.execute(
        "SELECT source, paper_id FROM run_papers WHERE run_id = ? ORDER BY rank", (run["id"],))
        if (r["source"], r["paper_id"]) not in removed]
    skipped = conn.execute("SELECT COUNT(*) FROM run_papers WHERE run_id = ?",
                           (run["id"],)).fetchone()[0] - len(rows)
    added = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO collection_papers(collection_id, source, paper_id,"
            " added_from_run, origin) VALUES(?,?,?,?,'run')",
            (collection["id"], r["source"], r["paper_id"], run["id"]))
        added += cur.rowcount
    request = json.loads(run["request"])
    if collection["profile"] is None and request.get("mode", "remote") != "local" and isinstance(
            request.get("profile"), dict):
        conn.execute("UPDATE collections SET profile = ?, libraries = ? WHERE id = ?",
                     (json.dumps(request["profile"]), json.dumps(request.get("libraries", [])),
                      collection["id"]))
    if commit:
        conn.commit()
    return {"added": added, "already": len(rows) - added, "skipped_removed": skipped}


def library_pairs(conn: sqlite3.Connection, user_id: int) -> set[tuple[str, str]]:
    """Every (source, paper_id) in the user's library, read once."""
    return {(r[0], r[1]) for r in conn.execute(f"SELECT source, paper_id FROM ({library_keys()})",
                                               {"user": user_id})}


def edit_papers(conn: sqlite3.Connection, collection_id: int, user_id: int,
                add: list[tuple[str, str]], remove: list[tuple[str, str]]) -> dict | None:
    """Add papers from the library and remove papers. None when a paper to add is not in the
    user's library. A removal is remembered so adding a run again does not undo it."""
    add, remove = list(dict.fromkeys(add)), list(dict.fromkeys(remove))
    if add and not set(add) <= library_pairs(conn, user_id):
        return None
    added = 0
    for s, i in add:
        conn.execute("DELETE FROM collection_removed WHERE collection_id = ? AND source = ?"
                     " AND paper_id = ?", (collection_id, s, i))
        added += conn.execute(
            "INSERT OR IGNORE INTO collection_papers(collection_id, source, paper_id, origin)"
            " VALUES(?,?,?,'manual')", (collection_id, s, i)).rowcount
    removed = 0
    for s, i in remove:
        removed += conn.execute("DELETE FROM collection_papers WHERE collection_id = ? AND"
                                " source = ? AND paper_id = ?", (collection_id, s, i)).rowcount
        conn.execute("INSERT OR IGNORE INTO collection_removed(collection_id, source, paper_id)"
                     " VALUES(?,?,?)", (collection_id, s, i))
    conn.commit()
    return {"added": added, "already": len(add) - added, "removed": removed}


def _papers(conn: sqlite3.Connection, collection_id: int, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT p.* FROM ({collection_keys(collection_id)}) k JOIN papers p"
        " ON p.source = k.source AND p.id = k.paper_id", {"user": user_id}).fetchall()


def _brief(row: sqlite3.Row | dict, year_now: int, **extra) -> dict:
    return {"source": row["source"], "id": row["id"], "title": row["title"] or "",
            "year": row["year"], "doi": row["doi"] or "", "venue": row["venue"] or "",
            "openalex_id": row["openalex_id"] or "", "citations": row["citations"] or 0,
            "velocity": velocity(row["citations"], row["year"], year_now), **extra}


def references_coverage(rows: list[sqlite3.Row]) -> dict:
    eligible = [r for r in rows if r["source"] != USER_SOURCE]
    stored = sum(r["references_count"] is not None for r in eligible)
    share = stored / len(eligible) if eligible else 0.0
    out = {"share": round(share, 4), "stored": stored, "papers": len(eligible)}
    if share < REFERENCES_NOTE_BELOW:
        out["note"] = BACKFILL_NOTE.format(pct=round(100 * share))
    return out


def _nodes(rows: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    """One row per W-id (the OpenAlex record when there is one)."""
    nodes: dict[str, sqlite3.Row] = {}
    for r in sorted(rows, key=lambda r: (r["source"] != "openalex", r["source"], r["id"])):
        if r["openalex_id"] and r["openalex_id"] not in nodes:
            nodes[r["openalex_id"]] = r
    return nodes


def _references(conn: sqlite3.Connection, wids: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for i in range(0, len(wids), 500):
        chunk = wids[i:i + 500]
        out += [(r[0], r[1]) for r in conn.execute(
            f"SELECT citing, cited FROM paper_references WHERE citing IN"
            f" ({','.join('?' * len(chunk))})", chunk)]
    return out


def stats(conn: sqlite3.Connection, collection_id: int, user_id: int, top: int = 10) -> dict:
    records = _papers(conn, collection_id, user_id)
    rows = works(records)
    year_now = current_year()
    nodes = _nodes(records)
    lcs = local_citations(build_graph({w: r["year"] for w, r in nodes.items()},
                                      _references(conn, list(nodes))))
    by_year = Counter(str(r["year"]) if r["year"] is not None else "unknown" for r in rows)
    venues = Counter(r["venue"] for r in rows if r["venue"])
    briefs = [_brief(r, year_now, local_citations=lcs.get(r["openalex_id"] or "", 0))
              for r in rows]
    return {
        "papers": len(rows),  # works: records of the same work from several libraries once
        "records": len(records),
        "by_year": dict(sorted(by_year.items())),
        "top_venues": [{"venue": v, "papers": n} for v, n in venues.most_common(top)],
        "top_cited": sorted(briefs, key=lambda b: (-b["citations"], b["title"]))[:top],
        "top_velocity": sorted([b for b in briefs if b["velocity"] is not None],
                               key=lambda b: (-b["velocity"], b["title"]))[:top],
        "top_local_citations": sorted(briefs, key=lambda b: (-b["local_citations"],
                                                             b["title"]))[:top],
        "references_coverage": references_coverage(records),
        "coverage": coverage(conn, user_id, collection_keys(collection_id)),
    }


def works(records: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """One record per work (same W-id, else same DOI), preferring the OpenAlex record."""
    seen: set[str] = set()
    out = []
    for r in sorted(records, key=lambda r: (r["source"] != "openalex", r["source"], r["id"])):
        keys = {k for k in (f"w:{r['openalex_id']}" if r["openalex_id"] else "",
                            f"doi:{doi_key(r['doi'])}" if doi_key(r["doi"]) else "") if k}
        if keys & seen:
            continue
        seen |= keys or {f"{r['source']}:{r['id']}"}
        out.append(r)
    return out


def foundation_ids(conn: sqlite3.Connection, wids: list[str], min_cocite: int,
                   top: int | None) -> list[tuple[str, int]]:
    """Works outside the collection cited by at least `min_cocite` collection works."""
    if not wids:
        return []
    inside = set(wids)
    counts: Counter = Counter()
    for _citing, cited in set(_references(conn, wids)):
        if cited not in inside:
            counts[cited] += 1
    ranked = sorted(((w, n) for w, n in counts.items() if n >= min_cocite),
                    key=lambda x: (-x[1], x[0]))
    return ranked[:top] if top else ranked


def milestones(conn: sqlite3.Connection, collection_id: int, user_id: int, min_cocite: int = 3,
               recent: int = 3, top: int = 25, key_routes: int = 3) -> dict:
    rows = _papers(conn, collection_id, user_id)
    year_now = current_year()
    nodes = _nodes(rows)
    refs = _references(conn, list(nodes))
    graph = build_graph({w: r["year"] for w, r in nodes.items()}, refs)
    lcs = local_citations(graph)
    paths = main_paths(graph, key_routes)

    def info(w: str, **extra) -> dict:
        r = nodes[w]
        rv = recent_velocity(json.loads(r["counts_by_year"]) if r["counts_by_year"] else None,
                             year_now, recent)
        return _brief(r, year_now, local_citations=lcs[w], recent_velocity=rv, **extra)

    # Top 5% by LCS (at least one work), ties included; a work nobody here cites is not one.
    ranked = sorted((w for w in nodes if lcs[w] > 0), key=lambda w: (-lcs[w], w))
    cut = max(1, math.ceil(TOP_SHARE * len(nodes)))
    threshold = lcs[ranked[cut - 1]] if len(ranked) >= cut else (lcs[ranked[-1]] if ranked else 0)
    why: dict[str, list[str]] = {}
    on_path = {w for route in paths["routes"] for w in route}
    for w in sorted(on_path):
        why.setdefault(w, []).append("main_path")
    for w in ranked:
        if lcs[w] >= threshold:
            why.setdefault(w, []).append("top_5pct_local_citations")
    milestone_list = sorted((info(w, why=reasons) for w, reasons in why.items()),
                            key=lambda m: (m["year"] is None, m["year"] or 0, m["openalex_id"]))

    cites: dict[str, set[str]] = {}
    for citing, cited in refs:
        cites.setdefault(citing, set()).add(cited)
    frontier = []
    for w, r in nodes.items():
        # The last `recent` calendar years, this one included (recent=3 in 2026: 2024-2026).
        if r["year"] is None or r["year"] <= year_now - recent:
            continue
        item = info(w)
        item["score"] = max(item["recent_velocity"] or 0.0, item["velocity"] or 0.0)
        item["cites_milestones"] = sorted(cites.get(w, set()) & set(why))
        frontier.append(item)
    frontier.sort(key=lambda f: (-f["score"], f["openalex_id"]))

    library = {r["openalex_id"]: r for r in conn.execute(
        f"SELECT p.openalex_id, p.title, p.year FROM ({library_keys()}) k JOIN papers p"
        " ON p.source = k.source AND p.id = k.paper_id WHERE p.openalex_id IS NOT NULL",
        {"user": user_id})}
    foundations = [{"openalex_id": w, "cited_by_collection": n,
                    # Titles only for works already in the user's own library.
                    "title": library[w]["title"] if w in library else None,
                    "year": library[w]["year"] if w in library else None}
                   for w, n in foundation_ids(conn, list(nodes), min_cocite, top)]
    return {
        "parameters": {"min_cocite": min_cocite, "recent": recent, "top": top,
                       "key_routes": key_routes},
        "papers": len(rows), "works_with_openalex_id": len(nodes),
        "edges": len(graph.edges), "dag_edges": paths["edges"],
        "dropped_edges": paths["dropped_edges"],
        "main_paths": [[info(w, spc_in=_spc_in(paths, route, i)) for i, w in enumerate(route)]
                       for route in paths["routes"]],
        "milestones": milestone_list,
        "frontier": frontier[:top],
        "foundations": foundations,
        "references_coverage": references_coverage(rows),
        "coverage": coverage(conn, user_id, collection_keys(collection_id)),
        "method": "Deterministic: local citation score, search-path count (SPC) and key-route "
                  "main path from stored references only. Cycles removed by publication year.",
    }


def _spc_in(paths: dict, route: list[str], i: int) -> int | None:
    """SPC of the edge arriving at the i-th work of a route (None for the first)."""
    return paths["spc"].get((route[i - 1], route[i])) if i else None


def add_foundations(conn: sqlite3.Connection, collection_id: int, user_id: int,
                    lookup, min_cocite: int, max_requests: int, batch: int,
                    progress: dict | None = None) -> dict:
    """Fetch foundation works by ID filter and add them to the collection (origin foundation)."""
    rows = _papers(conn, collection_id, user_id)
    nodes = _nodes(rows)
    wanted = [w for w, _ in foundation_ids(conn, list(nodes), min_cocite, None)]
    requests = fetched = 0
    found_ids: set[str] = set()
    for i in range(0, len(wanted), batch):
        if requests >= max_requests:
            break
        chunk = wanted[i:i + batch]
        requests += 1
        if progress is not None:
            progress["requests_made"] = requests
        works: list[Paper] = lookup.lookup("openalex:" + "|".join(chunk))
        for work in works:
            if work.openalex_id not in chunk or work.openalex_id in found_ids:
                continue
            found_ids.add(work.openalex_id)
            _store_paper(conn, work)
            cur = conn.execute(
                "INSERT OR IGNORE INTO collection_papers(collection_id, source, paper_id, origin)"
                " VALUES(?,?,?,'foundation')", (collection_id, work.source, work.id))
            fetched += cur.rowcount
        conn.commit()
    looked_up = wanted[:requests * batch]
    return {"foundations": len(wanted), "requests_made": requests, "added": fetched,
            "not_found": [w for w in looked_up if w not in found_ids],
            "remaining": max(0, len(wanted) - len(looked_up))}
