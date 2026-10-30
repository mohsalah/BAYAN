"""M10: idea proposer and proposal drafts after the SciPIP pipeline. No live model calls."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from test_accounts_search import register, search
from test_concepts import ABSTRACTS, AR, EN, FR, MSR, FakeConcepts, extract
from test_downloads import Connector, Net, make_fetcher, paper

from bayan.api import create_app
from bayan.config import Settings
from bayan.db import connect
from bayan.ideas import (
    BRAINSTORM_SCHEMA,
    ENTITY_SCHEMA,
    IDEAS_SCHEMA,
    INSPIRATION_SCHEMA,
    INTEGRATE_SCHEMA,
    PROPOSAL_SCHEMA,
    SECTIONS,
    STATUS,
    check_refs,
    clean_entities,
)
from bayan.reader import ReaderError, ReaderRefused, ReaderReply

ENTITIES = ["molten salt reactor", "coupled code", "light water reactor"]
SECTION_TEXT = {s: f"Text for {s} [P1]." for s in SECTIONS}


def ref_in(prompt):
    m = re.search(r"\[P(\d+)\]", prompt)
    return f"P{m.group(1)}" if m else None


class FakeIdeas:
    """Scripted answers for each step, chosen by the schema the step asks for."""

    model = "fake-ideas"

    def __init__(self, refuse=False, fail=False):
        self.refuse, self.fail, self.calls, self.prompts = refuse, fail, [], []
        self.integrated = [
            {"title": "Idea A+", "summary": "Combine [P1] and [P9].", "papers": ["P1", "P9"]},
            {"title": "Ungrounded", "summary": "No paper supports this.", "papers": []},
            {"title": "Idea C", "summary": "Builds on [P2].", "papers": ["P2", "P2"]},
        ]

    def extract(self, system, prompt, schema=None):
        self.calls.append(schema)
        self.prompts.append(prompt)
        if self.refuse:
            raise ReaderRefused("The model declined to read this text.")
        if self.fail:
            raise ReaderError("The model request failed.")
        if schema is ENTITY_SCHEMA:
            data = {"entities": ENTITIES + ["Molten Salt Reactor", "x" * 80, 42, "a b c d e f"]}
        elif schema is BRAINSTORM_SCHEMA:
            data = {"ideas": [{"title": "Wild idea", "summary": "From nothing."}]}
        elif schema is INSPIRATION_SCHEMA:
            ref = ref_in(prompt)
            if "light water" in prompt:
                data = {"usable": False, "inspirations": ["ignored"]}
            else:
                data = {"usable": True, "inspirations": [f"Use the approach of {ref}.", ""]}
        elif schema is IDEAS_SCHEMA:
            data = {"ideas": [{"title": "Idea A", "summary": "From [P1] and [P2].",
                               "papers": ["P1", "P2"]},
                              {"title": "Idea B", "summary": "Cites nothing real.",
                               "papers": ["P9"]}]}
        elif schema is INTEGRATE_SCHEMA:
            data = {"ideas": self.integrated}
        elif schema is PROPOSAL_SCHEMA:
            title = re.search(r"<idea>\n\*\*(.+?)\*\*", prompt).group(1)
            data = {"title": title, "papers": ["P1", "P7"], "sections": dict(SECTION_TEXT),
                    "questions": ["Q1 [P1]?", "Q2?", "Q3?", "Q4 extra?"],
                    "assumptions": ["Facility access is assumed."],
                    "to_verify": ["Search beyond the chosen set."]}
        else:
            raise AssertionError("unknown schema")
        return ReaderReply(json.loads(json.dumps(data)), self.model, 700, 150)


@pytest.fixture
def env(tmp_path):
    made = []

    def build(ideas=None, **extra):
        settings = Settings(database_path=tmp_path / f"i{len(made)}.sqlite",
                            pdf_root=tmp_path / f"pdfs{len(made)}", search_max_results=100,
                            searches_per_hour=50, job_resume_interval=0, **extra)
        papers = [paper(i).model_copy(update={"abstract": ABSTRACTS[f"W{i}"], "language": ""})
                  for i in range(1, 5)]
        fake = FakeIdeas() if ideas is None else ideas
        app = create_app(settings, connector_factory=lambda lib: Connector(papers),
                         fetcher=make_fetcher(Net()), concept_client=FakeConcepts(),
                         ideas_client=fake or None)
        tc = TestClient(app)
        tc.__enter__()
        made.append(tc)
        register(tc)
        run_id = search(tc).json()["id"]
        return type("Env", (), {"client": tc, "fake": fake, "app": app, "run_id": run_id,
                                "settings": settings})

    yield build
    for c in made:
        c.__exit__(None, None, None)


def start(e, background="How can a coupled code model a molten salt reactor?", **extra):
    body = {"run_id": e.run_id, "background": background, **extra}
    r = e.client.post("/api/ideas", json=body)
    assert r.status_code == 202, r.text
    return e.client.get(f"/api/ideas/{r.json()['id']}").json()


def refs(*ids):
    return [{"source": "openalex", "id": i} for i in ids]


# ---------------------------------------------------------------- units

def test_clean_entities_dedupes_and_limits():
    data = {"entities": ["Molten salt reactor", "molten-salt reactor", " coupled  code ", "",
                         "x" * 80, 42, "one two three four five six", "ok"]}
    assert clean_entities(data) == ["Molten salt reactor", "coupled code", "ok"]
    assert clean_entities({"entities": [f"e{i}" for i in range(20)]}, limit=3) == ["e0", "e1",
                                                                                   "e2"]
    assert clean_entities({}) == []


def test_check_refs_keeps_known_refs_once_in_order():
    kept, unknown = check_refs(["P2", "P9", "P1", "P2", 3, "p1"], {"P1", "P2"})
    assert kept == ["P2", "P1"] and unknown == ["P9", "3", "p1"]


# ---------------------------------------------------------------- pipeline

def test_pipeline_retrieves_grounds_and_drafts(env):
    e = env()
    extract(e, "W2")  # W2 gets a verified concept "molten salt reactor" (French label)
    rec = start(e, papers=refs("W3"), idea_count=2, proposal_count=2)
    assert rec["status"] == "done", rec
    assert [s["step"] for s in rec["steps"]] == ["entities", "brainstorm", "retrieve",
                                                 "inspirations", "ideas", "integrate",
                                                 "proposals"]
    assert all(s["status"] == "done" for s in rec["steps"])
    res = rec["result"]
    assert res["entities"] == ENTITIES

    # retrieval: ticked paper first, then keyword and concept matches, each with its signals
    got = {p["id"]: p for p in res["retrieval"]["papers"]}
    assert [p["id"] for p in res["retrieval"]["papers"]] == ["W3", "W1", "W2", "W4"]
    assert got["W3"]["ref"] == "P1" and got["W3"]["signals"] == ["selected"]
    assert got["W1"]["signals"] == ["keyword"]
    assert got["W1"]["entities"] == ["molten salt reactor", "coupled code"]
    assert got["W2"]["signals"] == ["concept"] and got["W2"]["entities"] == ["molten salt reactor"]
    assert got["W4"]["signals"] == ["keyword"] and got["W4"]["entities"] == ["light water reactor"]
    assert "co_citation" in res["retrieval"]["unavailable"]
    assert all(p["material"] == "abstract" for p in res["retrieval"]["papers"])

    # inspirations: one per paper, each tied to its reference; W4 was judged not usable
    insp = {i["ref"]: i for i in res["inspirations"]}
    assert set(insp) == {"P1", "P2", "P3", "P4"}
    assert insp["P4"]["usable"] is False and insp["P4"]["inspirations"] == []
    assert insp["P2"]["inspirations"] == ["Use the approach of P2."]
    assert insp["P2"]["level"] == "abstract"

    # ideas: unknown references removed, unsupported idea dropped with its reason
    assert [i["title"] for i in res["initial_ideas"]] == ["Idea A", "Idea B"]
    assert res["initial_ideas"][1]["papers"] == [] and res["initial_ideas"][1]["unknown_refs"] == ["P9"]
    assert [i["title"] for i in res["ideas"]] == ["Idea A+", "Idea C"]
    assert res["ideas"][0]["papers"] == ["P1"] and res["ideas"][0]["unknown_refs"] == ["P9"]
    assert res["ideas"][1]["papers"] == ["P2"]
    assert res["dropped"] == [{"title": "Ungrounded", "summary": "No paper supports this.",
                               "reason": "no supporting paper from the chosen set"}]
    assert res["brainstorm"] == [{"title": "Wild idea", "summary": "From nothing.",
                                  "grounded": False}]

    # proposals: one per kept idea, every section, three questions, checked citations
    assert [p["title"] for p in res["proposals"]] == ["Idea A+", "Idea C"]
    prop = res["proposals"][0]
    assert prop["status"] == STATUS and prop["papers"] == ["P1"] and prop["unknown_refs"] == ["P7"]
    assert list(prop["sections"]) == list(SECTIONS)
    assert prop["questions"] == ["Q1 [P1]?", "Q2?", "Q3?"]
    assert prop["assumptions"] and prop["to_verify"]
    assert res["coverage"] == {"papers": 4, "full_text": 0, "abstract": 4, "metadata_only": 0}
    assert rec["calls"] == 1 + 1 + 4 + 1 + 1 + 2 and rec["cached_calls"] == 0
    assert "unverified" in rec["result"]["note"]


def test_inspiration_uses_verified_reader_evidence_when_present(env):
    e = env()
    quote = "We couple a neutronics code to a thermal-hydraulics solver for the MSR core."
    with connect(e.settings.database_path) as conn:
        conn.execute(
            "INSERT INTO evidence(user_id, source, paper_id, dimension, statement, quote, page,"
            " page_label, level, verified) VALUES(1,'openalex','W1','methods','Coupled.',?,2,"
            "'2','full_text',1)", (quote,))
        conn.execute(
            "INSERT INTO evidence(user_id, source, paper_id, dimension, statement, quote, page,"
            " page_label, level, verified) VALUES(1,'openalex','W1','methods','Fake.','made up',"
            "3,'3','full_text',0)")
    rec = start(e, papers=refs("W1"), idea_count=1, proposal_count=1)
    assert rec["status"] == "done", rec
    res = rec["result"]
    w1 = next(p for p in res["retrieval"]["papers"] if p["id"] == "W1")
    assert w1["material"] == "full_text"
    insp = next(i for i in res["inspirations"] if i["ref"] == w1["ref"])
    assert insp["level"] == "full_text"
    prompt = next(p for p in e.fake.prompts if f"[{w1['ref']}]" in p and "<materials>" in p)
    assert quote in prompt and "page 2" in prompt and "made up" not in prompt
    assert EN not in prompt  # the abstract is not used when verified evidence exists
    assert res["coverage"]["full_text"] == 1


def test_brainstorm_can_be_skipped(env):
    e = env()
    rec = start(e, brainstorm=False, idea_count=1, proposal_count=1)
    assert rec["status"] == "done"
    assert BRAINSTORM_SCHEMA not in e.fake.calls
    step = next(s for s in rec["steps"] if s["step"] == "brainstorm")
    assert step["status"] == "skipped"
    assert rec["result"]["brainstorm"] is None
    integrate = next(p for p, s in zip(e.fake.prompts, e.fake.calls, strict=True) if s is INTEGRATE_SCHEMA)
    assert "<brainstorm>" not in integrate


def test_idea_count_and_proposal_count_cap_the_output(env):
    e = env()
    e.fake.integrated = [{"title": f"Idea {i}", "summary": "s", "papers": ["P1"]}
                         for i in range(5)]
    rec = start(e, idea_count=3, proposal_count=1)
    res = rec["result"]
    assert [i["title"] for i in res["ideas"]] == ["Idea 0", "Idea 1", "Idea 2"]
    assert [p["title"] for p in res["proposals"]] == ["Idea 0"]
    # nothing ticked and no concepts yet: only the two English abstracts match an entity
    assert [p["id"] for p in res["retrieval"]["papers"]] == ["W1", "W4"]
    assert rec["calls"] == 1 + 1 + 2 + 1 + 1 + 1


def test_no_usable_inspiration_stops_before_ideas(env):
    e = env()
    e.fake.integrated = []

    class NoUse(FakeIdeas):
        def extract(self, system, prompt, schema=None):
            if schema is INSPIRATION_SCHEMA:
                self.calls.append(schema)
                return ReaderReply({"usable": False, "inspirations": []}, self.model, 1, 1)
            return super().extract(system, prompt, schema)

    e2 = env(ideas=NoUse())
    rec = start(e2)
    assert rec["status"] == "done"
    steps = {s["step"]: s for s in rec["steps"]}
    assert steps["ideas"]["status"] == "skipped" and steps["proposals"]["status"] == "skipped"
    assert rec["result"]["ideas"] == [] and rec["result"]["proposals"] == []
    assert IDEAS_SCHEMA not in e2.fake.calls


def test_second_run_on_the_same_input_is_cached(env):
    e = env()
    first = start(e, papers=refs("W3"))
    n = len(e.fake.calls)
    second = start(e, papers=refs("W3"))
    assert second["status"] == "done" and second["id"] != first["id"]
    assert len(e.fake.calls) == n
    assert second["cached_calls"] == second["calls"] == first["calls"]
    assert second["result"]["proposals"] == first["result"]["proposals"]


def test_refusal_and_failure_are_recorded_without_partial_results(env):
    e = env(ideas=FakeIdeas(refuse=True))
    rec = start(e)
    assert rec["status"] == "refused" and "declined" in rec["detail"]
    assert rec["result"] is None
    assert rec["steps"][0] == {"step": "entities", "status": "failed", "detail": rec["detail"],
                               "calls": 1, "cached": 0}
    e = env(ideas=FakeIdeas(fail=True))
    rec = start(e)
    assert rec["status"] == "failed" and rec["result"] is None


def test_runs_are_listed_private_validated_and_rate_limited(env):
    e = env(idea_runs_per_hour=2)
    rec = start(e, papers=refs("W1"))
    listed = e.client.get("/api/ideas", params={"run_id": e.run_id}).json()
    assert [r["id"] for r in listed] == [rec["id"]] and listed[0]["status"] == "done"
    assert "background" in listed[0] and "result" not in listed[0]

    bad = {"run_id": e.run_id, "background": "x"}
    assert e.client.post("/api/ideas", json={**bad, "papers": refs("W99")}).status_code == 404
    assert e.client.post("/api/ideas", json={**bad, "background": ""}).status_code == 422
    assert e.client.post("/api/ideas", json={**bad, "idea_count": 0}).status_code == 422
    assert e.client.post("/api/ideas", json={**bad, "proposal_count": 6}).status_code == 422
    assert e.client.post("/api/ideas", json={**bad, "run_id": 99}).status_code == 404

    with TestClient(e.app) as other:
        register(other, "other@example.org")
        assert other.get(f"/api/ideas/{rec['id']}").status_code == 404
        assert other.get("/api/ideas", params={"run_id": e.run_id}).status_code == 404
        assert other.post("/api/ideas", json=bad).status_code == 404

    assert e.client.post("/api/ideas", json=bad).status_code == 202
    r = e.client.post("/api/ideas", json=bad)
    assert r.status_code == 429 and "2" in r.json()["detail"]


def test_not_configured_is_503(env):
    e = env(ideas=False)
    r = e.client.post("/api/ideas", json={"run_id": e.run_id, "background": "x"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_prompts_carry_the_rules_and_only_real_papers(env):
    e = env()
    extract(e, "W2")
    start(e, papers=refs("W3"))
    by_schema = {id(s): p for p, s in zip(e.fake.prompts, e.fake.calls, strict=True)}
    ideas_prompt = by_schema[id(IDEAS_SCHEMA)]
    assert "[P1]" in ideas_prompt and "[P4]" not in ideas_prompt  # P4 was not usable
    proposal = by_schema[id(PROPOSAL_SCHEMA)]
    assert "<papers>" in proposal and all(f"[P{i}]" in proposal for i in (1, 2, 3, 4))
    for text in (AR, EN, FR, MSR):
        assert text in proposal  # abstracts are quoted as given, never translated
    assert "Do not invent" in proposal and "assumption" in proposal.casefold()
