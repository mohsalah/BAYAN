"""M30: search the local library first."""

import csv
import io
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.library import library_match
from bayan.models import Paper

PASSWORD = "correct horse battery"


def paper(i, title, year=2020, cites=0, refs=None, source="openalex", doi=None, lang="en",
          category="peer_reviewed", abstract=""):
    return Paper(source=source, id=f"W{i}" if source == "openalex" else f"E{i}",
                 doi=doi if doi is not None else f"10.1/w{i}", title=title, year=year,
                 citations=cites, language=lang, category=category, abstract=abstract,
                 referenced_works=refs if source == "openalex" else None)


class Lib:
    """Answers each search with the papers registered for its query text."""

    tier = "open"
    supported_filters = frozenset({"years", "document_types", "languages"})

    def __init__(self, name):
        self.name, self.answers, self.calls = name, {}, 0

    def search(self, profile):
        self.calls += 1
        yield from self.answers.get(profile.query, [])


@pytest.fixture
def env(tmp_path):
    libs = {"openalex": Lib("openalex"), "europepmc": Lib("europepmc")}
    settings = Settings(database_path=tmp_path / "t.sqlite", search_max_results=50,
                        job_resume_interval=0, searches_per_hour=3)
    with TestClient(create_app(settings, connector_factory=lambda n: libs.get(n))) as client:
        client.libs = libs
        client.app_db = settings.database_path
        yield client


def sign_in(client, email):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    if r.status_code == 409:
        client.post("/api/auth/login", json={"email": email, "password": PASSWORD})


def remote(client, query, papers=None, libraries=("openalex",), mode="remote", quota_total=None,
           **profile):
    if papers is not None:
        client.libs[libraries[0]].answers[query] = papers
    body = {"profile": {"query": query, "year_from": 1990, "year_to": 2026,
                        "languages": ["en", "fr", "ar", "zh"],
                        "document_types": ["peer_reviewed", "other"], **profile},
            "libraries": list(libraries), "mode": mode}
    if quota_total:
        body["quota_total"] = quota_total
    r = client.post("/api/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def local(client, **params):
    r = client.get("/api/library/search", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def titles(body):
    return [p["title"] for p in body["papers"]]


def test_a_user_sees_only_their_own_library(env):
    sign_in(env, "a@example.org")
    remote(env, "a", [paper(1, "Reactor one"), paper(2, "Reactor two")])
    sign_in(env, "b@example.org")
    remote(env, "b", [paper(2, "Reactor two")])  # the same stored paper
    body = local(env, q="reactor")
    assert titles(body) == ["Reactor two"] and body["total"] == 1
    assert body["coverage"]["papers"] == 1 and len(body["coverage"]["runs"]) == 1
    run_a = 1
    assert env.get(f"/api/runs/{run_a}").status_code == 404


@pytest.mark.parametrize("q, found", [
    ("eleve", "Élève du réacteur"),
    ("المفاعل", "تصميم المُفاعِل"),
    ("输运", "中子输运方程"),
    ("REACTEUR", "Élève du réacteur"),
])
def test_normalised_matching(env, q, found):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Élève du réacteur", lang="fr"),
                      paper(2, "تصميم المُفاعِل", lang="ar"),
                      paper(3, "中子输运方程", lang="zh")])
    assert titles(local(env, q=q)) == [found]


def test_boolean_queries_phrases_and_fallback(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Neutron transport in reactors"),
                      paper(2, "Transport of neutron stars"),
                      paper(3, "Fusion plasma")])
    assert set(titles(local(env, q="reactors OR plasma"))) == {
        "Neutron transport in reactors", "Fusion plasma"}
    assert titles(local(env, q="neutron NOT stars")) == ["Neutron transport in reactors"]
    assert titles(local(env, q='"neutron transport"')) == ["Neutron transport in reactors"]
    assert set(titles(local(env, q="(reactors OR stars) AND neutron"))) == {
        "Neutron transport in reactors", "Transport of neutron stars"}
    # Malformed: falls back to every word.
    assert titles(local(env, q="(fusion plasma")) == ["Fusion plasma"]
    assert titles(local(env, q="neutron AND NOT stars")) == ["Neutron transport in reactors"]
    assert library_match("OR") == '"or"'  # an operator where a word is due is a word
    assert library_match("") == ""


def test_sort_orders(env):
    sign_in(env, "a@example.org")
    now = date.today().year
    remote(env, "x", [paper(1, "Old classic reactor", year=now - 19, cites=200, refs=[]),
                      paper(2, "Recent reactor", year=now, cites=50, refs=["W1"]),
                      paper(3, "Middle reactor", year=now - 4, cites=60, refs=["W1", "W2"])])
    assert titles(local(env, q="reactor", sort="citations"))[0] == "Old classic reactor"
    body = local(env, q="reactor", sort="velocity")
    assert titles(body) == ["Recent reactor", "Middle reactor", "Old classic reactor"]
    assert body["papers"][0]["velocity"] == 50 and body["papers"][2]["velocity"] == 10
    body = local(env, sort="local_citations")
    assert [(p["title"], p["local_citations"]) for p in body["papers"]] == [
        ("Old classic reactor", 2), ("Recent reactor", 1), ("Middle reactor", 0)]


