"""Test agents: each one is a researcher in one subfield using a live BAYAN over HTTP.

An agent signs up, then works the way a researcher in its field would:
1. topic searches with its field's own vocabulary (OpenAlex topic names, the subfield name)
   for N papers over the period, with the libraries that field would pick;
2. known-item searches: it types the title of real papers it knows (from the gold set) and
   checks that BAYAN finds them, at what rank, and with correct metadata;
3. exports the first results as BibTeX and checks the file (parses, nothing added);
4. robustness: a Boolean query with quotes, and a search in another interface language.
Every step is logged with its timing, HTTP status and the papers returned. Nothing is scored
here; metrics.py scores the episodes.

The agents are scripted, not LLM-driven, so runs are reproducible and cost nothing.
"""

import random
import threading
import time
from dataclasses import dataclass, field

import httpx

PASSWORD = "reliability-agent-pass-1"
LANG_ROTATION = ("fr", "de", "es", "zh", "ar")
# Libraries a researcher in each OpenAlex domain would add to OpenAlex.
DOMAIN_LIBRARIES = {"Physical Sciences": ["openalex", "arxiv"],
                    "Life Sciences": ["openalex", "europepmc"],
                    "Health Sciences": ["openalex", "europepmc"],
                    "Social Sciences": ["openalex"]}
ARXIV_GATE = threading.Semaphore(1)  # arXiv asks for one request every 3 s: one search at a time


@dataclass
class AgentConfig:
    base_url: str
    year_from: int = 2006
    year_to: int = 2025
    per_search: int = 50
    topic_queries: int = 2
    gold_topics: int = 1  # extra searches for topics of gold papers not otherwise searched
    known_items: int = 6
    export_n: int = 10
    seed: int = 1
    run_id: str = "run"
    timeout: float = 300.0


@dataclass
class Episode:
    area: dict
    steps: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self) -> dict:
        return {"area": {k: self.area[k] for k in ("id", "name", "field", "domain", "frascati")},
                "started": self.started, "error": self.error, "steps": self.steps}


def slim(paper: dict) -> dict:
    keep = ("source", "id", "doi", "title", "year", "first_author", "authors", "language",
            "topic", "category", "native_type", "citations", "venue")
    return {k: paper.get(k) for k in keep if k in paper}


def special_query(area: dict) -> str:
    """A Boolean query with quoted phrases, brackets, AND and OR, built from the topic's
    keywords: ("k1" OR "k2") AND ("k3" OR "k4").

    Stage 1 used the topic label as the quoted phrase ("Botany and Plant Ecology
    Studies"); labels are OpenAlex's names, not words papers use, and 16 of 77 areas got no
    results. Keywords are words papers use; this form found results in all 77 areas."""
    q = area["queries"][0] if area["queries"] else {"query": area["name"], "keywords": []}
    kws = [k.get("display_name") if isinstance(k, dict) else k
           for k in q.get("keywords") or []]
    clean = {}
    for k in kws:
        k = (k or "").replace('"', "").strip()
        if k:
            clean.setdefault(k.casefold(), k)  # "Coleoptera" and "coleoptera" are one term
    kws = [f'"{k}"' for k in clean.values()][:4]
    if len(kws) >= 4:
        return f"({kws[0]} OR {kws[1]}) AND ({kws[2]} OR {kws[3]})"
    if len(kws) >= 2:
        return f'{kws[0]} AND ({" OR ".join(kws[1:])})'
    head = kws[0] if kws else '"' + q["query"].replace('"', "") + '"'
    name = area["name"].replace('"', "")
    return f'{head} AND ("{name}")'


