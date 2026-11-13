"""M31: collections per research track, stats and citation-network milestones."""

import csv
import io
import sqlite3
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors.openalex import OpenAlexConnector
from bayan.models import Paper
from bayan.network import build_graph, local_citations, main_paths

PASSWORD = "correct horse battery"
NOW = date.today().year

# Hand-worked graph. Edges point from the cited (older) work to the citing one:
#   A->B, A->C, B->D, C->D, D->E, C->E, plus E->A (A cites E: a cycle edge, dropped).
# Paths from the source A to the sink E: A-B-D-E, A-C-D-E, A-C-E (3).
#   N-: A=1 B=1 C=1 D=2 E=3     N+: E=1 D=1 C=2 B=1 A=3
#   SPC = N-(u) * N+(v): A-B=1, A-C=2, B-D=1, C-D=1, D-E=2, C-E=1
# Key routes from the two top edges: A-C -> forward C-D (tie with C-E, lower ID) -> D-E gives
# A,C,D,E; D-E -> backward B-D (tie with C-D, lower ID) -> A-B gives A,B,D,E.
A, B, C, D, E, F = "W1", "W2", "W3", "W4", "W5", "W6"
YEARS = {A: NOW - 10, B: NOW - 9, C: NOW - 9, D: NOW - 8, E: NOW - 1, F: NOW}
REFS = {A: [E], B: [A, "W100"], C: [A, "W100", "W200"], D: [B, C, "W100", "W200"],
        E: [D, C], F: []}


def test_search_path_counts_and_main_paths_by_hand():
    graph = build_graph(dict(YEARS), [(c, r) for c, rs in REFS.items() for r in rs])
    assert local_citations(graph) == {A: 2, B: 1, C: 2, D: 1, E: 1, F: 0}
    result = main_paths(graph, key_routes=3)
    assert result["spc"] == {(A, B): 1, (A, C): 2, (B, D): 1, (C, D): 1, (D, E): 2, (C, E): 1}
    assert result["dropped_edges"] == 1  # E -> A goes back in time
    assert result["routes"] == [[A, C, D, E], [A, B, D, E]]
    assert main_paths(graph, key_routes=1)["routes"] == [[A, C, D, E]]


def work(w, cites=10, counts=None, refs=True):
    return Paper(source="openalex", id=w, openalex_id=w, doi=f"10.5555/{w.lower()}",
                 title=f"Paper {w}", year=YEARS.get(w, NOW - 5), citations=cites,
                 category="peer_reviewed", language="en", venue="J. Tracks",
                 referenced_works=REFS.get(w, []) if refs else None, counts_by_year=counts)


class Lib:
    name, tier = "openalex", "open"
    supported_filters = frozenset({"years", "document_types", "languages"})

    def __init__(self):
        self.answers = {}

    def search(self, profile):
        yield from self.answers.get(profile.query, [])


@pytest.fixture
def env(tmp_path):
    lib = Lib()
    lib.answers["track"] = [work(A, 200), work(B, 50), work(C, 80), work(D, 30), work(E, 40),
                            work(F, 5, counts=[{"year": NOW - y, "cited_by_count": 30}
                                               for y in (1, 2, 3)])]
    lib.answers["other"] = [work("W7", 1, refs=False)]
    known = {"W100": {"id": "https://openalex.org/W100", "title": "Founding work",
                      "publication_year": NOW - 30, "type": "article", "cited_by_count": 900,
                      "referenced_works": [], "counts_by_year": []}}

    def handler(request):
        values = request.url.params["filter"].split(":", 1)[1].split("|")
        lib.lookups.append(values)
        return httpx.Response(200, json={"results": [known[v] for v in values if v in known],
                                         "meta": {}})

    lib.lookups = []
    oa = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, max_attempts=1)

    def factory(name, user_id=None):
        if name == "openalex":
            return Proxy(lib, oa)
        return None

    settings = Settings(database_path=tmp_path / "t.sqlite", job_resume_interval=0)
    with TestClient(create_app(settings, connector_factory=factory)) as client:
        client.lib = lib
        client.db = settings.database_path
        yield client


class Proxy:
    """Searches with the fake library, looks works up with the mocked OpenAlex connector."""

    name, tier = "openalex", "open"
    supported_filters = Lib.supported_filters

    def __init__(self, lib, oa):
        self.search, self.lookup = lib.search, oa.lookup


def sign_in(client, email="a@example.org"):
    client.post("/api/auth/logout")
    if client.post("/api/auth/register", json={"email": email, "password": PASSWORD}
                   ).status_code == 409:
        client.post("/api/auth/login", json={"email": email, "password": PASSWORD})


