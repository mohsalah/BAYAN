"""M26: topic mapping of a run's papers (main topics, subtopics, topics of interest).

No live model calls: a fake client answers each step, chosen by the schema it asks for."""

import csv
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from test_accounts_search import register, search
from test_concepts import ABSTRACTS, FakeConcepts
from test_downloads import Connector, Net, make_fetcher, paper
from test_ideas import FakeIdeas

from bayan.api import create_app
from bayan.config import Settings
from bayan.ideas import INSPIRATION_SCHEMA
from bayan.mapgaps import topic_hints
from bayan.reader import ReaderError, ReaderRefused, ReaderReply
from bayan.topics import (
    ASSIGN_SCHEMA,
    MAPPED,
    MAX_OWN_TOPICS,
    REASONS,
    TAXONOMY_SCHEMA,
    UNCLASSIFIED,
    check_assignments,
    clean_own_topics,
    clean_taxonomy,
    counts_for,
)

OWN = ["Coupled codes", "Tritium control"]
TAXONOMY = {"main": [
    {"title": "Reactor physics", "subtopics": ["Neutron transport", "neutron  transport",
                                               "Coupled codes"]},
    {"title": "reactor physics", "subtopics": ["Duplicate group"]},
    {"title": "Waste management", "subtopics": ["Transmutation"]},
]}
# paper number -> the fake model's answer for it
ANSWERS = {
    1: {"none": False, "main": "T1", "sub": "T1.2", "related": ["T2.1", "T9.9", "T1.2"],
        "phrase": "“molten salt reactor”"},
    2: {"none": False, "main": "T2", "sub": "T2.1", "related": [],
        "phrase": "transmutation des déchets"},
    3: {"none": False, "main": "T1", "sub": "T1.1", "related": [],
        "phrase": "words that are not in the abstract"},
    4: {"none": False, "main": "T1", "sub": "T2.1", "related": [], "phrase": "MSR design"},
}


class FakeTopics:
    model = "fake-topics"

    def __init__(self, refuse=False, fail=False):
        self.refuse, self.fail, self.calls, self.prompts = refuse, fail, [], []

    def extract(self, system, prompt, schema=None):
        self.calls.append(schema)
        self.prompts.append(prompt)
        if self.refuse:
            raise ReaderRefused("The model declined to read this text.")
        if self.fail:
            raise ReaderError("The model request failed.")
        if schema is TAXONOMY_SCHEMA:
            data = TAXONOMY
        elif schema is ASSIGN_SCHEMA:
            rows = re.findall(r"^\[(P\d+)\] Neutron transport in fast reactors (\d+)", prompt,
                              re.M)
            data = {"papers": [{"ref": ref, **ANSWERS[int(n)]} for ref, n in rows]}
        else:
            raise AssertionError("unknown schema")
        return ReaderReply(json.loads(json.dumps(data)), self.model, 500, 100)


@pytest.fixture
def env(tmp_path):
    made = []

    def build(topics=None, ideas=None, **extra):
        settings = Settings(database_path=tmp_path / f"t{len(made)}.sqlite",
                            pdf_root=tmp_path / f"pdfs{len(made)}", search_max_results=100,
                            searches_per_hour=50, job_resume_interval=0, **extra)
        papers = [paper(i).model_copy(update={"abstract": ABSTRACTS[f"W{i}"], "language": "",
                                              "topic": "Nuclear reactor physics",
                                              "keywords": ["reactor"]})
                  for i in range(1, 5)]
        fake = FakeTopics() if topics is None else topics
        app = create_app(settings, connector_factory=lambda lib: Connector(papers),
                         fetcher=make_fetcher(Net()), concept_client=FakeConcepts(),
                         ideas_client=ideas or None, topics_client=fake or None)
        tc = TestClient(app)
        tc.__enter__()
        made.append(tc)
        register(tc)
        run_id = search(tc).json()["id"]
        return type("Env", (), {"client": tc, "fake": fake, "app": app, "run_id": run_id})

    yield build
    for c in made:
        c.__exit__(None, None, None)


def refs(*ids):
    return [{"source": "openalex", "id": f"W{i}"} for i in ids]