class AreaAgent:
    def __init__(self, cfg: AgentConfig, area: dict, gold: list[dict],
                 http: httpx.Client | None = None):
        self.cfg, self.area, self.gold = cfg, area, gold
        self.libraries = DOMAIN_LIBRARIES.get(area["domain"]["name"], ["openalex"])
        # `http` lets tests drive an in-process app (a TestClient is an httpx.Client)
        self._own_http = http is None
        self.http = http or httpx.Client(base_url=cfg.base_url, timeout=cfg.timeout)
        self.ep = Episode(area)

    # ------------------------------------------------------------ steps

    def _step(self, kind: str, method: str, url: str, **kw) -> tuple[dict, httpx.Response | None]:
        step = {"kind": kind, "url": url, **{k: v for k, v in kw.pop("log", {}).items()}}
        t0 = time.monotonic()
        try:
            resp = self.http.request(method, url, **kw)
            step["status"] = resp.status_code
        except httpx.HTTPError as exc:
            resp = None
            step["status"] = 0
            step["error"] = type(exc).__name__
        step["seconds"] = round(time.monotonic() - t0, 3)
        if resp is not None and resp.status_code >= 400:
            try:
                step["error"] = str(resp.json().get("detail"))[:300]
            except ValueError:
                step["error"] = resp.text[:300]
        self.ep.steps.append(step)
        return step, resp

    def sign_up(self) -> bool:
        email = f"agent-{self.cfg.run_id}-{self.area['id']}@reliability.invalid".lower()
        step, resp = self._step("sign_up", "POST", "/api/auth/register",
                                json={"email": email, "password": PASSWORD})
        return resp is not None and resp.status_code in (200, 201)

    def search(self, kind: str, query: str, *, years: tuple[int, int] | None = None,
               languages: list[str] | None = None, libraries: list[str] | None = None,
               extra: dict | None = None, quota: bool = True) -> dict:
        y0, y1 = years or (self.cfg.year_from, self.cfg.year_to)
        libs = libraries or self.libraries
        profile = {"query": query, "year_from": y0, "year_to": y1,
                   "document_types": ["peer_reviewed", "proceedings", "thesis", "report",
                                      "other"],
                   "languages": languages or ["en", "ar", "fr", "zh", "de", "es"]}
        # A title search leaves "Papers wanted" empty, as the search form does by default: the
        # per-year spread made BAYAN scan ~2,000 records (10 OpenAlex pages) to fill years a
        # single paper's hits never reach.
        body = {"profile": profile, "libraries": libs}
        if quota:
            body["quota_total"] = self.cfg.per_search
        log = {"query": query, "years": [y0, y1], "libraries": libs,
               "languages": profile["languages"], "quota": body.get("quota_total"),
               **(extra or {})}
        gate = ARXIV_GATE if "arxiv" in libs else None
        if gate:
            gate.acquire()
        try:
            step, resp = self._step(kind, "POST", "/api/search", json=body, log=log)
        finally:
            if gate:
                gate.release()
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            summary = data.get("summary") or {}
            step["run_id"] = data.get("id")
            step["found"] = summary.get("found")
            step["by_library"] = summary.get("by_library")
            step["shortfalls"] = summary.get("shortfalls")
            step["warnings"] = (summary.get("warnings") or [])[:5]
            step["duplicates"] = summary.get("duplicates")
            step["scanned"] = summary.get("scanned")  # records read: the credit cost driver
            step["stopped"] = summary.get("stopped")
            # filters a library could not apply (BAYAN reports them and does not guess)
            step["unapplied_filters"] = summary.get("unapplied_filters")
            # DOIs that BAYAN's own Crossref lookup filled in (not from the library record)
            step["doi_matches"] = [m.get("doi") for m in summary.get("doi_matches") or []
                                   if m.get("doi")]
            # the results page loads the run's papers, as the web UI does
            _, detail = self._step("open_results", "GET", f"/api/runs/{data.get('id')}",
                                   log={"for": kind})
            if detail is not None and detail.status_code == 200:
                step["papers"] = [slim(p) for p in detail.json().get("papers") or []]
        return step

    def export_bibtex(self, run_id: int, papers: list[dict]) -> dict:
        refs = [{"source": p["source"], "id": p["id"]} for p in papers[: self.cfg.export_n]]
        step, resp = self._step("export_bibtex", "POST", f"/api/runs/{run_id}/export",
                                json={"format": "bibtex", "papers": refs},
                                log={"papers_sent": len(refs)})
        if resp is not None and resp.status_code == 200:
            step["text"] = resp.text
            step["expected_dois"] = [p.get("doi") or "" for p in papers[: self.cfg.export_n]]
        return step

    # ------------------------------------------------------------ the episode

    def known_sample(self) -> list[dict]:
        rng = random.Random(f"{self.cfg.seed}-{self.area['id']}")
        by: dict[str, list[dict]] = {}
        for r in self.gold:
            by.setdefault(r["stratum"], []).append(r)
        order = [r for s in ("landmark", "mid", "recent") for r in rng.sample(by.get(s, []),
                                                                           len(by.get(s, [])))]
        # interleave the strata so a small sample still covers all three
        picked, i = [], 0
        while len(picked) < min(self.cfg.known_items, len(order)):
            for s in ("landmark", "mid", "recent"):
                items = [r for r in order if r["stratum"] == s and r not in picked]
                if items and len(picked) < self.cfg.known_items:
                    picked.append(items[0])
            i += 1
            if i > self.cfg.known_items * 3:
                break
        return picked

    def run(self) -> Episode:
        try:
            if not self.sign_up():
                self.ep.error = "sign-up failed"
                return self.ep
            queries = [q["query"] for q in self.area["queries"][: self.cfg.topic_queries]]
            first = None
            for q in [self.area["name"], *queries]:
                step = self.search("topic", q, extra={"query_origin": (
                    "subfield_name" if q == self.area["name"] else "topic_name")})
                if first is None and step.get("papers"):
                    first = step
            # gold papers whose own topic was not among the searched ones get one search too
            searched = set(queries)
            extra_topics = list(dict.fromkeys(r["query"] for r in self.gold
                                              if r["query"] and r["query"] not in searched))
            for q in extra_topics[: self.cfg.gold_topics]:
                self.search("topic", q, extra={"query_origin": "gold_topic"})
            for r in self.known_sample():
                self.search("known_item", r["title"], quota=False, extra={
                    "gold_id": r["openalex_id"], "gold_doi": r["doi"], "stratum": r["stratum"]})
            if first is not None:
                self.export_bibtex(first["run_id"], first["papers"])
            self.search("special_query", special_query(self.area),
                        extra={"query_origin": "boolean_keywords"})
            lang = LANG_ROTATION[int(self.area["id"]) % len(LANG_ROTATION)]
            self.search("language", self.area["name"], languages=[lang],
                        extra={"language": lang})
        except Exception as exc:  # an agent failure is a result, not a crash of the bench
            self.ep.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            if self._own_http:
                self.http.close()
        return self.ep