def search(client, query):
    r = client.post("/api/search", json={"profile": {"query": query, "year_from": 1990,
                                                     "year_to": NOW, "languages": ["en"]},
                                         "libraries": ["openalex"]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def make(client, query="track"):
    run_id = search(client, query)
    r = client.post("/api/collections", json={"name": "Track", "from_run": run_id})
    assert r.status_code == 201, r.text
    return r.json(), run_id


def test_create_from_a_run_keeps_its_profile_and_adding_twice_is_idempotent(env):
    sign_in(env)
    col, run_id = make(env)
    assert col["papers"] == 6 and col["profile"]["query"] == "track"
    assert col["libraries"] == ["openalex"]
    again = env.post(f"/api/collections/{col['id']}/runs", json={"run_id": run_id}).json()
    assert again == {"added": 0, "already": 6, "skipped_removed": 0}
    other = search(env, "other")
    one = env.post(f"/api/collections/{col['id']}/papers",
                   json={"add": [{"source": "openalex", "id": "W7"}]}).json()
    assert one["added"] == 1
    twice = env.post(f"/api/collections/{col['id']}/papers",
                     json={"add": [{"source": "openalex", "id": "W7"}]}).json()
    assert twice == {"added": 0, "already": 1, "removed": 0}
    assert other
    gone = env.post(f"/api/collections/{col['id']}/papers",
                    json={"remove": [{"source": "openalex", "id": "W7"}]}).json()
    assert gone["removed"] == 1
    assert env.post(f"/api/collections/{col['id']}/papers",
                    json={"add": [{"source": "openalex", "id": "W999"}]}).status_code == 404


def test_milestones_on_the_hand_worked_graph(env):
    sign_in(env)
    col, _ = make(env)
    m = env.get(f"/api/collections/{col['id']}/milestones",
                params={"min_cocite": 3, "recent": 3}).json()
    assert [[p["openalex_id"] for p in route] for route in m["main_paths"]] == [
        [A, C, D, E], [A, B, D, E]]
    assert [p["spc_in"] for p in m["main_paths"][0]] == [None, 2, 1, 2]
    why = {p["openalex_id"]: p["why"] for p in m["milestones"]}
    assert set(why) == {A, B, C, D, E}
    assert why[A] == ["main_path", "top_5pct_local_citations"]  # LCS 2, shared top
    assert why[C] == ["main_path", "top_5pct_local_citations"] and why[B] == ["main_path"]
    assert m["dropped_edges"] == 1 and m["edges"] == 7
    assert m["foundations"] == [{"openalex_id": "W100", "cited_by_collection": 3,
                                 "title": None, "year": None}]
    # Frontier: works from the last 3 years, by max(recent velocity, velocity).
    # F: recent velocity 30 (30 a year), velocity 5. E: velocity 40 / 2 = 20.
    assert [(f["openalex_id"], f["score"]) for f in m["frontier"]] == [(F, 30.0), (E, 20.0)]
    assert m["frontier"][1]["cites_milestones"] == [C, D]
    assert m["references_coverage"]["share"] == 1.0 and "note" not in m["references_coverage"]
    assert m["coverage"]["scope"] == "local_library" and m["coverage"]["papers"] == 6


def test_low_reference_coverage_adds_the_backfill_note(env):
    sign_in(env)
    run_id = search(env, "other")
    col = env.post("/api/collections", json={"name": "Thin", "from_run": run_id}).json()
    m = env.get(f"/api/collections/{col['id']}/milestones").json()
    assert m["references_coverage"]["share"] == 0.0 and "backfill" in (
        m["references_coverage"]["note"])
    assert m["main_paths"] == [] and m["milestones"] == []


def test_stats(env):
    sign_in(env)
    col, _ = make(env)
    s = env.get(f"/api/collections/{col['id']}/stats", params={"top": 2}).json()
    assert s["papers"] == 6 and sum(s["by_year"].values()) == 6
    assert s["by_year"][str(NOW - 9)] == 2
    assert s["top_venues"] == [{"venue": "J. Tracks", "papers": 6}]
    assert [p["id"] for p in s["top_cited"]] == [A, C]
    assert [p["id"] for p in s["top_velocity"]] == [E, A]  # 40/2=20, 200/11=18.2
    assert [(p["id"], p["local_citations"]) for p in s["top_local_citations"]] == [
        (A, 2), (C, 2)]
    assert s["references_coverage"]["share"] == 1.0 and s["coverage"]["papers"] == 6


def test_papers_export_and_a_run_from_the_collection(env):
    sign_in(env)
    col, _ = make(env)
    cid = col["id"]
    body = env.get(f"/api/collections/{cid}/papers",
                   params={"sort": "local_citations", "limit": 2}).json()
    assert body["total"] == 6 and [p["id"] for p in body["papers"]] == [A, C]
    assert body["coverage"]["papers"] == 6
    rows = list(csv.DictReader(io.StringIO(env.post(
        f"/api/collections/{cid}/export", json={"sort": "citations"}).content.decode("utf-8-sig"))))
    assert rows[0]["OpenAlex ID"] == A and len(rows) == 6
    assert env.post(f"/api/collections/{cid}/export", json={"format": "xlsx"}).status_code == 200
    r = env.post(f"/api/collections/{cid}/runs/new", json={"year_from": NOW - 3})
    assert r.status_code == 201
    detail = env.get(f"/api/runs/{r.json()['id']}").json()
    assert sorted(p["id"] for p in detail["papers"]) == [E, F]
    assert detail["request"]["collection_id"] == cid


def test_fetching_foundations_adds_them_to_the_collection_and_library(env):
    sign_in(env)
    col, _ = make(env)
    r = env.post(f"/api/collections/{col['id']}/foundations", json={"min_cocite": 2}).json()
    assert env.lib.lookups == [["W100", "W200"]]
    assert r == {"foundations": 2, "requests_made": 1, "added": 1, "not_found": ["W200"],
                 "remaining": 0}
    papers = env.get(f"/api/collections/{col['id']}/papers", params={"q": "founding"}).json()
    assert [p["id"] for p in papers["papers"]] == ["W100"]
    assert [p["title"] for p in env.get("/api/library/search",
                                        params={"q": "founding"}).json()["papers"]] == [
        "Founding work"]


def test_collections_are_owner_only(env):
    sign_in(env)
    col, run_id = make(env)
    cid = col["id"]
    sign_in(env, "b@example.org")
    assert env.get("/api/collections").json() == []
    for method, path, body in [
        ("get", f"/api/collections/{cid}", None),
        ("patch", f"/api/collections/{cid}", {"name": "x"}),
        ("delete", f"/api/collections/{cid}", None),
        ("post", f"/api/collections/{cid}/runs", {"run_id": run_id}),
        ("post", f"/api/collections/{cid}/papers", {"add": []}),
        ("get", f"/api/collections/{cid}/papers", None),
        ("post", f"/api/collections/{cid}/export", {}),
        ("post", f"/api/collections/{cid}/runs/new", {}),
        ("get", f"/api/collections/{cid}/stats", None),
        ("get", f"/api/collections/{cid}/milestones", None),
        ("post", f"/api/collections/{cid}/foundations", {}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert env.request(method.upper(), path, **kwargs).status_code == 404, path
    # B cannot build a collection from A's run, and A's collection papers are not B's library.
    assert env.post("/api/collections", json={"name": "x", "from_run": run_id}
                    ).status_code == 404
    assert env.get("/api/library/search").json()["total"] == 0


def test_edit_and_delete(env):
    sign_in(env)
    col, _ = make(env)
    cid = col["id"]
    assert env.patch(f"/api/collections/{cid}", json={"name": "CANDU lattice",
                                                      "description": "d"}).json()["name"] == (
        "CANDU lattice")
    assert env.delete(f"/api/collections/{cid}").json() == {"deleted": cid}
    assert env.get(f"/api/collections/{cid}").status_code == 404


def test_a_removed_paper_stays_out_when_its_run_is_added_again(env):
    sign_in(env)
    col, run_id = make(env)
    cid = col["id"]
    env.post(f"/api/collections/{cid}/papers",
             json={"remove": [{"source": "openalex", "id": F}, {"source": "openalex", "id": F}]})
    again = env.post(f"/api/collections/{cid}/runs", json={"run_id": run_id}).json()
    assert again == {"added": 0, "already": 5, "skipped_removed": 1}
    # Adding it by hand brings it back.
    assert env.post(f"/api/collections/{cid}/papers",
                    json={"add": [{"source": "openalex", "id": F}]}).json()["added"] == 1


def test_stats_count_works_not_records(env):
    sign_in(env)
    col, _ = make(env)
    env.lib.answers["dup"] = [Paper(source="europepmc", id="E1", doi="10.5555/w1",
                                    title="Paper W1 (Europe PMC)", year=YEARS[A], citations=1,
                                    category="peer_reviewed", language="en")]
    run_id = search(env, "dup")
    env.post(f"/api/collections/{col['id']}/runs", json={"run_id": run_id})
    s = env.get(f"/api/collections/{col['id']}/stats").json()
    assert (s["papers"], s["records"]) == (6, 7)
    assert sum(s["by_year"].values()) == 6


def test_foundations_are_marked_in_coverage_and_lookups_have_an_hourly_limit(env):
    sign_in(env)
    col, _ = make(env)
    env.post(f"/api/collections/{col['id']}/foundations", json={"min_cocite": 2})
    cov = env.get("/api/library/search").json()["coverage"]
    assert cov["fetched_by_id"] == 1 and "fetched by ID" in cov["note"]
    with sqlite3.connect(env.db) as conn:
        conn.execute("INSERT INTO lookup_attempts(user_id, requests) VALUES(1, 19)")
    r = env.post(f"/api/collections/{col['id']}/foundations", json={"min_cocite": 2})
    assert r.status_code == 429  # 1 + 19 requests used this hour


def test_large_limits_only_for_exports(env):
    sign_in(env)
    col, _ = make(env)
    assert env.get("/api/library/search", params={"limit": 501}).status_code == 422
    assert env.get(f"/api/collections/{col['id']}/papers",
                   params={"limit": 501}).status_code == 422
    assert env.post(f"/api/collections/{col['id']}/export",
                    json={"limit": 5000}).status_code == 200
