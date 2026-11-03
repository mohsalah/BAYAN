"""Geometry-bank literature search: BAYAN finds sources for each reactor / shielding design.

Starts a fresh BAYAN (as the reliability bench does), runs one search per design on OpenAlex,
and writes geometry/results/<run-id>/results.json: for each design the papers BAYAN returned
(title, year, DOI, venue, citations). These lists are compared with the sources that research
agents used for the same designs. Only public bibliographic metadata is fetched.

Usage: python -m geometry.run --run-id gh-123 [--per-search 25] [--only id1,id2]
"""

import argparse
import json
import time
from pathlib import Path

import httpx

from reliability.agents import PASSWORD, slim
from reliability.runner import start_server

HERE = Path(__file__).resolve().parent
LIBRARIES = ["openalex"]  # design reports are rarely on arXiv, and it rate-limits hard


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m geometry.run")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--per-search", type=int, default=25)
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    designs = json.loads((HERE / "designs.json").read_text())
    if args.only:
        keep = set(args.only.split(","))
        designs = [d for d in designs if d["id"] in keep]
    out = HERE / "results" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    proc, url = start_server(out, HERE.parent, {"BAYAN_DOI_LOOKUP_BUDGET": "0"})
    rows = []
    try:
        http = httpx.Client(base_url=url, timeout=300)
        for i, d in enumerate(designs):
            if i % 15 == 0:  # BAYAN allows 20 searches per user per hour: a new user per batch
                http.cookies.clear()
                email = f"geometry-{args.run_id}-{i}@bench.invalid"
                http.post("/api/auth/register",
                          json={"email": email, "password": PASSWORD}).raise_for_status()
            types = ["peer_reviewed", "proceedings", "thesis", "report", "other"]
            profile = {"query": d["query"], "year_from": 1950, "year_to": 2026,
                       "document_types": types, "languages": ["en"]}
            t0 = time.monotonic()
            row = {"id": d["id"], "query": d["query"], "papers": [], "error": None}
            try:
                r = http.post("/api/search", json={"profile": profile, "libraries": LIBRARIES})
                r.raise_for_status()
                data = r.json()
                keys = ("found", "scanned", "by_library", "stopped", "warnings")
                row["summary"] = {k: (data.get("summary") or {}).get(k) for k in keys}
                detail = http.get(f"/api/runs/{data.get('id')}")
                detail.raise_for_status()
                papers = detail.json().get("papers") or []
                row["papers"] = [slim(p) for p in papers][: args.per_search]
            except httpx.HTTPError as exc:
                body = getattr(getattr(exc, "response", None), "text", "")
                row["error"] = f"{type(exc).__name__}: {body[:300] or str(exc)[:200]}"
            row["seconds"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            print(f"{d['id']}: {len(row['papers'])} papers {row['error'] or ''}", flush=True)
    finally:
        proc.terminate()
    (out / "results.json").write_text(json.dumps({"run_id": args.run_id, "libraries": LIBRARIES,
                                                  "designs": rows}, indent=1))
    return 0 if any(r["papers"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
