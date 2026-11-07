"""Topic mapping (M26): organise the papers of a run under main topics and subtopics.

Two model steps, both cached and metered like the other agents, and deterministic checks:

1. taxonomy: from the research area, the papers' titles, library topics and keywords, and the
   researcher's own topics of interest (up to 10), the model proposes 4 to 10 main topics with
   2 to 6 subtopics each. Every topic of interest must come back unchanged; one the model
   dropped is added back by code as its own main topic, so nothing the researcher asked for is
   lost. The researcher may then tick, untick or add topics before the mapping runs.
2. assignment: in batches, each paper gets one main topic, one subtopic, up to two related
   subtopics and a short phrase copied from its own title or abstract. Code checks that every
   paper reference and topic id exists and that the phrase occurs in the paper's text; any
   answer that fails is recorded as "unclassified" with the reason, never guessed. The basis
   (`abstract`, or `title_only` when the source supplied no abstract) comes from the data.

The mapping covers the chosen scope only (the whole run or the ticked papers) and uses titles
and abstracts, never full texts. Counts, including zeros, come from the stored assignments.
"""

import json
import logging
import sqlite3

from .concepts import cached_call, concept_key
from .reader import ReaderClient, ReaderError, ReaderRefused, squash

log = logging.getLogger("bayan.topics")

VERSION = "topics-v1"
STEPS = ("taxonomy", "assign")
SCOPES = ("all", "selected")
MAX_OWN_TOPICS = 10
MAX_TOPIC_CHARS = 80
MAX_MAIN = 10
MAX_SUB = 6
MAX_RELATED = 2
MAX_MAPPED = 500
MAX_TAXONOMY_PAPERS = 200  # paper lines shown to the taxonomy step (run rank order)
BATCH = 25
MAX_TITLE_CHARS = 200
MAX_ABSTRACT_CHARS = 1500
MAX_PHRASE_CHARS = 160
UNCLASSIFIED = "unclassified"
MAPPED = "mapped"
BASIS = {"abstract": "title and abstract", "title_only": "title only (no abstract supplied)"}
REASONS = {
    "no_answer": "the model gave no answer for this paper",
    "none": "the model found no fitting topic",
    "unknown_main": "the main topic id is not in the topic list",
    "unknown_sub": "the subtopic id is not under that main topic",
    "phrase": "the justifying phrase was not found in the title or abstract",
}
PLACED_BY_CODE = "added by code: the model left this topic of interest out"
NOTE = ("Topics were assigned from titles and abstracts only, by a model, and every answer was "
        "checked in code: the paper, the topic ids and the quoted phrase must exist. Papers "
        "that failed a check are listed as unclassified with the reason. The mapping covers the "
        "chosen scope only and says nothing about papers outside it.")

Ref = tuple[str, str]

SYSTEM = """You organise research papers under topics for a researcher. You work only from the
text you are given: titles, abstracts, library topics and keywords. You never use outside
knowledge about a paper, never invent a topic the papers do not support, and when asked to
quote you copy words exactly as they appear. Answer in the language of the research area."""

TAXONOMY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["main"],
    "properties": {"main": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "subtopics"],
        "properties": {"title": {"type": "string"},
                       "subtopics": {"type": "array", "items": {"type": "string"}}}}}}}

ASSIGN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["papers"],
    "properties": {"papers": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["ref", "none", "main", "sub", "related", "phrase"],
        "properties": {"ref": {"type": "string"}, "none": {"type": "boolean"},
                       "main": {"type": "string"}, "sub": {"type": "string"},
                       "related": {"type": "array", "items": {"type": "string"}},
                       "phrase": {"type": "string"}}}}}}


# --------------------------------------------------------------------------- text helpers

def _clean(value: object, limit: int) -> str:
    return squash(str(value)).strip()[:limit].strip() if isinstance(value, str) else ""


def _norm(text: str) -> str:
    return " ".join(squash(text).lower().split())


