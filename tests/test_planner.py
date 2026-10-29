"""M3: planner agent and the editable search profile. No live model or OpenAlex calls."""

import json

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_accounts_search import FakeConnector, make_paper, register
from test_reader import FakeReader, sse

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors import ConnectorError
from bayan.connectors.arxiv import build_query as arxiv_query
from bayan.connectors.openalex import OpenAlexConnector, build_filter
from bayan.db import connect
from bayan.models import SearchProfile
from bayan.planner import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    Plan,
    PlanRequest,
    build_prompt,
    clean_output,
    resolve_topics,
)
from bayan.reader import AnthropicReader

AREA = "neutron transport in molten salt reactors"
MAIN = '("molten salt reactor" OR MSR) AND ("neutron transport" OR neutronics)'
ANSWER = {
    "summary": "Neutronics of molten salt reactors.",
    "main_query": MAIN,
    "tracks": [
        {"name": "Fuel salt flow", "query": '"molten salt" AND "delayed neutron precursor"',
         "why": "Moving fuel changes the precursor balance."},
        {"name": "Thermal scattering", "query": '"FLiBe" AND "thermal scattering"',
         "why": "Salt scattering data."},
    ],
    "queries": [
        {"language": "en", "query": MAIN},
        {"language": "fr", "query": '"réacteur à sels fondus" AND neutronique'},
        {"language": "de", "query": "Salzschmelzereaktor Neutronik"},  # not requested
    ],
    "topic_names": ["Nuclear reactor physics and engineering", "Molten salt chemistry",
                    "T99999 made-up topic"],
    "notes": ["MSR can also mean a magnetic stimulation protocol; assumed reactors."],
}
TOPICS = {
    "nuclear reactor physics and engineering": [
        {"id": "T10372", "name": "Nuclear reactor physics and engineering", "field": "Energy",
         "subfield": "Nuclear Energy and Engineering", "works_count": 61000},
        {"id": "T11080", "name": "Nuclear Materials and Properties", "field": "Materials Science",
         "subfield": "Materials Chemistry", "works_count": 40000}],
    "molten salt chemistry": [
        {"id": "T12046", "name": "Molten salt chemistry and electrochemical processes",
         "field": "Chemistry", "subfield": "Physical Chemistry", "works_count": 9000}],
}


class FakeLookup:
    """Plays OpenAlex /topics: returns only the topics listed in TOPICS."""

    def __init__(self, fail=()):
        self.fail, self.asked = set(fail), []

    def search_topics(self, text, limit=5):
        self.asked.append(text)
        if text in self.fail:
            raise ConnectorError("OpenAlex request failed: HTTP 503")
        return TOPICS.get(text.casefold(), [])[:limit]


@pytest.fixture
def env(tmp_path):
    made = []

    def build(answer=ANSWER, planner="default", lookup=None, **extra):
        settings = Settings(database_path=tmp_path / f"p{len(made)}.sqlite",
                            search_max_results=50, searches_per_hour=50, job_resume_interval=0,
                            **extra)
        fake = FakeReader(answer) if planner == "default" else planner
        connector = FakeConnector([make_paper(i, 2020) for i in range(3)])
        lookup = lookup or FakeLookup()
        app = create_app(settings, connector_factory=lambda lib: connector, planner=fake,
                         topic_lookup=lookup)
        client = TestClient(app)
        client.__enter__()
        made.append(client)
        register(client)
        return type("Env", (), {"client": client, "planner": fake, "lookup": lookup,
                                "connector": connector, "settings": settings})

    yield build
    for client in made:
        client.__exit__(None, None, None)


def plan(e, **body):
    return e.client.post("/api/plans", json={"area": AREA, "languages": ["en", "fr"],
                                             "year_from": 2015, "year_to": 2024, **body})


# ------------------------------------------------------------------ profile + OpenAlex filter

def test_profile_topics_accept_ids_and_urls_and_reject_other_text():
    p = SearchProfile(topics=["T10372", "https://openalex.org/t10372", " T12046 "])
    assert p.topics == ["T10372", "T12046"]
    for bad in ("10372", "T0", "Nuclear physics", "W123"):
        with pytest.raises(ValidationError):
            SearchProfile(topics=[bad])


def test_openalex_filter_has_topics_only_when_asked():
    assert "topics.id" not in build_filter(SearchProfile())
    assert build_filter(SearchProfile(topics=["T1", "T22"])).endswith(",topics.id:T1|T22")


