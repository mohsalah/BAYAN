"""Score agent episodes: one row per search (training data for the models) and one scorecard
row per subfield.

Matching a result to a gold paper uses identifiers first (DOI, or the OpenAlex work ID for
OpenAlex results), then title similarity >= 0.9 with the year within one. Gold papers in a
language BAYAN does not search (outside en/ar/fr/zh/de/es) are out of scope and not counted.

The reliability score (0-100) is a fixed, documented weighting of the measures below; it is a
summary for ranking areas, not a statistical quantity.
"""

import re
from statistics import median

from .gold import title_score

SCOPE_LANGUAGES = {"en", "ar", "fr", "zh", "de", "es"}
WEIGHTS = {"known_item_found": 30, "topical_precision": 20, "gold_recall": 15,
           "no_shortfall": 10, "no_errors": 10, "metadata": 10, "integrity": 5}


def _doi(x: str | None) -> str:
    return (x or "").lower().removeprefix("https://doi.org/").strip()


def matches(paper: dict, gold: dict) -> bool:
    if gold.get("doi") and _doi(paper.get("doi")) == gold["doi"]:
        return True
    if paper.get("source") == "openalex" and paper.get("id") == gold.get("openalex_id"):
        return True
    ids = gold.get("identifiers") or {}
    pid = str(paper.get("id") or "")
    if paper.get("source") == "europepmc" and (
            (ids.get("pmid") and pid == f"MED:{ids['pmid']}")
            or (ids.get("pmcid") and pid == f"PMC:{ids['pmcid']}")):
        return True
    if paper.get("source") == "arxiv" and ids.get("arxiv") and \
            re.sub(r"v\d+$", "", pid) == ids["arxiv"]:
        return True
    year_ok = (paper.get("year") is None or gold.get("year") is None
               or abs(paper["year"] - gold["year"]) <= 1)
    return year_ok and title_score(paper.get("title") or "", gold.get("title") or "") >= 0.9


def rank_of(papers: list[dict], gold: dict) -> int | None:
    for i, p in enumerate(papers, 1):
        if matches(p, gold):
            return i
    return None


def _family(name: str) -> str:
    name = (name or "").strip()
    part = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    return re.sub(r"[\W_]+", "", part.casefold())


def metadata_checks(paper: dict, gold: dict) -> dict:
    out = {"year": paper.get("year") == gold.get("year"),
           "title": title_score(paper.get("title") or "", gold["title"]) >= 0.9}
    if gold.get("doi"):
        out["doi"] = _doi(paper.get("doi")) == gold["doi"]
    if gold.get("first_author") and paper.get("first_author"):
        g, p = _family(gold["first_author"]), _family(paper["first_author"])
        out["first_author"] = bool(g) and (g == p or g in p or p in g)
    return out


def topic_map(taxonomy: dict) -> dict[str, tuple[str, str]]:
    """OpenAlex topic name -> (subfield id, field id)."""
    return {name: (a["id"], a["field"]["id"]) for a in taxonomy["areas"]
            for name in a.get("all_topics") or []}


def search_rows(episode: dict, gold: list[dict], topics: dict) -> list[dict]:
    """One row per search step, with its outcome. These rows are the models' training data."""
    area = episode["area"]
    in_scope = [g for g in gold if (g.get("language") or "en") in SCOPE_LANGUAGES]
    by_id = {g["openalex_id"]: g for g in gold}
    rows = []
    for st in episode["steps"]:
        if st["kind"] not in ("topic", "known_item", "special_query", "language"):
            continue
        papers = st.get("papers") or []
        y0, y1 = st.get("years") or [None, None]
        known = [topics[p["topic"]] for p in papers
                 if p.get("source") == "openalex" and p.get("topic") in topics]
        row = {
            "subfield_id": area["id"], "subfield": area["name"],
            "field_id": area["field"]["id"], "field": area["field"]["name"],
            "domain": area["domain"]["name"], "frascati": area["frascati"],
            "kind": st["kind"], "query_origin": st.get("query_origin", ""),
            "stratum": st.get("stratum", ""), "query": st.get("query", ""),
            "query_words": len((st.get("query") or "").split()),
            "query_chars": len(st.get("query") or ""),
            "boolean": int(bool(re.search(r"\b(AND|OR|NOT)\b|\"", st.get("query") or ""))),
            "languages": ",".join(st.get("languages") or []),
            "libraries": ",".join(st.get("libraries") or []),
            "year_from": y0, "year_to": y1, "status": st.get("status"),
            "error": st.get("error", ""), "seconds": st.get("seconds"),
            "found": st.get("found") if st.get("status") == 200 else 0,
            "requested": 50, "duplicates": st.get("duplicates") or 0,
            "shortfall_years": len(st.get("shortfalls") or {}),
            "results_with_topic": len(known),
            "precision_subfield": (sum(s == area["id"] for s, _ in known) / len(known)
                                   if known else None),
            "precision_field": (sum(f == area["field"]["id"] for _, f in known) / len(known)
                                if known else None),
            "out_of_years": sum(1 for p in papers if p.get("year") is not None and y0 and y1
                                and not y0 <= p["year"] <= y1),
            "with_doi": sum(1 for p in papers if p.get("doi")),
        }
        if st["kind"] == "known_item":
            g = by_id.get(st.get("gold_id"))
            r = rank_of(papers, g) if g else None
            row.update(gold_in_scope=int(bool(g) and (g.get("language") or "en")
                                         in SCOPE_LANGUAGES),
                       known_found=int(r is not None), known_rank=r,
                       known_top1=int(r == 1), known_top10=int(r is not None and r <= 10))
            if r is not None:
                checks = metadata_checks(papers[r - 1], g)
                row.update({f"meta_{k}": int(v) for k, v in checks.items()})
        if st["kind"] == "topic":
            wanted = [g for g in in_scope if g.get("query") == st.get("query")]
            if wanted:
                ranks = [rank_of(papers, g) for g in wanted]
                row["gold_for_query"] = len(wanted)
                row["gold_recall_at50"] = sum(r is not None for r in ranks) / len(wanted)
                row["gold_recall_at10"] = sum(r is not None and r <= 10
                                              for r in ranks) / len(wanted)
        if st["kind"] == "language":
            lang = st.get("language")
            row["language_correct"] = (sum(1 for p in papers if p.get("language") == lang)
                                       / len(papers) if papers else None)
        rows.append(row)
    return rows