def topic_key(name: str) -> str:
    """Case-, space- and plural-insensitive key, so "Reactor Physics" and "reactor physics"
    are one topic."""
    return concept_key(name)


def clean_own_topics(values: object) -> list[str]:
    """The researcher's topics of interest: trimmed, non-empty, distinct, at most ten."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        text = _clean(value, MAX_TOPIC_CHARS)
        key = topic_key(text)
        if text and key and key not in seen:
            seen.add(key)
            out.append(text)
    return out[:MAX_OWN_TOPICS]


def _tagged(tag: str, text: str) -> str:
    return f"<{tag}>\n{text}\n</{tag}>"


# --------------------------------------------------------------------------- taxonomy

def paper_line(ref: str, paper: dict) -> str:
    bits = [_clean(paper.get("title"), MAX_TITLE_CHARS) or "(untitled)"]
    if paper.get("topic"):
        bits.append(f"library topic: {_clean(paper['topic'], MAX_TOPIC_CHARS)}")
    keywords = [_clean(k, MAX_TOPIC_CHARS) for k in (paper.get("keywords") or [])][:8]
    if keywords:
        bits.append("keywords: " + "; ".join(k for k in keywords if k))
    return f"[{ref}] " + " · ".join(bits)


def taxonomy_prompt(area: str, papers: list[dict], own_topics: list[str]) -> str:
    shown = papers[:MAX_TAXONOMY_PAPERS]
    lines = "\n".join(paper_line(f"P{i}", p) for i, p in enumerate(shown, start=1))
    if len(papers) > len(shown):
        lines += f"\n(and {len(papers) - len(shown)} more papers not listed)"
    own = "\n".join(f"- {t}" for t in own_topics) or "(none)"
    return (f"Task: propose a topic structure for organising the papers below, which were found "
            f"for the research area given. Give between 4 and {MAX_MAIN} main topics, each with "
            f"2 to {MAX_SUB} subtopics, specific to this research area and to what these papers "
            f"actually cover. For a nuclear engineering area the main topics might be, for "
            f"example, reactor physics and neutronics; reactor shielding design; reactor "
            f"materials; fuel management; thermal-hydraulics; fuel fabrication; choose what fits "
            f"these papers, not that list. Use only the titles, library topics and keywords "
            f"given.\n\nEvery topic of interest listed by the researcher must appear unchanged, "
            f"either as a main topic or as a subtopic under a fitting main topic: never rename, "
            f"merge or drop one. Topic names are short noun phrases of at most "
            f"{MAX_TOPIC_CHARS} characters, each distinct.\n\n"
            f"{_tagged('area', _clean(area, 500) or '(not given)')}\n\n"
            f"{_tagged('topics_of_interest', own)}\n\n{_tagged('papers', lines)}")


def clean_taxonomy(data: dict, own_topics: list[str]) -> list[dict]:
    """The model's topic structure, checked: distinct names, caps applied, every topic of
    interest present (marked `own`), ids T1, T1.1... assigned in order."""
    own_keys = {topic_key(t): t for t in own_topics}
    groups: list[dict] = []
    seen: set[str] = set()
    for item in (data.get("main") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), MAX_TOPIC_CHARS)
        key = topic_key(title)
        if not title or not key or key in seen:
            continue
        seen.add(key)
        subs: list[dict] = []
        for sub in item.get("subtopics") or []:
            name = _clean(sub, MAX_TOPIC_CHARS)
            skey = topic_key(name)
            if not name or not skey or skey in seen or len(subs) == MAX_SUB:
                continue
            seen.add(skey)
            subs.append({"name": name, "own": skey in own_keys})
        groups.append({"title": title, "own": key in own_keys, "subtopics": subs})
    groups = _keep_own(groups[:MAX_MAIN], groups[MAX_MAIN:])
    for key, text in own_keys.items():
        if not _present(groups, key):
            groups.append({"title": text, "own": True, "subtopics": [], "placed_by": "code"})
    return number(groups)


def _present(groups: list[dict], key: str) -> bool:
    for g in groups:
        if topic_key(g["title"]) == key or any(topic_key(s["name"]) == key
                                               for s in g["subtopics"]):
            return True
    return False


def _keep_own(kept: list[dict], dropped: list[dict]) -> list[dict]:
    """Groups beyond the cap are dropped unless they hold a topic of interest."""
    for g in dropped:
        if g["own"] or any(s["own"] for s in g["subtopics"]):
            kept.append(g)
    return kept


def number(groups: list[dict]) -> list[dict]:
    """Assigns ids (T1, T1.1...) in order; used for model output and for a researcher-edited
    structure alike."""
    out = []
    for i, g in enumerate(groups, start=1):
        subs = [{"id": f"T{i}.{j}", "name": s["name"], "own": bool(s.get("own"))}
                for j, s in enumerate(g["subtopics"], start=1)]
        group = {"id": f"T{i}", "title": g["title"], "own": bool(g.get("own")),
                 "subtopics": subs}
        if g.get("placed_by"):
            group["placed_by"] = g["placed_by"]
        out.append(group)
    return out


def check_taxonomy(groups: object, own_topics: list[str]) -> list[dict]:
    """A researcher-edited structure (ticked suggestions plus own additions): the same cleaning
    as the model's, and the topics of interest kept."""
    data = {"main": [{"title": g.get("title"), "subtopics": [s.get("name") if isinstance(s, dict)
                                                              else s for s in
                                                              g.get("subtopics") or []]}
                     for g in groups if isinstance(g, dict)]} \
        if isinstance(groups, list) else {"main": []}
    return clean_taxonomy(data, own_topics)