def start(e, **extra):
    body = {"run_id": e.run_id, "own_topics": OWN, **extra}
    r = e.client.post("/api/topics", json=body)
    assert r.status_code == 202, r.text
    return e.client.get(f"/api/topics/{r.json()['id']}").json()


# ---------------------------------------------------------------- cleaning (no app)

def test_own_topics_are_trimmed_distinct_and_capped():
    many = [f"Topic {i}" for i in range(15)]
    assert clean_own_topics(["  Shielding ", "shielding", "", 7, "x" * 120]) == \
        ["Shielding", "x" * 80]
    assert len(clean_own_topics(many)) == MAX_OWN_TOPICS
    assert clean_own_topics("not a list") == []


def test_taxonomy_keeps_every_topic_of_interest_and_numbers_ids():
    tax = clean_taxonomy(TAXONOMY, OWN)
    assert [g["title"] for g in tax] == ["Reactor physics", "Waste management",
                                         "Tritium control"]
    assert [g["id"] for g in tax] == ["T1", "T2", "T3"]
    assert [s["name"] for s in tax[0]["subtopics"]] == ["Neutron transport", "Coupled codes"]
    assert [s["id"] for s in tax[0]["subtopics"]] == ["T1.1", "T1.2"]
    assert tax[0]["subtopics"][1]["own"] is True and tax[0]["subtopics"][0]["own"] is False
    # the model left "Tritium control" out: code adds it as its own main topic, marked
    assert tax[2]["own"] is True and tax[2]["placed_by"] == "code"
    assert clean_taxonomy({"main": "junk"}, []) == []


def test_taxonomy_cap_never_drops_a_topic_of_interest():
    data = {"main": [{"title": f"Area {i}", "subtopics": [f"Sub {i}"]} for i in range(12)]}
    tax = clean_taxonomy(data, ["Sub 11"])
    assert len(tax) == 11 and tax[-1]["title"] == "Area 11"
    assert tax[-1]["subtopics"][0]["own"] is True and "placed_by" not in tax[-1]


def test_assignments_are_checked_in_code():
    tax = clean_taxonomy(TAXONOMY, OWN)
    batch = [(f"P{i}", {"source": "openalex", "id": f"W{i}", "title": f"Title {i}",
                        "abstract": ABSTRACTS[f"W{i}"]}) for i in range(1, 5)]
    batch.append(("P5", {"source": "openalex", "id": "W5", "title": "Tritium control loop",
                         "abstract": ""}))
    batch.append(("P6", {"source": "openalex", "id": "W6", "title": "Nothing", "abstract": ""}))
    answers = [{"ref": f"P{n}", **a} for n, a in ANSWERS.items()]
    answers += [{"ref": "P5", "none": False, "main": "T3", "sub": "T3.9", "related": [],
                 "phrase": "Tritium control"},
                {"ref": "P1", "none": True, "main": "", "sub": "", "related": [], "phrase": ""}]
    out = {p["id"]: p for p in check_assignments({"papers": answers}, tax, batch)}
    assert out["W1"]["status"] == MAPPED and out["W1"]["related"] == ["T2.1"]
    assert out["W1"]["phrase"] == "molten salt reactor"  # typographic quotes stripped
    assert out["W2"]["status"] == MAPPED and out["W2"]["sub"] == "T2.1"
    assert out["W3"] == {**out["W3"], "status": UNCLASSIFIED, "reason": REASONS["phrase"]}
    assert out["W4"]["reason"] == REASONS["unknown_sub"]
    # a main topic without subtopics takes no subtopic; the basis comes from the data
    assert out["W5"]["status"] == MAPPED and out["W5"]["sub"] == ""
    assert out["W5"]["basis"] == "title_only" and out["W1"]["basis"] == "abstract"
    assert out["W6"]["reason"] == REASONS["no_answer"]
    counts = counts_for(tax, list(out.values()))
    assert counts["main"] == {"T1": 1, "T2": 1, "T3": 1}
    assert counts["sub"] == {"T1.1": 0, "T1.2": 1, "T2.1": 1}
    assert counts["related"]["T2.1"] == 1 and counts["unclassified"] == 3