def bibtex_check(episode: dict) -> dict:
    """Did the export parse, with one entry per paper and no DOI that was not in the results?"""
    step = next((s for s in episode["steps"] if s["kind"] == "export_bibtex"), None)
    if step is None:
        return {"bibtex": "not_tried"}
    if step.get("status") == 422:
        return {"bibtex": "not_available"}
    if step.get("status") != 200:
        return {"bibtex": "failed"}
    try:
        import bibtexparser
    except ImportError:
        return {"bibtex": "not_checked"}
    entries = bibtexparser.loads(step["text"]).entries
    dois = {_doi(e.get("doi")) for e in entries if e.get("doi")}
    expected = {_doi(d) for d in step.get("expected_dois") or [] if d}
    ok = len(entries) == step.get("papers_sent") and dois <= expected
    return {"bibtex": "ok" if ok else "wrong", "bibtex_entries": len(entries),
            "bibtex_extra_dois": len(dois - expected)}


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def scorecard_row(episode: dict, rows: list[dict],
                  doi_checks: dict[str, tuple[bool, bool]]) -> dict:
    """`doi_checks`: DOI -> (exists, added by BAYAN's own Crossref lookup)."""
    area = episode["area"]
    topic = [r for r in rows if r["kind"] == "topic"]
    known = [r for r in rows if r["kind"] == "known_item" and r.get("gold_in_scope")]
    found_known = [r for r in known if r["known_found"]]
    meta = [r[k] for r in found_known for k in r if k.startswith("meta_")]
    special = next((r for r in rows if r["kind"] == "special_query"), {})
    lang = next((r for r in rows if r["kind"] == "language"), {})
    shown = [d for d in doi_checks]
    out = {
        "subfield_id": area["id"], "subfield": area["name"], "field": area["field"]["name"],
        "domain": area["domain"]["name"], "frascati": area["frascati"],
        "searches": len(rows), "errors": sum(1 for r in rows if r["status"] != 200),
        "agent_error": episode.get("error", ""),
        # searches stopped by a library's usage limit: a fact about the test run, not BAYAN
        "upstream_limit": sum(1 for r in rows if "usage limit" in (r.get("error") or "")),
        "latency_median_s": median([r["seconds"] for r in rows if r["seconds"] is not None])
        if rows else None,
        "topic_found_mean": _mean(r["found"] for r in topic),
        "topic_shortfall_rate": _mean(int(r["found"] < r["requested"]) for r in topic),
        "topical_precision_subfield": _mean(r["precision_subfield"] for r in topic),
        "topical_precision_field": _mean(r["precision_field"] for r in topic),
        "gold_recall_at50": _mean(r.get("gold_recall_at50") for r in topic),
        "gold_recall_at10": _mean(r.get("gold_recall_at10") for r in topic),
        "known_items": len(known),
        "known_item_found": _mean(r["known_found"] for r in known),
        "known_item_top1": _mean(r["known_top1"] for r in known),
        "metadata_accuracy": _mean(meta),
        "out_of_years": sum(r["out_of_years"] for r in rows),
        "special_query_ok": int(special.get("status") == 200) if special else None,
        "language_test": lang.get("languages", ""),
        "language_found": lang.get("found"),
        "language_correct": lang.get("language_correct"),
        "dois_checked": len(shown),
        "dois_unresolved": sum(1 for d in shown if not doi_checks[d][0]),
        # a wrong DOI that BAYAN itself added, as opposed to one passed on from a library record
        "dois_unresolved_lookup": sum(1 for d in shown if not doi_checks[d][0]
                                      and doi_checks[d][1]),
        **bibtex_check(episode),
    }
    integrity = [out["out_of_years"] == 0, out["dois_unresolved_lookup"] == 0,
                 out.get("bibtex") in ("ok", "not_available", "not_checked")]
    parts = {
        "known_item_found": out["known_item_found"],
        "topical_precision": out["topical_precision_field"],
        "gold_recall": out["gold_recall_at50"],
        "no_shortfall": None if out["topic_shortfall_rate"] is None
        else 1 - out["topic_shortfall_rate"],
        "no_errors": 1 - out["errors"] / out["searches"] if out["searches"] else 0,
        "metadata": out["metadata_accuracy"],
        "integrity": sum(integrity) / len(integrity),
    }
    # An area is scored only when it was fully tested; a partial test gets no score, so a
    # missing measure can never raise an area's standing.
    missing = [k for k, v in parts.items() if v is None]
    if out["upstream_limit"]:
        missing.append("upstream_limit")
    out["complete"] = int(not missing and not episode.get("error"))
    out["missing_measures"] = ",".join(missing)
    out["reliability_score"] = (round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
                                if out["complete"] else None)
    return out
