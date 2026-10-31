"""Run the agents against a live BAYAN and write the results of one run.

The bench starts its own BAYAN (uvicorn, a fresh database under the run folder, the real
library connectors) unless --base-url points to a server that is already running. Output in
results/<run_id>/:
  run.json        configuration, versions, timings, counts
  searches.csv    one row per search (the models' training data)
  scorecard.csv   one row per subfield
  raw/            full episodes and the server log (not committed)
"""

import csv
import json
import os
import random
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from . import gold as gold_mod
from .agents import AgentConfig, AreaAgent
from .client import Client, FetchError
from .metrics import scorecard_row, search_rows, topic_map

DOIS_PER_AREA = 10


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(run_dir: Path, repo: Path, env_extra: dict) -> tuple[subprocess.Popen, str]:
    port = free_port()
    raw = run_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BAYAN_")}
    env.update({"BAYAN_DATABASE_PATH": str(raw / "app.sqlite"),
                "BAYAN_PDF_ROOT": str(raw / "pdfs"), "BAYAN_SEARCHES_PER_HOUR": "1000000",
                "BAYAN_JOB_RESUME_INTERVAL": "0", "PYTHONPATH": str(repo), **env_extra})
    log = (raw / "server.log").open("w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "bayan.api:app", "--host",
                             "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=raw, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/api/libraries", timeout=2).status_code < 500:
                return proc, url
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"BAYAN did not start; see {raw / 'server.log'}")


def doi_exists(client: Client, doi: str) -> bool:
    try:
        body = client.get_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
        if body and body.get("message"):
            return True
        handle = client.get_json(f"https://doi.org/api/handles/{quote(doi, safe='/')}")
        return bool(handle and handle.get("responseCode") == 1)
    except FetchError:
        return True  # a failed check is not evidence of a made-up DOI; it is counted apart


def shown_dois(episode: dict, seed: str) -> list[str]:
    dois = list(dict.fromkeys(p["doi"].lower() for st in episode["steps"]
                              if st["kind"] == "topic" for p in st.get("papers") or []
                              if p.get("doi")))
    random.Random(seed).shuffle(dois)
    return dois[:DOIS_PER_AREA]


def git_sha(repo: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def write_csv(path: Path, rows: list[dict]) -> None:
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def run(taxonomy: dict, gold_dir: Path, results: Path, repo: Path, client: Client, *,
        run_id: str | None = None, only: list[str] | None = None, base_url: str | None = None,
        workers: int = 6, cfg_overrides: dict | None = None, env_extra: dict | None = None,
        log=print) -> Path:
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = results / run_id
    (run_dir / "raw" / "episodes").mkdir(parents=True, exist_ok=True)
    areas = [a for a in taxonomy["areas"] if not only or a["id"] in only]
    proc = None
    if base_url is None:
        proc, base_url = start_server(run_dir, repo, env_extra or {})
    cfg = AgentConfig(base_url=base_url, run_id=run_id, **(cfg_overrides or {}))
    topics = topic_map(taxonomy)
    t0 = time.time()
    rows: list[dict] = []
    cards: list[dict] = []
    try:
        def one(area: dict) -> tuple[list[dict], dict]:
            gold = gold_mod.load(gold_dir, area["id"])
            ep = AreaAgent(cfg, area, gold).run().to_dict()
            (run_dir / "raw" / "episodes" / f"{area['id']}.json").write_text(
                json.dumps(ep, ensure_ascii=False))
            r = search_rows(ep, gold, topics)
            lookup = {d.lower() for st in ep["steps"] for d in st.get("doi_matches") or []}
            checks = {d: (doi_exists(client, d), d in lookup)
                      for d in shown_dois(ep, f"{run_id}{area['id']}")}
            return r, scorecard_row(ep, r, checks)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, a): a for a in areas}
            for n, fut in enumerate(as_completed(futures), 1):
                area = futures[fut]
                try:
                    r, card = fut.result()
                except Exception as exc:
                    log(f"[{n}/{len(areas)}] {area['id']} {area['name']}: bench error {exc}")
                    continue
                rows += r
                cards.append(card)
                log(f"[{n}/{len(areas)}] {area['id']} {area['name']}: "
                    f"score {card['reliability_score']}")
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=20)
    cards.sort(key=lambda c: int(c["subfield_id"]))
    write_csv(run_dir / "searches.csv", rows)
    write_csv(run_dir / "scorecard.csv", cards)
    meta = {"run_id": run_id, "started": datetime.fromtimestamp(t0, UTC).isoformat(),
            "minutes": round((time.time() - t0) / 60, 1), "bayan_commit": git_sha(repo),
            "base_url": "own server" if proc else "external", "areas": len(areas),
            "scored": len(cards), "searches": len(rows),
            "config": {k: v for k, v in cfg.__dict__.items() if k != "base_url"},
            "server_env": {k: v for k, v in (env_extra or {}).items() if "KEY" not in k}}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    return run_dir


def merge(results: Path, run_id: str, parts: list[str]) -> Path:
    """Join runs over disjoint parts of the taxonomy (e.g. one cloud job per domain)."""
    run_dir = results / run_id
    episodes = run_dir / "raw" / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    cards: list[dict] = []
    metas = []
    for part in parts:
        src = results / part
        metas.append(json.loads((src / "run.json").read_text()))
        for name, acc in (("searches.csv", rows), ("scorecard.csv", cards)):
            if (src / name).exists():
                with (src / name).open(encoding="utf-8") as f:
                    acc += list(csv.DictReader(f))
        for ep in (src / "raw" / "episodes").glob("*.json"):
            if (episodes / ep.name).exists():
                raise ValueError(f"area {ep.stem} is in more than one part")
            (episodes / ep.name).write_bytes(ep.read_bytes())
    configs = {json.dumps(m["config"] | {"run_id": ""}, sort_keys=True) for m in metas}
    if len(configs) > 1:
        raise ValueError("parts were run with different settings; they cannot be joined")
    cards.sort(key=lambda c: int(c["subfield_id"]))
    write_csv(run_dir / "searches.csv", rows)
    write_csv(run_dir / "scorecard.csv", cards)
    meta = {"run_id": run_id, "started": min(m["started"] for m in metas),
            "minutes": max(m["minutes"] for m in metas), "bayan_commit": metas[0]["bayan_commit"],
            "base_url": metas[0]["base_url"], "areas": sum(m["areas"] for m in metas),
            "scored": len(cards), "searches": len(rows),
            "config": metas[0]["config"] | {"run_id": run_id},
            "server_env": metas[0]["server_env"], "parts": parts}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    return run_dir
