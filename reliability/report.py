"""REPORT.md for one run: reliability per field, top failures, model results, and a prioritised
improvement backlog in which every item cites the numbers that motivate it."""

import json
from collections import defaultdict
from pathlib import Path

from .ml import _f, read_csv

NOT_IMPLEMENTED = [
    ("Citation styles (APA 7, MLA 9, Chicago 17, IEEE, Vancouver)",
     "BAYAN exports BibTeX and RIS; reference managers format the styles. No in-app styles."),
    ("Saved searches", "Runs are kept per user, but there is no named, re-runnable saved search."),
    ("Zotero/Mendeley round trip", "BibTeX is checked by a parser here; importing into the real "
                                   "programs was not automated."),
]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _num(x, nd=1) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def group(cards: list[dict], key: str) -> list[dict]:
    by = defaultdict(list)
    for c in cards:
        by[c[key]].append(c)
    out = []
    for name, cs in by.items():
        scored = [_f(c["reliability_score"]) for c in cs]
        out.append({"name": name, "areas": len(cs),
                    "complete": sum(c.get("complete") == "1" for c in cs),
                    "score": _mean(scored),
                    "known": _mean(_f(c["known_item_found"]) for c in cs),
                    "precision": _mean(_f(c["topical_precision_field"]) for c in cs),
                    "recall": _mean(_f(c["gold_recall_at50"]) for c in cs),
                    "shortfall": _mean(_f(c["topic_shortfall_rate"]) for c in cs),
                    "meta": _mean(_f(c["metadata_accuracy"]) for c in cs)})
    return sorted(out, key=lambda r: (r["score"] is None, -(r["score"] or 0)))


def backlog(cards: list[dict], rows: list[dict], ml: dict) -> list[str]:
    items = []
    searches = len(rows)
    errors = [r for r in rows if r["status"] != "200"]
    if errors:
        kinds = defaultdict(int)
        for r in errors:
            kinds[(r["status"], (r["error"] or "")[:60])] += 1
        top = ", ".join(f"HTTP {s} \"{e}\" x{n}" for (s, e), n in
                        sorted(kinds.items(), key=lambda x: -x[1])[:3])
        items.append((len(errors) / max(1, searches) * 100,
                      f"**Fix failing searches**: {len(errors)} of {searches} searches failed "
                      f"({_pct(len(errors) / max(1, searches))}). Most common: {top}."))
    known = [r for r in rows if r["kind"] == "known_item" and r.get("gold_in_scope") == "1"]
    miss = [r for r in known if r.get("known_found") != "1"]
    if known and miss:
        by_stratum = defaultdict(lambda: [0, 0])
        for r in known:
            by_stratum[r["stratum"]][0] += 1
            by_stratum[r["stratum"]][1] += r.get("known_found") != "1"
        detail = ", ".join(f"{s} {m}/{n} missed" for s, (n, m) in sorted(by_stratum.items()))
        items.append((len(miss) / len(known) * 80,
                      f"**Find papers by title more reliably**: {len(miss)} of {len(known)} "
                      f"known papers typed by title were not found ({detail}). Add a title/DOI "
                      "lookup path (exact-title and DOI search before the topic search)."))
    weak = [c for c in cards if _f(c["topical_precision_field"]) is not None
            and _f(c["topical_precision_field"]) < 0.5]
    if weak:
        names = ", ".join(c["subfield"] for c in sorted(
            weak, key=lambda c: _f(c["topical_precision_field"]))[:6])
        items.append((len(weak) / max(1, len(cards)) * 70,
                      f"**Keep searches inside the field**: in {len(weak)} areas fewer than half "
                      f"of the results belong to the searched field (worst: {names}). Offer the "
                      "planner's OpenAlex topic filter by default for these fields."))
    short = [c for c in cards if (_f(c["topic_shortfall_rate"]) or 0) > 0]
    if short:
        items.append((len(short) / max(1, len(cards)) * 50,
                      f"**Explain shortfalls**: {len(short)} areas could not fill 50 papers in "
                      "at least one search; the per-year shortfall is reported, but suggest a "
                      "broader query or more libraries when it happens."))
    lang = [c for c in cards if _f(c["language_correct"]) is not None]
    bad_lang = [c for c in lang if _f(c["language_correct"]) < 0.8]
    if bad_lang:
        items.append((len(bad_lang) / max(1, len(lang)) * 40,
                      f"**Language filter**: in {len(bad_lang)} of {len(lang)} single-language "
                      "searches under 80% of results were in the requested language."))
    unresolved = sum(int(c["dois_unresolved"] or 0) for c in cards)
    ours = sum(int(c.get("dois_unresolved_lookup") or 0) for c in cards)
    checked = sum(int(c["dois_checked"] or 0) for c in cards)
    if ours:
        items.append((95, f"**Wrong DOIs added by BAYAN**: {ours} DOIs that BAYAN's Crossref "
                          "lookup filled in are not registered anywhere. Raise the title-match "
                          "threshold or require an author match."))
    if unresolved - ours:
        items.append((45, f"**Broken DOIs from library records**: {unresolved - ours} of "
                          f"{checked} DOIs shown came from the library's own record but are not "
                          "registered at doi.org. Check DOIs before showing them and mark "
                          "unregistered ones instead of linking to a dead page."))
    outside = sum(int(c["out_of_years"] or 0) for c in cards)
    if outside:
        items.append((85, f"**Year filter**: {outside} results were outside the requested years."))
    bib = defaultdict(int)
    for c in cards:
        bib[c.get("bibtex", "")] += 1
    if bib.get("wrong") or bib.get("failed"):
        items.append((80, f"**BibTeX export**: {bib.get('wrong', 0)} wrong and "
                          f"{bib.get('failed', 0)} failed exports."))
    if bib.get("not_available"):
        items.append((30, f"**BibTeX export missing in this build**: {bib['not_available']} areas "
                          "could not export BibTeX (the BAYAN commit tested predates it)."))
    lat = sorted(_f(r["seconds"]) for r in rows if _f(r["seconds"]) is not None)
    if lat:
        p95 = lat[int(0.95 * (len(lat) - 1))]
        if p95 > 30:
            items.append((min(60, p95), f"**Speed**: 95% of searches finished within "
                                        f"{p95:.0f} s; the slowest took {lat[-1]:.0f} s. "
                                        "Cache library pages and stream results to the page."))
    items.append((20, "**arXiv pacing across users**: each search paces its own arXiv pages "
                      "(3 s), but parallel searches are not paced together, so many users at "
                      "once can exceed arXiv's one request every 3 s. The bench ran arXiv "
                      "searches one at a time for this reason. Add a shared per-host limiter."))
    rk = ml.get("ranker") or {}
    if rk.get("ndcg10_reranked") and rk.get("ndcg10_bayan_order") \
            and rk["ndcg10_reranked"] > rk["ndcg10_bayan_order"]:
        items.append((35, f"**Result order**: a re-ranker trained on other fields raised nDCG@10 "
                          f"from {rk['ndcg10_bayan_order']} to {rk['ndcg10_reranked']} on "
                          f"{rk['lists']} held-out result lists."))
    return [text for _, text in sorted(items, key=lambda x: -x[0])]