def test_topic_hints_thin_topics_and_pairs_never_together():
    tax = clean_taxonomy({"main": [{"title": "A", "subtopics": ["A1", "A2"]},
                                   {"title": "B", "subtopics": ["B1"]}]}, ["B1"])

    def p(i, sub, related=()):
        return {"source": "openalex", "id": f"W{i}", "status": MAPPED, "main": sub[:2],
                "sub": sub, "related": list(related)}
    papers = [p(1, "T1.1"), p(2, "T1.1"), p(3, "T2.1"), p(4, "T2.1"), p(5, "T1.2", ["T2.1"]),
              {"source": "openalex", "id": "W6", "status": UNCLASSIFIED, "main": "", "sub": "",
               "related": []}]
    result = {"taxonomy": tax, "papers": papers, "counts": counts_for(tax, papers),
              "scope": "all"}
    hints = topic_hints(result, min_papers=2)
    thin = [h for h in hints["hints"] if h["kind"] == "thin_topic"]
    assert [h["topics"][0]["name"] for h in thin] == ["A2"]
    pairs = [h for h in hints["hints"] if h["kind"] == "topics_never_together"]
    # A1 and B1 (a topic of interest) share no paper; A2 has too few papers to take part
    assert [[t["name"] for t in h["topics"]] for h in pairs] == [["A1", "B1"]]
    assert pairs[0]["topics"][1]["own"] is True
    assert {x["id"] for x in pairs[0]["papers"]} == {"W1", "W2", "W3", "W4", "W5"}
    assert all(h["status"] == "unverified candidate" and "6 papers" in h["caveat"]
               for h in hints["hints"])


# ---------------------------------------------------------------- API

def test_without_a_key_the_endpoints_answer_503(env):
    e = env(topics=False)
    assert e.client.post("/api/topics/suggest", json={"run_id": e.run_id}).status_code == 503
    assert e.client.post("/api/topics", json={"run_id": e.run_id}).status_code == 503
    assert e.client.get(f"/api/topics?run_id={e.run_id}").json()["configured"] is False