def test_openalex_topic_search_request_and_parsing():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [
            {"id": "https://openalex.org/T10372", "display_name": "Nuclear reactor physics",
             "field": {"display_name": "Energy"}, "subfield": {"display_name": "Nuclear"},
             "works_count": 61000},
            {"display_name": "record without an id"}]})

    oa = OpenAlexConnector(contact_email="me@example.org",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))
    found = oa.search_topics("reactor physics", 3)
    assert found == [{"id": "T10372", "name": "Nuclear reactor physics", "description": "",
                      "field": "Energy", "subfield": "Nuclear", "works_count": 61000}]
    url = seen[0].url
    assert url.path == "/topics" and url.params["search"] == "reactor physics"
    assert url.params["per-page"] == "3" and url.params["mailto"] == "me@example.org"


@pytest.mark.parametrize("query, expected", [
    ("neutron transport", "(all:neutron AND all:transport)"),
    (MAIN, '((all:"molten salt reactor" OR all:MSR) AND '
           '(all:"neutron transport" OR all:neutronics))'),
    ("fission NOT fusion", "(all:fission ANDNOT all:fusion)"),
    # not well formed: falls back to every word, as before
    ("(reactor OR", "(all:reactor AND all:OR)"),
])
def test_arxiv_understands_planner_queries(query, expected):
    built = arxiv_query(SearchProfile(query=query, year_from=2020, year_to=2021))
    assert built == expected + " AND submittedDate:[202001010000 TO 202112312359]"


# ------------------------------------------------------------------ cleaning and topic lookup

def test_clean_output_keeps_only_well_formed_parts():
    data = json.loads(json.dumps(ANSWER))
    data["tracks"] += [{"name": "dup", "query": data["tracks"][0]["query"], "why": ""},
                       {"name": "too long", "query": "x " * 600, "why": ""},
                       {"name": "", "query": "no name", "why": ""}, "not an object"]
    out = clean_output(data, ["en", "fr"])
    assert [t["name"] for t in out["tracks"]] == ["Fuel salt flow", "Thermal scattering"]
    # en repeats the main query and de was not requested: only fr is kept
    assert out["queries_by_language"] == {"fr": '"réacteur à sels fondus" AND neutronique'}
    assert out["topic_names"] == ANSWER["topic_names"]
    assert clean_output({"main_query": "y " * 600}, ["en"])["main_query"] == ""


def test_resolve_topics_uses_only_what_openalex_returns():
    topics, unresolved, failed = resolve_topics(ANSWER["topic_names"], FakeLookup())
    assert [(t["id"], t["match"]) for t in topics] == [("T10372", "exact"), ("T12046", "closest")]
    assert topics[1]["asked"] == "Molten salt chemistry"
    # an ID written by the model is never taken as a topic
    assert unresolved == ["T99999 made-up topic"] and not failed


def test_failed_lookup_is_reported_not_hidden():
    names = ["Nuclear reactor physics and engineering", "Molten salt chemistry"]
    topics, unresolved, failed = resolve_topics(names, FakeLookup(fail={names[1]}))
    assert [t["id"] for t in topics] == ["T10372"]
    assert unresolved == [names[1]] and failed


def test_prompt_fences_the_area_and_names_languages():
    prompt = build_prompt(PlanRequest(area=AREA, languages=["en", "ar"], year_from=2015))
    assert f"<area>\n{AREA}\n</area>" in prompt
    assert "English (en), Arabic (ar)" in prompt and "2015 to now" in prompt
    assert "Never give topic IDs" in SYSTEM_PROMPT


# ------------------------------------------------------------------ API

def test_plan_is_drafted_stored_and_reviewable(env):
    e = env()
    r = plan(e)
    assert r.status_code == 201, r.text
    body = r.json()
    p = body["plan"]
    assert p["main_query"] == MAIN and p["languages"] == ["en", "fr"]
    assert [t["id"] for t in p["topics"]] == ["T10372", "T12046"]
    assert p["unresolved_topics"] == ["T99999 made-up topic"]
    assert p["notes"] == ANSWER["notes"] and not body["edited"] and not body["cached"]
    assert body["suggested"] == p and "Anthropic API" in body["note"]
    assert AREA in e.planner.prompts[0]
    assert e.client.get(f"/api/plans/{body['id']}").json()["plan"] == p
    assert [x["id"] for x in e.client.get("/api/plans").json()] == [body["id"]]