def write(run_dir: Path, gold_meta: dict | None = None) -> Path:
    cards = read_csv(run_dir / "scorecard.csv")
    rows = read_csv(run_dir / "searches.csv")
    meta = json.loads((run_dir / "run.json").read_text())
    ml_path = run_dir / "ml" / "results.json"
    ml = json.loads(ml_path.read_text()) if ml_path.exists() else {}
    complete = [c for c in cards if c.get("complete") == "1"]
    L = [f"# BAYAN reliability report, run {meta['run_id']}", ""]
    cfg = meta["config"]
    L += [f"BAYAN commit `{meta['bayan_commit']}`; {meta['scored']} of {meta['areas']} subfields "
          f"scored, {len(complete)} fully tested; {meta['searches']} searches in "
          f"{meta['minutes']} minutes. Period {cfg['year_from']}-{cfg['year_to']}, "
          f"{cfg['per_search']} papers per search, {cfg['known_items']} known papers per area.",
          ""]
    if gold_meta:
        L += [f"Gold set: {gold_meta['kept']} verified papers over {gold_meta['areas']} "
              f"subfields (period {gold_meta['period'][0]}-{gold_meta['period'][1]}, "
              f"{gold_meta['per_area']} per area).", ""]
    L += ["## How to read this", "",
          "- **Score** (0-100) = 30 known papers found by title + 20 results in the field + 15 "
          "gold recall + 10 no shortfall + 10 no errors + 10 metadata correct + 5 integrity "
          "(years, DOIs, BibTeX). Only fully tested areas get a score.",
          "- Gold papers and field labels come from OpenAlex, which BAYAN also searches, so "
          "these numbers test BAYAN's queries, merging and ranking more than library coverage. "
          "Field labels are OpenAlex's automatic classification and are imperfect.",
          "- Results in other languages than BAYAN's six are out of scope and not counted.", ""]
    for key, title in (("frascati", "By OECD Frascati group"), ("domain", "By OpenAlex domain"),
                       ("field", "By field")):
        L += [f"## {title}", "", "| Group | Areas (complete) | Score | Known found | In field | "
              "Recall@50 | Shortfall | Metadata |", "|---|---|---|---|---|---|---|---|"]
        for g in group(cards, key):
            L.append(f"| {g['name']} | {g['areas']} ({g['complete']}) | {_num(g['score'])} | "
                     f"{_pct(g['known'])} | {_pct(g['precision'])} | {_pct(g['recall'])} | "
                     f"{_pct(g['shortfall'])} | {_pct(g['meta'])} |")
        L.append("")
    worst = sorted(complete, key=lambda c: _f(c["reliability_score"]))[:15]
    L += ["## Weakest areas", "", "| Area | Field | Score | Known found | In field | Errors |",
          "|---|---|---|---|---|---|"]
    for c in worst:
        L.append(f"| {c['subfield']} | {c['field']} | {c['reliability_score']} | "
                 f"{_pct(_f(c['known_item_found']))} | "
                 f"{_pct(_f(c['topical_precision_field']))} | {c['errors']} |")
    incomplete = [c for c in cards if c.get("complete") != "1"]
    if incomplete:
        L += ["", f"Not fully tested ({len(incomplete)}): " + "; ".join(
            f"{c['subfield']} ({c.get('missing_measures') or c.get('agent_error') or '?'})"
            for c in incomplete[:30])]
    L += ["", "## Prioritised improvements", ""]
    L += [f"{i}. {t}" for i, t in enumerate(backlog(cards, rows, ml), 1)]
    if ml:
        fp, qf, rk = ml.get("failure_predictor", {}), ml.get("query_forms", {}), ml.get("ranker",
                                                                                         {})
        L += ["", "## What the models learned", "",
              f"- **Failure predictor**: failure rate {_pct(fp.get('failure_rate'))} of "
              f"{fp.get('searches')} searches; AUC on unseen fields: logistic "
              f"{fp.get('logistic_auc', 'n/a')}, boosted trees {fp.get('boosted_auc', 'n/a')}."
              + (f" {fp['note']}." if fp.get("note") else "")]
        for s in fp.get("strongest_signals", [])[:8]:
            L.append(f"  - {s['feature']}: {'raises' if s['raises_failure'] else 'lowers'} "
                     f"failure risk (weight {s['weight']})")
        best = qf.get("best_form_per_field", {})
        if best:
            L.append("- **Best query form per field** (precision x fill): " + "; ".join(
                f"{f}: {v['form']} ({v['precision_x_fill']})" for f, v in best.items()))
        if rk.get("lists"):
            L.append(f"- **Re-ranker**: nDCG@10 {rk['ndcg10_bayan_order']} (BAYAN order) -> "
                     f"{rk['ndcg10_reranked']} (re-ranked) on {rk['lists']} held-out lists; "
                     f"{rk['lists_improved']} better, {rk['lists_worse']} worse.")
        elif rk.get("note"):
            L.append(f"- **Re-ranker**: {rk['note']}.")
        if "regressions" in ml:
            reg = ml["regressions"]
            L.append(f"- **Regressions** against run {ml['compared_with']}: {len(reg)} areas"
                     + (": " + "; ".join(f"{r['subfield']} ({r['reasons']})"
                                         for r in reg[:10]) if reg else "."))
    L += ["", "## Not tested because BAYAN does not have it yet", ""]
    L += [f"- **{a}**: {b}" for a, b in NOT_IMPLEMENTED]
    path = run_dir / "REPORT.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


