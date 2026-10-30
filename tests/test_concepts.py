"""M17: concept extraction, quote verification and cross-language merging. No live model calls."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from test_accounts_search import register, search
from test_downloads import Connector, Net, make_fetcher, paper
from test_reader import download

from bayan.api import create_app
from bayan.concepts import (
    NORMALIZE_SCHEMA,
    OUTPUT_SCHEMA,
    check_concepts,
    clean_groups,
    concept_key,
)
from bayan.config import Settings
from bayan.db import connect
from bayan.reader import ReaderRefused, ReaderReply, Source

EN = "The molten salt reactor is studied with a coupled code for the transmutation of waste."
FR = "Le réacteur à sels fondus est étudié dans cette thèse pour la transmutation des déchets."
AR = "يدرس هذا البحث مفاعل الملح المنصهر باستخدام نموذج حسابي مقترن لتحويل النفايات."
MSR = "The MSR design is compared with a light water reactor for the same thermal power."

ABSTRACTS = {"W1": EN, "W2": FR, "W3": AR, "W4": MSR}


def concept(ctype, label, name_en, quote, page=0):
    return {"type": ctype, "label": label, "name_en": name_en,
            "mentions": [{"quote": quote, "page": page}]}


EXTRACT = {
    EN: [concept("system", "molten salt reactor", "Molten salt reactor", EN),
         concept("method", "coupled code", "coupled code", EN),
         concept("system", "fast reactor", "fast reactor", EN)],  # label not in the quote
    FR: [concept("system", "réacteur à sels fondus", "molten-salt reactors", FR)],
    AR: [concept("system", "مفاعل الملح المنصهر", "Molten Salt Reactor", AR)],
    MSR: [concept("system", "MSR", "MSR", MSR),
          concept("system", "light water reactor", "light water reactor", MSR, page=4)],
}


class FakeConcepts:
    """Scripted concept answers by abstract, and a synonym pass grouping names together."""

    model = "fake-model"

    def __init__(self, groups=(("molten salt reactor", "MSR"),), refuse=False):
        self.groups, self.refuse, self.calls = groups, refuse, []

    def extract(self, system, prompt, schema=None):
        self.calls.append(schema)
        if self.refuse:
            raise ReaderRefused("The model declined to read this text.")
        if prompt.startswith("<concepts>"):
            numbers = {m.group(2).casefold(): int(m.group(1))
                       for m in re.finditer(r"^(\d+) \| \w+ \| ([^|]+?) \|", prompt, re.M)}
            data = {"groups": [[numbers[n.casefold()] for n in g if n.casefold() in numbers]
                               for g in self.groups] + [[999, -1]]}
        else:
            data = {"concepts": next((v for k, v in EXTRACT.items() if k in prompt), [])}
        return ReaderReply(json.loads(json.dumps(data)), self.model, 500, 100)


@pytest.fixture
def env(tmp_path):
    made = []

    def build(client=None, papers=None, **extra):
        settings = Settings(database_path=tmp_path / f"c{len(made)}.sqlite",
                            pdf_root=tmp_path / f"pdfs{len(made)}", search_max_results=100,
                            searches_per_hour=50, job_resume_interval=0, **extra)
        if papers is None:
            papers = [paper(i).model_copy(update={"abstract": ABSTRACTS[f"W{i}"],
                                                  "language": ""}) for i in range(1, 5)]
        fake = FakeConcepts() if client is None else client
        net = Net()
        app = create_app(settings, connector_factory=lambda lib: Connector(papers),
                         fetcher=make_fetcher(net), concept_client=fake or None)
        tc = TestClient(app)
        tc.__enter__()
        made.append(tc)
        register(tc)
        run_id = search(tc).json()["id"]
        return type("Env", (), {"client": tc, "fake": fake, "app": app, "run_id": run_id,
                                "net": net, "settings": settings})

    yield build
    for c in made:
        c.__exit__(None, None, None)


def extract(e, *ids):
    r = e.client.post("/api/concepts", json={
        "run_id": e.run_id, "papers": [{"source": "openalex", "id": i} for i in ids]})
    assert r.status_code == 202, r.text
    job = e.client.get(f"/api/concepts/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    return {p["id"]: p for p in job["papers"]}


def nodes(e):
    return {n["name"].casefold(): n for n in
            e.client.get("/api/concepts", params={"run_id": e.run_id}).json()["nodes"]}


# ---------------------------------------------------------------- keys and verifier

@pytest.mark.parametrize("a,b", [
    ("Molten-salt reactors", "molten salt reactor"),
    ("Monte Carlo methods", "Monte  Carlo method"),
    ("Cross sections", "cross-section"),
    ("Uncertainties", "uncertainty"),
])
def test_keys_ignore_case_hyphens_and_plurals(a, b):
    assert concept_key(a) == concept_key(b)


def test_keys_keep_different_concepts_apart():
    assert concept_key("analysis") == "analysis" and concept_key("gas") == "gas"
    assert concept_key("fast reactor") != concept_key("thermal reactor")
    assert concept_key("  ---  ") == ""


def test_check_concepts_rejects_unverifiable_mentions():
    src = Source("abstract", abstract=EN)
    rows = check_concepts(src, {"concepts": [
        concept("system", "molten salt reactor", "MSR", EN),
        concept("system", "molten salt reactor", "MSR", EN, page=3),
        concept("system", "fast reactor", "fast reactor", EN),
        concept("system", "molten salt reactor", "MSR", "The molten salt reactor ... waste."),
        concept("planet", "molten salt reactor", "MSR", EN),
        concept("system", "molten salt reactor", "MSR", "A molten salt reactor is cheap to run."),
    ]})
    assert [r["reject_reason"] for r in rows] == [
        None, "page_not_found", "label_not_in_quote", "quote_has_ellipsis", "unknown_type",
        "quote_not_on_page"]
    assert rows[0]["verified"] and rows[0]["language"] == "en"


def test_clean_groups_keeps_only_valid_numbers():
    assert clean_groups({"groups": [[0, 1, 1], [1, 2], [3], [4, "5", 99, -1, 5], "x"]}, 6) == [
        [0, 1], [4, 5]]


# ---------------------------------------------------------------- the API flow

def test_same_concept_in_three_languages_is_one_node(env):
    e = env()
    result = extract(e, "W1", "W2", "W3", "W4")
    assert {k: v["status"] for k, v in result.items()} == dict.fromkeys(result, "done")
    assert result["W1"]["concepts"] == 2 and result["W1"]["rejected"] == 1
    assert result["W4"]["rejected"] == 1  # page 4 of an abstract does not exist
    got = nodes(e)
    msr = got["molten salt reactor"]
    assert msr["paper_count"] == 3 and msr["languages"] == ["ar", "en", "fr"]
    assert {(lab["label"], lab["language"]) for lab in msr["labels"]} == {
        ("molten salt reactor", "en"), ("réacteur à sels fondus", "fr"),
        ("مفاعل الملح المنصهر", "ar")}
    assert msr["merged_by"] == [] and len(msr["members"]) == 1
    for m in msr["mentions"]:  # every mention has its paper, page and the quote
        assert m["level"] == "abstract" and m["page"] is None
        assert m["label"] in m["quote"] and m["quote"] == ABSTRACTS[m["paper"]["id"]]
    assert "fast reactor" not in got and "light water reactor" not in got
    assert set(got) == {"molten salt reactor", "coupled code", "msr"}


def test_synonym_pass_merge_split_and_user_decisions(env):
    e = env()
    extract(e, "W1", "W2", "W3", "W4")
    body = {"run_id": e.run_id}
    r = e.client.post("/api/concepts/normalize", json=body).json()
    assert r["groups"] == 1 and r["merged"] == 2 and r["total"] == 3 and not r["cached"]
    got = nodes(e)
    assert set(got) == {"molten salt reactor", "coupled code"}
    node = got["molten salt reactor"]
    assert node["paper_count"] == 4 and node["merged_by"] == ["model"]
    assert node["key"] == "molten salt reactor" and node["members"] == ["molten salt reactor",
                                                                        "msr"]
    assert "MSR" in {lab["label"] for lab in node["labels"]}

    # split MSR out: the model may not merge it again
    assert e.client.post("/api/concepts/split", json={**body, "key": "msr"}).status_code == 200
    assert "msr" in nodes(e)
    e.client.post("/api/concepts/normalize", json=body)
    assert "msr" in nodes(e) and nodes(e)["molten salt reactor"]["paper_count"] == 3

    # the user merges it back; a new model pass keeps the user's decision
    r = e.client.post("/api/concepts/merge",
                      json={**body, "keys": ["msr", "molten salt reactor"]})
    assert r.json() == {"key": "msr"}
    e.fake.groups = ()
    e.client.post("/api/concepts/normalize", json={**body, "fresh": True})
    got = nodes(e)
    assert set(got) == {"msr", "coupled code"} and got["msr"]["merged_by"] == ["user"]
    assert got["msr"]["paper_count"] == 4


def test_model_never_merges_into_a_split_out_concept(env):
    e = env()
    extract(e, "W1", "W2", "W3", "W4")
    body = {"run_id": e.run_id}
    e.client.post("/api/concepts/normalize", json=body)
    # split out the bigger, canonical member; the model then proposes the same group again
    e.client.post("/api/concepts/split", json={**body, "key": "molten salt reactor"})
    r = e.client.post("/api/concepts/normalize", json={**body, "fresh": True}).json()
    assert r["groups"] == 0
    got = nodes(e)
    assert got["molten salt reactor"]["paper_count"] == 3 and got["msr"]["members"] == ["msr"]


def test_model_leaves_user_built_nodes_alone(env):
    e = env()
    extract(e, "W1", "W2", "W3", "W4")
    body = {"run_id": e.run_id}
    e.client.post("/api/concepts/merge", json={**body, "keys": ["coupled code", "msr"]})
    e.fake.groups = (("molten salt reactor", "coupled code"),)
    r = e.client.post("/api/concepts/normalize", json=body).json()
    assert r["groups"] == 0
    got = nodes(e)
    assert got["coupled code"]["members"] == ["coupled code", "msr"]
    assert got["coupled code"]["merged_by"] == ["user"]
    assert got["molten salt reactor"]["paper_count"] == 3


def test_splitting_the_canonical_member_keeps_the_rest_together(env):
    e = env()
    extract(e, "W1", "W2", "W3", "W4")
    body = {"run_id": e.run_id}
    e.client.post("/api/concepts/merge",
                  json={**body, "keys": ["coupled code", "molten salt reactor", "msr"]})
    assert set(nodes(e)) == {"coupled code"}
    e.client.post("/api/concepts/split", json={**body, "key": "coupled code"})
    got = nodes(e)
    assert got["coupled code"]["paper_count"] == 1
    assert sorted(got["molten salt reactor"]["members"]) == ["molten salt reactor", "msr"]


def test_bad_merge_and_split_requests(env):
    e = env()
    extract(e, "W1")
    body = {"run_id": e.run_id}
    r = e.client.post("/api/concepts/merge", json={**body, "keys": ["coupled code", "nope"]})
    assert r.status_code == 422 and "Unknown concept" in r.json()["detail"]
    r = e.client.post("/api/concepts/merge", json={**body, "keys": ["coupled code"] * 2})
    assert r.status_code == 422
    r = e.client.post("/api/concepts/split", json={**body, "key": "coupled code"})
    assert r.status_code == 422


def test_full_text_mentions_carry_page_and_label(env):
    e = env(papers=[paper(1, oa_url="https://oa.example/W1.pdf")])
    q2 = "Monte Carlo simulation with continuous energy cross sections."
    q3 = "The multiplication factor agrees within 200 pcm of the benchmark."
    EXTRACT[q2] = [concept("method", "Monte Carlo simulation", "Monte Carlo simulations", q2, 2),
                   concept("quantity", "multiplication factor", "multiplication factor", q3, 2)]
    try:
        download(e)
        extract(e, "W1")
    finally:
        del EXTRACT[q2]
    got = nodes(e)
    m = got["monte carlo simulations"]["mentions"][0]
    assert (m["level"], m["page"], m["page_label"]) == ("full_text", 2, "2")
    assert "multiplication factor" not in got  # the quote is on page 3, not on page 2


def test_refusal_after_success_removes_old_mentions(env):
    e = env()
    extract(e, "W1")
    assert "molten salt reactor" in nodes(e)
    with connect(e.settings.database_path) as conn:
        conn.execute("DELETE FROM reader_cache")
    e.fake.refuse = True
    assert extract(e, "W1")["W1"]["status"] == "refused"
    assert nodes(e) == {}
    with connect(e.settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0] == 0


def test_cached_extraction_costs_nothing(env):
    e = env()
    extract(e, "W1")
    calls = len(e.fake.calls)
    again = extract(e, "W1")["W1"]
    assert again["cached_parts"] == 1 and len(e.fake.calls) == calls
    assert e.fake.calls[0] is OUTPUT_SCHEMA


def test_refused_paper_is_marked_refused(env):
    e = env(client=FakeConcepts(refuse=True))
    result = extract(e, "W2")["W2"]
    assert result["status"] == "refused" and nodes(e) == {}
    listing = e.client.get("/api/concepts", params={"run_id": e.run_id}).json()
    assert [p["status"] for p in listing["papers"]] == ["refused"]


def test_normalize_sends_its_own_schema_and_is_cached(env):
    e = env()
    extract(e, "W1", "W4")
    e.client.post("/api/concepts/normalize", json={"run_id": e.run_id})
    assert e.fake.calls[-1] is NORMALIZE_SCHEMA
    calls = len(e.fake.calls)
    r = e.client.post("/api/concepts/normalize", json={"run_id": e.run_id}).json()
    assert r["cached"] and len(e.fake.calls) == calls and r["groups"] == 1


def test_not_configured_is_503(env):
    e = env(client=False)
    body = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": "W1"}]}
    r = e.client.post("/api/concepts", json=body)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert e.client.post("/api/concepts/normalize",
                         json={"run_id": e.run_id}).status_code == 503


def test_concepts_are_private_and_rate_limited(env):
    e = env(concept_readings_per_hour=2, concept_merges_per_hour=1)
    extract(e, "W1", "W2")
    body = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": "W3"}]}
    r = e.client.post("/api/concepts", json=body)
    assert r.status_code == 429 and "0 left" in r.json()["detail"]
    assert e.client.post("/api/concepts/normalize", json={"run_id": e.run_id}).status_code == 200
    assert e.client.post("/api/concepts/normalize", json={"run_id": e.run_id}).status_code == 429
    with TestClient(e.app) as other:
        register(other, "other@example.org")
        assert other.get("/api/concepts/jobs/1").status_code == 404
        assert other.get("/api/concepts", params={"run_id": e.run_id}).status_code == 404
        assert other.post("/api/concepts", json=body).status_code == 404
        merge = {"run_id": e.run_id, "keys": ["msr", "coupled code"]}
        assert other.post("/api/concepts/merge", json=merge).status_code == 404
        own = search(other).json()["id"]
        assert other.get("/api/concepts", params={"run_id": own}).json()["nodes"] == []
    bad = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": "W99"}]}
    assert e.client.post("/api/concepts", json=bad).status_code == 404