def test_edits_persist_and_added_topics_are_marked_as_the_users(env):
    e = env()
    body = plan(e).json()
    edited = body["plan"]
    edited["main_query"] = '"molten salt reactor" AND neutronics'
    edited["tracks"] = edited["tracks"][:1]
    edited["topics"] = [dict(edited["topics"][0], match="closest"),
                        {"id": "T11111", "name": "My topic", "match": "exact"}]
    r = e.client.put(f"/api/plans/{body['id']}", json=edited)
    assert r.status_code == 200, r.text
    saved = e.client.get(f"/api/plans/{body['id']}").json()
    assert saved["edited"] and saved["plan"]["main_query"] == edited["main_query"]
    # the client cannot change how a topic was matched, or claim a lookup it never had
    assert [(t["id"], t["match"]) for t in saved["plan"]["topics"]] == [
        ("T10372", "exact"), ("T11111", "user")]
    assert saved["suggested"]["main_query"] == MAIN  # the original stays for comparison


@pytest.mark.parametrize("change", [
    {"languages": ["en", "xx"]},
    {"queries_by_language": {"ar": "not a chosen language"}},
    {"topics": [{"id": "not-an-id"}]},
    {"main_query": ""},
])
def test_invalid_edits_are_rejected(env, change):
    e = env()
    body = plan(e).json()
    r = e.client.put(f"/api/plans/{body['id']}", json={**body["plan"], **change})
    assert r.status_code == 422


def test_profile_from_plan_feeds_the_search(env):
    e = env()
    pid = plan(e).json()["id"]
    main = e.client.get(f"/api/plans/{pid}/profile").json()
    assert main["query"] == MAIN and main["topics"] == ["T10372", "T12046"]
    assert main["queries_by_language"] == {"fr": '"réacteur à sels fondus" AND neutronique'}
    assert (main["year_from"], main["year_to"]) == (2015, 2024)
    track = e.client.get(f"/api/plans/{pid}/profile", params={"track": 1}).json()
    assert track["query"] == ANSWER["tracks"][1]["query"] and track["queries_by_language"] == {}
    assert e.client.get(f"/api/plans/{pid}/profile", params={"track": 5}).status_code == 422
    r = e.client.post("/api/search", json={"profile": main})
    assert r.status_code == 200, r.text
    # the fake library has no topic filter, so the run says so instead of guessing
    assert r.json()["summary"]["unapplied_filters"] == {"openalex": ["topics"]}
    assert e.connector.profiles[0].topics == ["T10372", "T12046"]


def test_same_request_uses_the_cache_unless_fresh(env):
    e = env()
    first, second = plan(e).json(), plan(e).json()
    assert len(e.planner.prompts) == 1 and second["cached"] and not first["cached"]
    assert second["plan"] == first["plan"] and second["id"] != first["id"]
    plan(e, fresh=True)
    assert len(e.planner.prompts) == 2
    with connect(e.settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2


def test_planner_not_configured_answers_503(env):
    e = env(planner=None)
    assert e.client.get("/api/planner").json()["available"] is False
    r = plan(e)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_plans_are_private_and_rate_limited(env):
    e = env(plans_per_hour=2)
    pid = plan(e).json()["id"]
    plan(e, fresh=True)
    assert plan(e, fresh=True).status_code == 429
    e.client.post("/api/auth/logout")
    register(e.client, email="bob@example.org")
    assert e.client.get(f"/api/plans/{pid}").status_code == 404
    assert e.client.get(f"/api/plans/{pid}/profile").status_code == 404
    assert e.client.get("/api/plans").json() == []


def test_unusable_answer_stores_nothing(env):
    e = env(answer={**ANSWER, "main_query": "   "})
    r = plan(e)
    assert r.status_code == 422 and "no usable search query" in r.json()["detail"]
    assert e.client.get("/api/plans").json() == []


def test_failed_topic_lookup_adds_a_note(env):
    e = env(lookup=FakeLookup(fail={"Molten salt chemistry"}))
    p = plan(e).json()["plan"]
    assert p["notes"][0].startswith("The OpenAlex topic lookup failed")
    assert "Molten salt chemistry" in p["unresolved_topics"]


def test_planner_sends_its_own_schema():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return sse(ANSWER)

    client = AnthropicReader("sk-ant-test", "claude-sonnet-5-5", "medium", max_tokens=16000,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    assert client.extract(SYSTEM_PROMPT, "PROMPT", OUTPUT_SCHEMA).data == ANSWER
    assert seen[0]["output_config"] == {"effort": "medium", "format": {
        "type": "json_schema", "schema": OUTPUT_SCHEMA}}
    assert seen[0]["max_tokens"] == 16000


def test_plan_model_round_trips_to_a_profile():
    p = Plan(main_query="q", languages=["ar"], queries_by_language={"ar": "مفاعل"},
             topics=[{"id": "T5"}])
    profile = p.to_profile(year_from=2000, year_to=2001)
    assert profile.language_queries() == [("ar", "مفاعل")] and profile.topics == ["T5"]