def publish(run_dir: Path, path: Path, gold_meta: dict | None = None) -> dict:
    """A compact, public summary of one run for BAYAN's reliability page."""
    cards = read_csv(run_dir / "scorecard.csv")
    rows = read_csv(run_dir / "searches.csv")
    meta = json.loads((run_dir / "run.json").read_text())
    ml_path = run_dir / "ml" / "results.json"
    ml = json.loads(ml_path.read_text()) if ml_path.exists() else {}

    def slim(g: dict) -> dict:
        return {"name": g["name"], "areas": g["areas"], "complete": g["complete"],
                **{k: (round(g[k], 3) if g[k] is not None else None)
                   for k in ("score", "known", "precision", "recall", "shortfall", "meta")}}

    summary = {
        "run_id": meta["run_id"], "date": meta["started"][:10],
        "bayan_commit": meta["bayan_commit"], "period": [meta["config"]["year_from"],
                                                         meta["config"]["year_to"]],
        "areas_tested": meta["scored"], "areas_complete": sum(c.get("complete") == "1"
                                                              for c in cards),
        "searches": meta["searches"], "gold_papers": (gold_meta or {}).get("kept"),
        "by_group": [slim(g) for g in group(cards, "frascati")],
        "by_field": [slim(g) for g in group(cards, "field")],
        "improvements": backlog(cards, rows, ml)[:8],
        "limits": ["Gold papers and field labels come from OpenAlex, which BAYAN also searches.",
                   "Only BAYAN's six languages are counted.",
                   "Each area was tested by scripted agents, not by people."],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return summary