def test_filters_counts_and_paging(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "A reactor", year=2010), paper(2, "B reactor", year=2020),
                      paper(3, "C reactor", year=2020, lang="fr"),
                      paper(4, "D reactor", year=2020, category="other")])
    body = local(env, q="reactor", year_from=2015, types="peer_reviewed", languages="en")
    assert titles(body) == ["B reactor"]
    body = local(env, q="reactor", limit=2, offset=2, sort="citations")
    assert body["total"] == 4 and len(body["papers"]) == 2
    assert body["by_year"] == {"2010": 1, "2020": 3}
    assert env.get("/api/library/search", params={"sort": "bogus"}).status_code == 422
    assert env.get("/api/library/search",
                   params={"year_from": 2020, "year_to": 2010}).status_code == 422


def test_a_saved_local_run_works_with_export_and_downloads(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Reactor one", cites=5), paper(2, "Reactor two", cites=9)])
    r = env.post("/api/library/runs", json={"query": "reactor", "sort": "citations"})
    assert r.status_code == 201
    run_id, summary = r.json()["id"], r.json()["summary"]
    assert summary["origin"] == "local_library" and summary["coverage"]["papers"] == 2
    detail = env.get(f"/api/runs/{run_id}").json()
    assert titles(detail) == ["Reactor two", "Reactor one"]
    assert {p["library_status"] for p in detail["papers"]} == {"local"}
    rows = list(csv.DictReader(io.StringIO(env.post(
        f"/api/runs/{run_id}/export", json={"format": "csv"}).content.decode("utf-8-sig"))))
    assert [r["Title"] for r in rows] == ["Reactor two", "Reactor one"]
    manifest = env.get("/api/downloads/manifest", params={"run_id": run_id}).json()
    assert manifest["total"] == 2
    xlsx = env.post(f"/api/runs/{run_id}/export", json={"format": "xlsx"})
    assert xlsx.status_code == 200


def test_local_mode_sends_nothing_and_does_not_use_the_search_budget(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Reactor one")])
    calls = env.libs["openalex"].calls
    for _ in range(4):  # more than searches_per_hour
        body = remote(env, "reactor", mode="local", topics=["T10001"])
    assert env.libs["openalex"].calls == calls
    assert body["summary"]["found"] == 1 and body["summary"]["coverage"]["scope"] == (
        "local_library")
    assert body["summary"]["unapplied_filters"] == {"local_library": ["topics"]}


def test_local_then_remote_counts_new_and_known(env):
    sign_in(env, "a@example.org")
    remote(env, "first", [paper(1, "Reactor one"), paper(2, "Reactor two")])
    env.libs["europepmc"].answers["reactor"] = [
        paper(9, "Reactor one (Europe PMC)", source="europepmc", doi="10.1/w1")]
    body = remote(env, "reactor", [paper(2, "Reactor two"), paper(3, "Reactor three")],
                  libraries=("openalex", "europepmc"), mode="local_then_remote")
    s = body["summary"]
    assert (s["local_hits"], s["remote_new"], s["remote_known"], s["remote_also_local"]) == (
        2, 1, 1, 1)
    assert s["coverage"]["papers"] == 2
    detail = env.get(f"/api/runs/{body['id']}").json()
    assert [(p["title"], p["library_status"]) for p in detail["papers"]][:2] == [
        ("Reactor one", "local"), ("Reactor two", "local")]
    status = {p["title"]: p["library_status"] for p in detail["papers"]}
    assert status["Reactor three"] == "new" and status["Reactor one (Europe PMC)"] == "local"


def test_plain_remote_runs_have_no_badge(env):
    sign_in(env, "a@example.org")
    run = remote(env, "x", [paper(1, "Reactor one")])
    detail = env.get(f"/api/runs/{run['id']}").json()
    assert detail["papers"][0]["library_status"] == "" and "coverage" not in detail["summary"]


def test_local_runs_list_no_library_and_have_their_own_limit(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Reactor one")])
    body = remote(env, "reactor", mode="local", libraries=("openalex",))
    request = env.get(f"/api/runs/{body['id']}").json()["request"]
    assert request["libraries"] == [] and request["libraries_requested"] == ["openalex"]
    assert isinstance(body["summary"]["coverage"]["runs"], dict)  # stored compactly
    with sqlite3.connect(env.app_db) as conn:
        conn.executemany("INSERT INTO search_runs(user_id, request, summary) VALUES(1, '{}', ?)",
                         [('{"origin": "local_library"}',)] * 120)
    r = env.post("/api/library/runs", json={"query": "reactor"})
    assert r.status_code == 429


def test_local_hits_ignore_the_search_language_and_match_doi_forms(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Reactor one", lang=""), paper(2, "Reactor two", lang="pt")])
    body = remote(env, "reactor", mode="local", languages=["en"])
    assert body["summary"]["found"] == 2
    env.libs["europepmc"].answers["reactor"] = [
        paper(9, "Same work", source="europepmc", doi="https://doi.org/10.1/W1 ")]
    body = remote(env, "reactor", libraries=("europepmc",), mode="local_then_remote")
    assert body["summary"]["remote_known"] == 1 and body["summary"]["remote_new"] == 0


def test_local_hits_are_capped_at_the_papers_wanted(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(i, f"Reactor {i}") for i in range(1, 6)])
    body = remote(env, "reactor", [], mode="local_then_remote", quota_total=2)
    assert body["summary"]["local_hits"] == 2


def test_relevance_without_words_lists_newest_first(env):
    sign_in(env, "a@example.org")
    remote(env, "x", [paper(1, "Old", year=2001), paper(2, "New", year=2021)])
    assert titles(local(env)) == ["New", "Old"]