# --------------------------------------------------------------------------- assignment

def _topic_lines(taxonomy: list[dict]) -> str:
    lines = []
    for g in taxonomy:
        lines.append(f"{g['id']} {g['title']}" + ("" if g["subtopics"] else " (no subtopics)"))
        lines.extend(f"  {s['id']} {s['name']}" for s in g["subtopics"])
    return "\n".join(lines)


def _paper_text(paper: dict) -> str:
    abstract = _clean(paper.get("abstract"), MAX_ABSTRACT_CHARS)
    title = _clean(paper.get("title"), MAX_TITLE_CHARS) or "(untitled)"
    return f"{title}\nAbstract: {abstract or 'not supplied by the source.'}"


def assign_prompt(area: str, taxonomy: list[dict], batch: list[tuple[str, dict]]) -> str:
    papers = "\n\n".join(f"[{ref}] {_paper_text(p)}" for ref, p in batch)
    return (f"Task: assign each paper below to exactly one main topic and, when that main topic "
            f"has subtopics, to exactly one of its subtopics, using only the paper's title and "
            f"abstract as given here. Answer with the ids (T1, T1.2...). Add up to "
            f"{MAX_RELATED} related subtopic ids when the paper clearly also concerns them, "
            f"else an empty list. Quote a phrase of 3 to 12 words copied exactly from the "
            f"paper's title or abstract that justifies the main topic. If no topic fits, answer "
            f"none: true for that paper with empty ids. Answer for every paper, by its "
            f"reference, once.\n\n{_tagged('area', _clean(area, 500) or '(not given)')}\n\n"
            f"{_tagged('topics', _topic_lines(taxonomy))}\n\n{_tagged('papers', papers)}")


