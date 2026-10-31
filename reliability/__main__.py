"""python -m reliability <command>: see reliability/__init__.py."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import gold, ml, report, runner, taxonomy
from .client import Client

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = ROOT / "cache"
RESULTS = ROOT / "results"


def client(args) -> Client:
    # OPENALEX_API_KEY is optional; it is read from the environment or .env and never logged.
    return Client(CACHE, openalex_key=(os.environ.get("OPENALEX_API_KEY")
                                              or dotenv_value("OPENALEX_API_KEY") or None),
                  offline=getattr(args, "offline", False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reliability")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("taxonomy", help="build data/taxonomy.json from OpenAlex")
    t.add_argument("--queries", type=int, default=taxonomy.QUERIES_PER_SUBFIELD)
    g = sub.add_parser("gold", help="build the gold set (N verified papers per subfield)")
    g.add_argument("--per-area", type=int, default=50)
    g.add_argument("--from-year", type=int, default=2006)
    g.add_argument("--to-year", type=int, default=2025)
    g.add_argument("--only", nargs="*", help="subfield ids, e.g. 2104 1210")
    g.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("run", help="test agents drive a live BAYAN; writes results/<run_id>/")
    r.add_argument("--only", nargs="*", help="subfield ids")
    r.add_argument("--domain", help="only the subfields of one OpenAlex domain id (1-4)")
    r.add_argument("--run-id")
    r.add_argument("--base-url", help="use a running BAYAN instead of starting one")
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--per-search", type=int, default=50)
    r.add_argument("--topic-queries", type=int, default=3)
    r.add_argument("--known-items", type=int, default=10)
    r.add_argument("--from-year", type=int, default=2006)
    r.add_argument("--to-year", type=int, default=2025)
    r.add_argument("--doi-lookup-budget", type=int, default=20)
    mg = sub.add_parser("merge", help="join runs of parts of the taxonomy into one run")
    mg.add_argument("--run-id", required=True, help="the joined run")
    mg.add_argument("parts", nargs="+", help="run ids of the parts")
    m = sub.add_parser("train", help="train the models on a run")
    m.add_argument("--run-id", help="default: the latest run")
    m.add_argument("--previous", help="run id to compare with (regression detector)")
    rp = sub.add_parser("report", help="write REPORT.md for a run")
    rp.add_argument("--run-id", help="default: the latest run")
    pb = sub.add_parser("publish", help="put a run's summary on BAYAN's reliability page")
    pb.add_argument("--run-id", help="default: the latest run")
    for p in (t, g):
        p.add_argument("--offline", action="store_true", help="use only the cache")
    args = ap.parse_args(argv)

    if args.cmd == "taxonomy":
        tax = taxonomy.build(client(args), args.queries)
        taxonomy.save(tax, DATA / "taxonomy.json")
        print(f"taxonomy: {tax['counts']}")
    elif args.cmd == "gold":
        tax = taxonomy.load(DATA / "taxonomy.json")
        meta = gold.build(client(args), tax, DATA / "gold", args.from_year, args.to_year,
                          args.per_area, args.only, args.workers)
        print(f"gold: {meta['kept']} papers in {meta['areas']} areas; "
              f"failed areas: {meta['failed_areas'] or 'none'}")
    elif args.cmd == "run":
        tax = taxonomy.load(DATA / "taxonomy.json")
        env = {"BAYAN_DOI_LOOKUP_BUDGET": str(args.doi_lookup_budget)}
        key = os.environ.get("OPENALEX_API_KEY") or dotenv_value("OPENALEX_API_KEY")
        if key:
            env["OPENALEX_API_KEY"] = key
        only = args.only
        if args.domain:
            only = [a["id"] for a in tax["areas"]
                    if str(a["domain"]["id"]).rsplit("/", 1)[-1] == args.domain
                    and (not only or a["id"] in only)]
            if not only:  # an empty list would mean "every area" to the runner
                raise SystemExit(f"no subfields in domain {args.domain}")
        run_dir = runner.run(
            tax, DATA / "gold", RESULTS, ROOT.parent, client(args), run_id=args.run_id,
            only=only, base_url=args.base_url, workers=args.workers,
            cfg_overrides={"per_search": args.per_search, "topic_queries": args.topic_queries,
                           "known_items": args.known_items, "year_from": args.from_year,
                           "year_to": args.to_year},
            env_extra=env)
        print(f"run written to {run_dir}")
    elif args.cmd == "merge":
        run_dir = runner.merge(RESULTS, args.run_id, args.parts)
        print(f"merged {len(args.parts)} parts into {run_dir}")
    elif args.cmd == "train":
        run_dir = pick_run(args.run_id)
        prev = RESULTS / args.previous if args.previous else None
        res = ml.train(run_dir, taxonomy.load(DATA / "taxonomy.json"), prev)
        fp = res["failure_predictor"]
        print(f"failure rate {fp.get('failure_rate')}; AUC unseen fields: "
              f"{fp.get('logistic_auc')} / {fp.get('boosted_auc')}; ranker {res['ranker']}")
    elif args.cmd == "report":
        run_dir = pick_run(args.run_id)
        summary = DATA / "gold" / "summary.json"
        path = report.write(run_dir, json.loads(summary.read_text()) if summary.exists()
                            else None)
        print(f"report: {path}")
    elif args.cmd == "publish":
        target = ROOT.parent / "bayan" / "static" / "reliability.json"
        summary = report.publish(pick_run(args.run_id), target, gold_summary())
        print(f"published run {summary['run_id']} to {target}")
    return 0


def gold_summary() -> dict | None:
    path = DATA / "gold" / "summary.json"
    return json.loads(path.read_text()) if path.exists() else None


def pick_run(run_id: str | None) -> Path:
    if run_id:
        return RESULTS / run_id
    runs = sorted(p for p in RESULTS.iterdir() if (p / "run.json").exists())
    if not runs:
        raise SystemExit("no runs yet: python -m reliability run")
    return runs[-1]


def dotenv_value(name: str) -> str | None:
    """A value from the repository's .env, if there is one (the key is never printed)."""
    for env in (ROOT.parent / ".env", Path.home() / "brilliant-research-agent" / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                    return line.split("=", 1)[1].strip()
    return None


if __name__ == "__main__":
    sys.exit(main())
