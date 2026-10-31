"""Models that learn from bench runs. Simple and explainable first (logistic regression,
gradient-boosted trees), always validated on fields the model did not see (grouped by field).

1. Failure predictor: given a search (field, kind, query form, size, libraries, years), how
   likely is BAYAN to fail it? Failure = HTTP error, a known paper not found, fewer than half
   the papers asked for, or under half of the results in the searched field.
2. Query-form learner: which query form (subfield name, topic name, Boolean) gives the best
   topical precision and fill rate, per field, with the evidence counts behind each choice.
3. Re-ranker: re-orders a topic search's results using result features; judged by nDCG@10 on
   held-out fields against BAYAN's own order. Relevance = the result's OpenAlex topic belongs
   to the searched subfield (OpenAlex results only; labels inherit OpenAlex's classifier).
4. Regression detector: areas whose score or a key measure dropped between two runs.
"""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

FEATURES_CAT = ["domain", "field", "kind", "query_origin", "stratum", "libraries"]
FEATURES_NUM = ["query_words", "query_chars", "boolean", "n_languages", "year_span"]
REGRESSION_POINTS = 5.0
KEY_MEASURES = ("known_item_found", "topical_precision_field", "gold_recall_at50",
                "metadata_accuracy")


def _f(x) -> float | None:
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def failed(row: dict) -> int:
    if row["status"] != "200":
        return 1
    if row["kind"] == "known_item":
        return int(row.get("gold_in_scope") == "1" and row.get("known_found") != "1")
    found = _f(row["found"]) or 0
    if row["kind"] in ("topic", "special_query") and found < 25:
        return 1
    prec = _f(row.get("precision_field"))
    return int(row["kind"] == "topic" and prec is not None and prec < 0.5)


def features(rows: list[dict]) -> tuple[np.ndarray, list[str]]:
    cats = {c: sorted({r[c] for r in rows}) for c in FEATURES_CAT}
    names = [f"{c}={v}" for c in FEATURES_CAT for v in cats[c]] + FEATURES_NUM
    X = np.zeros((len(rows), len(names)))
    index = {n: i for i, n in enumerate(names)}
    for i, r in enumerate(rows):
        for c in FEATURES_CAT:
            X[i, index[f"{c}={r[c]}"]] = 1
        X[i, index["query_words"]] = _f(r["query_words"]) or 0
        X[i, index["query_chars"]] = math.log1p(_f(r["query_chars"]) or 0)
        X[i, index["boolean"]] = _f(r["boolean"]) or 0
        X[i, index["n_languages"]] = len([x for x in r["languages"].split(",") if x])
        span = (_f(r["year_to"]) or 0) - (_f(r["year_from"]) or 0)
        X[i, index["year_span"]] = span
    return X, names


def failure_predictor(rows: list[dict]) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    from sklearn.preprocessing import StandardScaler

    y = np.array([failed(r) for r in rows])
    groups = np.array([r["field_id"] for r in rows])
    X, names = features(rows)
    out = {"searches": len(rows), "failure_rate": float(y.mean()) if len(y) else None}
    if len(set(y)) < 2 or len(set(groups)) < 3:
        out["note"] = "not enough variation to train (need failures, successes and 3+ fields)"
        return out
    folds = GroupKFold(n_splits=min(5, len(set(groups))))
    scores: dict[str, list[float]] = defaultdict(list)
    for train, test in folds.split(X, y, groups):
        if len(set(y[test])) < 2 or len(set(y[train])) < 2:
            continue
        scaler = StandardScaler().fit(X[train])
        lr = LogisticRegression(max_iter=2000, C=0.5).fit(scaler.transform(X[train]), y[train])
        scores["logistic_auc"].append(roc_auc_score(y[test], lr.predict_proba(
            scaler.transform(X[test]))[:, 1]))
        gb = HistGradientBoostingClassifier(max_depth=3, max_iter=200).fit(X[train], y[train])
        scores["boosted_auc"].append(roc_auc_score(y[test], gb.predict_proba(X[test])[:, 1]))
    out.update({k: round(float(np.mean(v)), 3) for k, v in scores.items()})
    out["validation"] = "grouped by field: every fold tests on fields not used in training"
    scaler = StandardScaler().fit(X)
    lr = LogisticRegression(max_iter=2000, C=0.5).fit(scaler.transform(X), y)
    coef = sorted(zip(names, lr.coef_[0], strict=True), key=lambda x: -abs(x[1]))
    out["strongest_signals"] = [{"feature": n, "weight": round(float(w), 3),
                                 "raises_failure": bool(w > 0)} for n, w in coef[:15]]
    # areas ranked by predicted risk, so the weakest get attention first
    risk: dict[str, list[float]] = defaultdict(list)
    for r, p in zip(rows, lr.predict_proba(scaler.transform(X))[:, 1], strict=True):
        risk[f'{r["subfield_id"]} {r["subfield"]}'].append(float(p))
    ranked = sorted(risk.items(), key=lambda x: -np.mean(x[1]))[:20]
    out["highest_risk_areas"] = [{"area": a, "risk": round(float(np.mean(v)), 3)}
                                 for a, v in ranked]
    return out