def check_assignments(data: dict, taxonomy: list[dict], batch: list[tuple[str, dict]],
                      ) -> list[dict]:
    """One record per paper of the batch, in batch order: `mapped` with checked ids and phrase,
    or `unclassified` with the reason."""
    groups = {g["id"]: g for g in taxonomy}
    subs = {s["id"]: g["id"] for g in taxonomy for s in g["subtopics"]}
    answers: dict[str, dict] = {}
    for item in (data.get("papers") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict) and isinstance(item.get("ref"), str) \
                and item["ref"] not in answers:
            answers[item["ref"]] = item
    out = []
    for ref, paper in batch:
        base = {"source": paper["source"], "id": paper["id"],
                "basis": "abstract" if (paper.get("abstract") or "").strip() else "title_only",
                "main": "", "sub": "", "related": [], "phrase": ""}
        answer = answers.get(ref)
        if answer is None:
            out.append({**base, "status": UNCLASSIFIED, "reason": REASONS["no_answer"]})
            continue
        if answer.get("none") is True:
            out.append({**base, "status": UNCLASSIFIED, "reason": REASONS["none"]})
            continue
        main = _clean(answer.get("main"), 10)
        sub = _clean(answer.get("sub"), 10)
        if main not in groups:
            out.append({**base, "status": UNCLASSIFIED, "reason": REASONS["unknown_main"]})
            continue
        if groups[main]["subtopics"]:
            if subs.get(sub) != main:
                out.append({**base, "status": UNCLASSIFIED, "reason": REASONS["unknown_sub"]})
                continue
        else:
            sub = ""
        phrase = _clean(answer.get("phrase"), MAX_PHRASE_CHARS).strip("\"'“”«» ")
        text = _norm(f"{paper.get('title') or ''} {paper.get('abstract') or ''}")
        if not phrase or _norm(phrase) not in text:
            out.append({**base, "status": UNCLASSIFIED, "reason": REASONS["phrase"]})
            continue
        related = []
        for r in answer.get("related") or []:
            rid = _clean(r, 10)
            if rid in subs and rid != sub and rid not in related:
                related.append(rid)
        out.append({**base, "status": MAPPED, "main": main, "sub": sub,
                    "related": related[:MAX_RELATED], "phrase": phrase})
    return out


def counts_for(taxonomy: list[dict], papers: list[dict]) -> dict:
    """Papers per main topic and per subtopic (direct), related mentions per subtopic, and the
    unclassified count. Every id is listed, with 0 where nothing was mapped."""
    main = {g["id"]: 0 for g in taxonomy}
    sub = {s["id"]: 0 for g in taxonomy for s in g["subtopics"]}
    related = dict.fromkeys(sub, 0)
    unclassified = 0
    for p in papers:
        if p["status"] != MAPPED:
            unclassified += 1
            continue
        main[p["main"]] += 1
        if p["sub"]:
            sub[p["sub"]] += 1
        for r in p["related"]:
            related[r] += 1
    return {"main": main, "sub": sub, "related": related, "unclassified": unclassified,
            "papers": len(papers)}


# --------------------------------------------------------------------------- data access