def test_suggest_returns_the_structure_with_topics_of_interest(env):
    e = env()
    r = e.client.post("/api/topics/suggest", json={"run_id": e.run_id, "own_topics": OWN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["papers"] == 4 and body["own_topics"] == OWN and body["cached"] is False
    assert [g["title"] for g in body["taxonomy"]][-1] == "Tritium control"
    prompt = e.fake.prompts[0]
    assert "<area>\nreactor\n</area>" in prompt and "- Tritium control" in prompt
    assert "library topic: Nuclear reactor physics" in prompt
    again = e.client.post("/api/topics/suggest", json={"run_id": e.run_id, "own_topics": OWN})
    assert again.json()["cached"] is True and len(e.fake.calls) == 1


def test_mapping_job_stores_assignments_counts_and_hints(env):
    e = env()
    rec = start(e)
    assert rec["status"] == "done", rec
    assert [s["step"] for s in rec["steps"]] == ["taxonomy", "assign"]
    res = rec["result"]
    assert res["area"] == "reactor" and res["scope"] == "all"
    by_id = {p["id"]: p for p in res["papers"]}
    assert by_id["W1"]["sub"] == "T1.2" and by_id["W2"]["main"] == "T2"
    assert by_id["W3"]["reason"] == REASONS["phrase"]
    assert by_id["W4"]["reason"] == REASONS["unknown_sub"]
    assert res["counts"]["unclassified"] == 2 and res["counts"]["main"]["T3"] == 0
    assert rec["calls"] == 2 and "hints" in rec
    latest = e.client.get(f"/api/topics?run_id={e.run_id}").json()
    assert latest["latest"]["id"] == rec["id"] and latest["earlier"] == []
    gaps = e.client.get(f"/api/concepts/gaps?run_id={e.run_id}").json()
    assert gaps["topic_hints"]["status"] == "unverified candidate"


def test_export_has_topic_columns(env):
    e = env()
    start(e)
    r = e.client.post(f"/api/runs/{e.run_id}/export", json={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    got = {row["Title"]: row for row in rows}
    w1 = got["Neutron transport in fast reactors 1"]
    assert (w1["Main topic"], w1["Subtopic"], w1["Related subtopics"]) == \
        ("Reactor physics", "Coupled codes", "Transmutation")
    assert got["Neutron transport in fast reactors 3"]["Main topic"] == UNCLASSIFIED


def test_researcher_structure_skips_the_taxonomy_step(env):
    e = env()
    tax = [{"title": "Reactor physics", "subtopics": ["Neutron transport", "Coupled codes"]},
           {"title": "Waste management", "subtopics": ["Transmutation"]}]
    rec = start(e, taxonomy=tax, own_topics=["Coupled codes"])
    assert rec["steps"][0] == {**rec["steps"][0], "step": "taxonomy", "status": "skipped"}
    assert e.fake.calls == [ASSIGN_SCHEMA]
    assert [g["title"] for g in rec["result"]["taxonomy"]] == ["Reactor physics",
                                                               "Waste management"]
    assert e.client.post("/api/topics", json={"run_id": e.run_id,
                                              "taxonomy": []}).status_code == 422


def test_selected_scope_maps_only_ticked_papers(env):
    e = env()
    assert e.client.post("/api/topics", json={"run_id": e.run_id,
                                              "scope": "selected"}).status_code == 422
    assert e.client.post("/api/topics", json={"run_id": e.run_id, "scope": "selected",
                                              "papers": refs(99)}).status_code == 404
    rec = start(e, scope="selected", papers=refs(2, 1))
    assert [p["id"] for p in rec["result"]["papers"]] == ["W1", "W2"]  # run order
    assert rec["result"]["counts"]["papers"] == 2


def test_limits_and_validation(env):
    e = env(topic_runs_per_hour=2)
    too_many = {"run_id": e.run_id, "own_topics": [f"t{i}" for i in range(11)]}
    assert e.client.post("/api/topics", json=too_many).status_code == 422
    long_one = {"run_id": e.run_id, "own_topics": ["x" * 81]}
    assert e.client.post("/api/topics", json=long_one).status_code == 422
    start(e)
    start(e)
    assert e.client.post("/api/topics", json={"run_id": e.run_id}).status_code == 429


def test_other_users_cannot_see_or_map(env):
    e = env()
    rec = start(e)
    other = TestClient(e.app)
    register(other, "other@example.org")
    assert other.get(f"/api/topics/{rec['id']}").status_code == 404
    assert other.get(f"/api/topics?run_id={e.run_id}").status_code == 404
    assert other.post("/api/topics", json={"run_id": e.run_id}).status_code == 404


@pytest.mark.parametrize("kind,status", [("refuse", "refused"), ("fail", "failed")])
def test_model_refusal_and_failure_are_recorded(env, kind, status):
    e = env(topics=FakeTopics(**{kind: True}))
    rec = start(e)
    assert rec["status"] == status and rec["result"] is None
    assert rec["steps"][-1]["status"] == "failed"


def test_mapping_feeds_the_idea_proposer(env):
    ideas = FakeIdeas()
    e = env(ideas=ideas)
    start(e)
    r = e.client.post("/api/ideas", json={
        "run_id": e.run_id, "background": "How can a coupled code model a molten salt reactor?"})
    rec = e.client.get(f"/api/ideas/{r.json()['id']}").json()
    assert rec["status"] == "done", rec
    papers = {p["id"]: p for p in rec["result"]["retrieval"]["papers"]}
    # the entity "coupled code" matches the mapped subtopic "Coupled codes" of W1
    assert "topic" in papers["W1"]["signals"] and "Coupled codes" in papers["W1"]["entities"]
    assert papers["W1"]["topic_main"] == "Reactor physics"
    assert rec["result"]["coverage"]["topic_mapped"] == 1  # W1 mapped; W4 unclassified
    insp = [p for s, p in zip(ideas.calls, ideas.prompts, strict=True)
            if s is INSPIRATION_SCHEMA and "fast reactors 1" in p]
    assert insp and "topic: Reactor physics > Coupled codes" in insp[0]