def query_forms(rows: list[dict]) -> dict:
    """Mean precision and fill per (field, query form), and the best form per field."""
    acc: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r["kind"] not in ("topic", "special_query") or r["status"] != "200":
            continue
        form = "boolean" if r["kind"] == "special_query" else r["query_origin"]
        prec = _f(r.get("precision_field"))
        if prec is None:
            continue
        acc[(r["field"], form)].append((prec, min(1.0, (_f(r["found"]) or 0) / 50)))
    table = [{"field": f, "form": form, "searches": len(v),
              "precision": round(float(np.mean([p for p, _ in v])), 3),
              "fill": round(float(np.mean([q for _, q in v])), 3)}
             for (f, form), v in sorted(acc.items())]
    best = {}
    for row in table:
        value = row["precision"] * row["fill"]
        if row["searches"] >= 3 and value > best.get(row["field"], (None, -1))[1]:
            best[row["field"]] = (row["form"], value)
    return {"table": table,
            "best_form_per_field": {f: {"form": form, "precision_x_fill": round(v, 3)}
                                    for f, (form, v) in sorted(best.items())},
            "note": "forms with fewer than 3 searches in a field are not chosen"}


def _ndcg(labels: list[int], k: int = 10) -> float | None:
    ideal = sorted(labels, reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    if not idcg:
        return None
    return sum(g / math.log2(i + 2) for i, g in enumerate(labels[:k])) / idcg


def ranker(episodes: list[dict], taxonomy: dict) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import GroupKFold

    topics = {a["id"]: set(a.get("all_topics") or []) for a in taxonomy["areas"]}
    lists = []
    for ep in episodes:
        area = ep["area"]
        for st in ep["steps"]:
            if st["kind"] != "topic":
                continue
            papers = [p for p in st.get("papers") or [] if p.get("source") == "openalex"
                      and p.get("topic")]
            if len(papers) < 10:
                continue
            q = set((st.get("query") or "").casefold().split())
            rows = []
            for rank, p in enumerate(papers, 1):
                title = set((p.get("title") or "").casefold().split())
                rows.append(([rank, math.log1p(p.get("citations") or 0), p.get("year") or 0,
                              int(bool(p.get("doi"))), len(q & title) / (len(q) or 1)],
                             int(p["topic"] in topics.get(area["id"], set()))))
            lists.append((area["field"]["id"], rows))
    if len({g for g, _ in lists}) < 3:
        return {"note": "not enough topic searches with OpenAlex topics to train a ranker"}
    groups = np.array([g for g, rows in lists for _ in rows])
    X = np.array([f for _, rows in lists for f, _ in rows], dtype=float)
    y = np.array([lab for _, rows in lists for _, lab in rows])
    owner = np.array([i for i, (_, rows) in enumerate(lists) for _ in rows])
    base, learned = [], []
    for train, test in GroupKFold(n_splits=min(5, len(set(groups)))).split(X, y, groups):
        if len(set(y[train])) < 2:
            continue
        model = HistGradientBoostingClassifier(max_depth=3, max_iter=150).fit(X[train], y[train])
        score = model.predict_proba(X[test])[:, 1]
        for i in sorted(set(owner[test])):
            mask = owner[test] == i
            labels = list(y[test][mask])
            b = _ndcg(labels)
            if b is None:
                continue
            order = np.argsort(-score[mask], kind="stable")
            base.append(b)
            learned.append(_ndcg([labels[j] for j in order]))
    if not base:
        return {"note": "no held-out list had a relevant result"}
    return {"lists": len(base), "ndcg10_bayan_order": round(float(np.mean(base)), 3),
            "ndcg10_reranked": round(float(np.mean(learned)), 3),
            "lists_improved": int(sum(x > y_ + 1e-9 for x, y_ in zip(learned, base, strict=True))),
            "lists_worse": int(sum(x < y_ - 1e-9 for x, y_ in zip(learned, base, strict=True))),
            "features": ["bayan_rank", "log_citations", "year", "has_doi", "title_query_overlap"],
            "validation": "grouped by field: lists are re-ranked by a model that never saw "
                          "their field",
            "relevance": "result's OpenAlex topic is one of the searched subfield's topics"}


def regressions(current: list[dict], previous: list[dict]) -> list[dict]:
    before = {r["subfield_id"]: r for r in previous}
    out = []
    for r in current:
        old = before.get(r["subfield_id"])
        if not old:
            continue
        reasons = []
        a, b = _f(old.get("reliability_score")), _f(r.get("reliability_score"))
        if a is not None and b is not None and a - b >= REGRESSION_POINTS:
            reasons.append(f"score {a} -> {b}")
        for m in KEY_MEASURES:
            x, y = _f(old.get(m)), _f(r.get(m))
            if x is not None and y is not None and x - y >= 0.1:
                reasons.append(f"{m} {x:.2f} -> {y:.2f}")
        if reasons:
            out.append({"subfield_id": r["subfield_id"], "subfield": r["subfield"],
                        "reasons": "; ".join(reasons)})
    return out


def train(run_dir: Path, taxonomy: dict, previous: Path | None = None) -> dict:
    rows = read_csv(run_dir / "searches.csv")
    episodes = [json.loads(p.read_text()) for p in sorted((run_dir / "raw" / "episodes")
                                                           .glob("*.json"))]
    result = {"failure_predictor": failure_predictor(rows), "query_forms": query_forms(rows),
              "ranker": ranker(episodes, taxonomy)}
    if previous and (previous / "scorecard.csv").exists():
        result["regressions"] = regressions(read_csv(run_dir / "scorecard.csv"),
                                            read_csv(previous / "scorecard.csv"))
        result["compared_with"] = previous.name
    out = run_dir / "ml"
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    return result