def run_area(conn: sqlite3.Connection, run_id: int) -> str:
    row = conn.execute("SELECT request FROM search_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return ""
    try:
        return str(json.loads(row["request"]).get("profile", {}).get("query") or "")
    except (ValueError, AttributeError):
        return ""


def scope_papers(conn: sqlite3.Connection, run_id: int, scope: str,
                 selected: list[Ref]) -> list[dict]:
    """The run's papers in rank order, or the ticked ones (in run order), as dicts."""
    rows = conn.execute(
        "SELECT p.source, p.id, p.title, p.abstract, p.keywords, p.topic FROM run_papers rp"
        " JOIN papers p ON p.source = rp.source AND p.id = rp.paper_id WHERE rp.run_id = ?"
        " ORDER BY rp.rank", (run_id,)).fetchall()
    wanted = set(selected) if scope == "selected" else None
    out = []
    for r in rows:
        if wanted is not None and (r["source"], r["id"]) not in wanted:
            continue
        try:
            keywords = json.loads(r["keywords"] or "[]")
        except ValueError:
            keywords = []
        out.append({"source": r["source"], "id": r["id"], "title": r["title"] or "",
                    "abstract": r["abstract"] or "", "topic": r["topic"] or "",
                    "keywords": keywords if isinstance(keywords, list) else []})
    return out[:MAX_MAPPED]


def latest_map(conn: sqlite3.Connection, user_id: int, run_id: int,
               done_only: bool = False) -> sqlite3.Row | None:
    where = " AND status = 'done'" if done_only else ""
    return conn.execute(f"SELECT * FROM topic_maps WHERE user_id = ? AND run_id = ?{where}"
                        " ORDER BY id DESC LIMIT 1", (user_id, run_id)).fetchone()


def labels_for(row: sqlite3.Row | None) -> dict[Ref, dict]:
    """(source, id) -> {main, sub, related} names, for the papers a finished mapping covers.
    Used by the export, the idea proposer and the gap hints."""
    if row is None or row["status"] != "done" or not row["result"]:
        return {}
    result = json.loads(row["result"])
    names = {g["id"]: g["title"] for g in result["taxonomy"]}
    names.update({s["id"]: s["name"] for g in result["taxonomy"] for s in g["subtopics"]})
    out = {}
    for p in result["papers"]:
        if p["status"] == MAPPED:
            out[(p["source"], p["id"])] = {
                "main": names[p["main"]], "sub": names.get(p["sub"], ""),
                "related": [names[r] for r in p["related"]],
                "ids": [p["main"], p["sub"], *p["related"]]}
        else:
            out[(p["source"], p["id"])] = {"main": UNCLASSIFIED, "sub": "", "related": [],
                                           "ids": []}
    return out


# --------------------------------------------------------------------------- model steps

def suggest(conn: sqlite3.Connection, client: ReaderClient, user_id: int, area: str,
            papers: list[dict], own_topics: list[str]) -> tuple[list[dict], bool]:
    """(taxonomy, cached): the model's proposal, cleaned and with the topics of interest kept."""
    data, cached = cached_call(conn, client, user_id, VERSION, SYSTEM, TAXONOMY_SCHEMA,
                               taxonomy_prompt(area, papers, own_topics))
    return clean_taxonomy(data, own_topics), cached


class _Job:
    def __init__(self, conn: sqlite3.Connection, client: ReaderClient, map_id: int,
                 user_id: int) -> None:
        self.conn, self.client, self.map_id, self.user_id = conn, client, map_id, user_id
        self.steps: list[dict] = []
        self.calls = self.cached = 0

    def call(self, schema: dict, prompt: str) -> dict:
        data, cached = cached_call(self.conn, self.client, self.user_id, VERSION, SYSTEM,
                                   schema, prompt)
        self.calls += 1
        self.cached += cached
        return data

    def record(self, step: str, status: str, detail: str = "") -> None:
        self.steps.append({"step": step, "status": status, "detail": detail,
                           "calls": self.calls, "cached": self.cached})
        self.conn.execute("UPDATE topic_maps SET steps = ?, calls = ?, cached_calls = ?,"
                          " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                          (json.dumps(self.steps), self.calls, self.cached, self.map_id))
        self.conn.commit()


def _map(job: _Job, payload: dict) -> dict:
    conn = job.conn
    area = run_area(conn, payload["run_id"])
    own = clean_own_topics(payload.get("own_topics"))
    papers = scope_papers(conn, payload["run_id"], payload["scope"],
                          [tuple(p) for p in payload.get("papers") or []])
    if payload.get("taxonomy"):
        taxonomy = check_taxonomy(payload["taxonomy"], own)
        job.record("taxonomy", "skipped", "structure chosen by the researcher")
    else:
        data = job.call(TAXONOMY_SCHEMA, taxonomy_prompt(area, papers, own))
        taxonomy = clean_taxonomy(data, own)
        job.record("taxonomy", "done", f"{len(taxonomy)} main topics")
    if not taxonomy:
        raise ReaderError("The model proposed no topics for these papers.")
    assigned: list[dict] = []
    for start in range(0, len(papers), BATCH):
        batch = [(f"P{i}", p) for i, p in enumerate(papers[start:start + BATCH],
                                                     start=start + 1)]
        data = job.call(ASSIGN_SCHEMA, assign_prompt(area, taxonomy, batch))
        assigned.extend(check_assignments(data, taxonomy, batch))
    counts = counts_for(taxonomy, assigned)
    job.record("assign", "done", f"{counts['papers'] - counts['unclassified']} mapped, "
                                 f"{counts['unclassified']} unclassified")
    return {"area": area, "scope": payload["scope"], "own_topics": own, "taxonomy": taxonomy,
            "papers": assigned, "counts": counts, "basis": BASIS, "note": NOTE}


def _finish(conn: sqlite3.Connection, map_id: int, status: str, detail: str | None,
            result: dict | None, job: _Job | None = None) -> None:
    conn.execute(
        "UPDATE topic_maps SET status = ?, detail = ?, result = ?, steps = ?, calls = ?,"
        " cached_calls = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, detail, json.dumps(result) if result is not None else None,
         json.dumps(job.steps if job else []), job.calls if job else 0,
         job.cached if job else 0, map_id))
    conn.commit()


def run_topic_job(db_path, job_id: int, client: ReaderClient) -> None:
    """Background job body: builds (or takes) the topic structure, then maps the papers."""
    from .db import connect
    from .downloads import set_job

    conn = connect(db_path)
    try:
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (job_id,)).fetchone()["payload"])
        map_id = payload["map_id"]
        set_job(conn, job_id, "running")
        conn.execute("UPDATE topic_maps SET status = 'running', model = ?,"
                     " updated_at = CURRENT_TIMESTAMP WHERE id = ?", (client.model, map_id))
        conn.commit()
        job = _Job(conn, client, map_id, payload["user_id"])
        try:
            result = _map(job, payload)
        except ReaderRefused as exc:
            job.record(STEPS[min(len(job.steps), 1)], "failed", str(exc))
            _finish(conn, map_id, "refused", str(exc), None, job)
            set_job(conn, job_id, "done")
            return
        except ReaderError as exc:
            job.record(STEPS[min(len(job.steps), 1)], "failed", str(exc))
            _finish(conn, map_id, "failed", str(exc), None, job)
            set_job(conn, job_id, "done")
            return
        _finish(conn, map_id, "done", None, result, job)
        set_job(conn, job_id, "done")
    except Exception as exc:
        log.warning("topic job %s failed: %s", job_id, type(exc).__name__)
        try:
            conn.rollback()
            payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                              (job_id,)).fetchone()["payload"])
            _finish(conn, payload["map_id"], "failed", "The topic mapping failed unexpectedly.",
                    None)
        except Exception:
            pass
        set_job(conn, job_id, "failed", type(exc).__name__)
    finally:
        conn.close()


# --------------------------------------------------------------------------- records

def map_record(row: sqlite3.Row, full: bool = True) -> dict:
    out = {"id": row["id"], "run_id": row["run_id"], "status": row["status"],
           "detail": row["detail"], "scope": row["scope"],
           "request": json.loads(row["request"]), "steps": json.loads(row["steps"] or "[]"),
           "model": row["model"], "calls": row["calls"], "cached_calls": row["cached_calls"],
           "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if full:
        out["result"] = json.loads(row["result"]) if row["result"] else None
    return out


__all__ = ["ASSIGN_SCHEMA", "BASIS", "MAPPED", "MAX_OWN_TOPICS", "NOTE", "REASONS", "SCOPES",
           "STEPS", "TAXONOMY_SCHEMA", "UNCLASSIFIED", "check_assignments", "check_taxonomy",
           "clean_own_topics", "clean_taxonomy", "counts_for", "labels_for", "latest_map",
           "map_record", "run_topic_job", "scope_papers", "suggest", "topic_key"]
