"""M32: incremental refresh of collections (new papers since the last refresh, stale citations)."""

import json
import sqlite3
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan import refresh as rf
from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors.arxiv import build_query as arxiv_query
from bayan.connectors.base import RequestBudget
from bayan.connectors.europepmc import build_query as epmc_query
from bayan.connectors.openalex import OpenAlexConnector
from bayan.db import init_db
from bayan.models import SearchProfile

PASSWORD = "correct horse battery"
NOW = date.today().year


def wd(w, year=NOW, cites=10, doi=None):
    return {"id": f"https://openalex.org/{w}", "title": f"Paper {w}", "publication_year": year,
            "doi": f"https://doi.org/{doi or '10.5555/' + w.lower()}", "type": "article",
            "language": "en", "cited_by_count": cites, "referenced_works": [],
            "counts_by_year": [],
            "primary_location": {"source": {"type": "journal", "display_name": "J. Tracks"}}}


class OpenAlexStub:
    """Answers OpenAlex requests from tables; records every request's parameters."""

    def __init__(self):
        self.initial = [wd("W1", NOW - 5, 100), wd("W2", NOW - 3, 30)]
        self.pub_pages: list[list[dict]] = [[]]
        self.created_pages: list[list[dict]] = [[]]
        self.known: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail = False
        self.first_page_only = False  # as OpenAlex without a key: later pages answer 401

    def __call__(self, request):
        params = dict(request.url.params)
        self.requests.append(params)
        if self.fail:
            return httpx.Response(500)
        if self.first_page_only and params.get("cursor", "*") != "*":
            return httpx.Response(401)
        flt = params.get("filter", "")
        if flt.startswith(("openalex:", "doi:")):
            values = flt.split(":", 1)[1].split("|")
            return httpx.Response(200, json={"results": [self.known[v] for v in values
                                                         if v in self.known], "meta": {}})
        pages = (self.pub_pages if "from_publication_date" in flt else
                 self.created_pages if "from_created_date" in flt else [self.initial])
        cursor = params.get("cursor", "*")
        i = 0 if cursor == "*" else int(cursor[1:])
        nxt = f"c{i + 1}" if i + 1 < len(pages) else None
        return httpx.Response(200, json={"results": pages[i], "meta": {"next_cursor": nxt}})


@pytest.fixture
def env(tmp_path):
    stub = OpenAlexStub()

    def factory(name):
        if name != "openalex":
            return None  # no Crossref DOI lookups in these tests
        return OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(stub)),
                                 sleep=lambda s: None, max_attempts=1)

    settings = Settings(database_path=tmp_path / "t.sqlite", job_resume_interval=0,
                        search_max_results=50)
    with TestClient(create_app(settings, connector_factory=factory)) as client:
        client.stub, client.db, client.factory = stub, settings.database_path, factory
        yield client


def sign_in(client, email):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    if r.status_code == 409:
        client.post("/api/auth/login", json={"email": email, "password": PASSWORD})


def make_collection(client):
    r = client.post("/api/search", json={
        "profile": {"query": "track", "year_from": 1990, "year_to": NOW, "languages": ["en"],
                    "document_types": ["peer_reviewed", "other"]},
        "libraries": ["openalex"]})
    assert r.status_code == 200, r.text
    r = client.post("/api/collections", json={"name": "Track", "from_run": r.json()["id"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def db(client):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    return conn


def refresh(client, cid, **body):
    r = client.post(f"/api/collections/{cid}/refresh", json=body)
    assert r.status_code == 202, r.text
    return history(client, cid)[0]


def history(client, cid):
    r = client.get(f"/api/collections/{cid}/refreshes")
    assert r.status_code == 200
    return r.json()["refreshes"]


def search_filters(stub):
    return [p["filter"] for p in stub.requests if "cursor" in p]


def test_date_filters_are_sent_with_the_overlap(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        conn.execute("UPDATE collections SET last_refreshed_at = '2026-05-10T08:00:00Z'")
    env.stub.requests.clear()
    done = refresh(env, cid, citations=False)
    assert done["since"] == "2026-04-10" and done["status"] == "done"
    filters = search_filters(env.stub)
    assert len(filters) == 2
    assert filters[0].endswith(",from_publication_date:2026-04-10")
    assert filters[1].endswith(",from_created_date:2026-04-10")
    assert all("publication_year:1990-" in f for f in filters)  # the stored profile applies
    with db(env) as conn:
        moved = conn.execute("SELECT last_refreshed_at FROM collections").fetchone()[0]
    assert moved > "2026-05-10T08:00:00Z"


def test_first_refresh_starts_from_the_newest_run_minus_the_overlap(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    done = refresh(env, cid, citations=False)
    assert done["since"] == (date.today() - timedelta(days=rf.OVERLAP_DAYS)).isoformat()


def test_only_new_works_are_added_and_flagged(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.stub.pub_pages = [[wd("W1", NOW - 5, 100), wd("W3", NOW, 12)]]
    # The same new work again, and a work whose DOI is already in the collection.
    env.stub.created_pages = [[wd("W3", NOW, 12), wd("W9", NOW, 1, doi="10.5555/w2")]]
    done = refresh(env, cid, citations=False)
    assert done["new_papers"] == 1
    with db(env) as conn:
        rows = conn.execute("SELECT paper_id, origin FROM collection_papers ORDER BY paper_id"
                            ).fetchall()
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (done["job_id"],)).fetchone()[0])
    assert [tuple(r) for r in rows] == [("W1", "run"), ("W2", "run"), ("W3", "refresh")]
    assert payload["already_in_collection"] == 2
    new = env.get(f"/api/collections/{cid}/new", params={"since": "2000-01-01"}).json()
    flags = {p["id"]: p["added_by_refresh"] for p in new["papers"]}
    assert flags == {"W1": False, "W2": False, "W3": True}
    assert new["coverage"]["scope"] == "local_library"


def test_removed_papers_stay_out(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.post(f"/api/collections/{cid}/papers",
             json={"remove": [{"source": "openalex", "id": "W2"}]})
    env.stub.pub_pages = [[wd("W2", NOW - 3, 30)]]
    assert refresh(env, cid, citations=False)["new_papers"] == 0


def test_budget_stops_the_job_and_the_next_refresh_carries_on(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        conn.execute("UPDATE collections SET last_refreshed_at = '2026-05-10T08:00:00Z'")
    env.stub.pub_pages = [[wd("W3")], [wd("W4")], [wd("W5")]]
    env.stub.requests.clear()
    first = refresh(env, cid, max_requests=2, citations=False)
    assert first["requests_made"] == 2 and len(search_filters(env.stub)) == 2
    assert first["status"] == "partial" and first["new_papers"] == 2
    assert first["shortfalls"][0]["reason"] == "budget"
    with db(env) as conn:
        assert conn.execute("SELECT last_refreshed_at FROM collections").fetchone()[0] == (
            "2026-05-10T08:00:00Z")
    second = refresh(env, cid, max_requests=10, citations=False)
    assert second["since"] == first["since"]  # same window again
    assert second["new_papers"] == 1 and second["status"] == "done"


def test_an_interrupted_job_resumes_within_its_budget(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.stub.pub_pages = [[wd("W3")], [wd("W4")]]
    with db(env) as conn:
        job = rf.queue_refresh(conn, cid, 1, 3, False)
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (job,)).fetchone()[0])
        payload["requests_made"] = 2  # two requests were sent before a crash
        conn.execute("UPDATE jobs SET payload = ?, status = 'running',"
                     " updated_at = datetime('now', '-2 hours') WHERE id = ?",
                     (json.dumps(payload), job))
    env.stub.requests.clear()
    assert rf.run_waiting_refreshes(env.db, env.factory, 50, 30, 900) == [job]
    assert len(env.stub.requests) == 1  # only the budget left
    done = history(env, cid)[0]
    assert done["requests_made"] == 3 and done["shortfalls"][0]["reason"] == "budget"


def test_citations_are_refreshed_only_for_stale_papers(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        conn.execute("UPDATE papers SET citations_updated_at = '2001-01-01T00:00:00Z'"
                     " WHERE id = 'W1'")
    env.stub.known = {"W1": {**wd("W1", NOW - 5, 150), "referenced_works":
                             ["https://openalex.org/W2"],
                             "counts_by_year": [{"year": NOW - 1, "cited_by_count": 50}]},
                      "W2": wd("W2", NOW - 3, 999)}
    papers = env.get(f"/api/collections/{cid}/papers").json()["papers"]
    assert {p["id"]: p["citations_stale"] for p in papers} == {"W1": True, "W2": False}
    env.stub.requests.clear()
    done = refresh(env, cid, max_requests=5)
    lookups = [p["filter"] for p in env.stub.requests if p["filter"].startswith("openalex:")]
    assert lookups == ["openalex:W1"]
    assert done["citations_updated"] == 1 and done["requests_made"] == 3
    with db(env) as conn:
        cites = dict(conn.execute("SELECT id, citations FROM papers").fetchall())
        refs = conn.execute("SELECT citing, cited FROM paper_references").fetchall()
    assert cites == {"W1": 150, "W2": 30} and [tuple(r) for r in refs] == [("W1", "W2")]
    lib = env.get("/api/library/search").json()["papers"]
    assert all(p["citations_stale"] is False for p in lib)
    stats = env.get(f"/api/collections/{cid}/stats").json()
    assert all(b["citations_stale"] is False for b in stats["top_cited"])


def test_a_failed_refresh_is_recorded_and_does_not_move_the_date(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.stub.fail = True
    done = refresh(env, cid)
    assert done["status"] == "failed" and "HTTP 500" in done["error"]
    assert done["finished_at"] is not None
    with db(env) as conn:
        assert conn.execute("SELECT last_refreshed_at FROM collections").fetchone()[0] is None
    env.stub.fail = False
    assert refresh(env, cid)["status"] == "done"
    assert [h["status"] for h in history(env, cid)] == ["done", "failed"]


def test_a_collection_without_a_profile_reports_it(env):
    sign_in(env, "a@example.org")
    cid = env.post("/api/collections", json={"name": "Empty"}).json()["id"]
    done = refresh(env, cid)
    assert done["status"] == "partial" and done["shortfalls"][0]["reason"] == "no_profile"
    assert done["requests_made"] == 0


def test_libraries_without_a_date_filter_are_skipped(env, tmp_path):
    class NoDates:
        name, tier = "openalex", "open"

        def search(self, profile):
            raise AssertionError("must not be searched")

    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        job = rf.queue_refresh(conn, cid, 1, 5, False)
        conn.commit()
    rf.run_refresh_job(env.db, job, lambda n: NoDates(), 50, 30)
    done = history(env, cid)[0]
    assert done["skipped"] == {"libraries_without_date_filter": ["openalex"]}
    assert done["shortfalls"][0]["reason"] == "no_library_with_date_filter"


def test_new_lists_papers_since_a_date_by_velocity(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.stub.pub_pages = [[wd("W3", NOW - 1, 10), wd("W4", NOW, 40)]]
    refresh(env, cid, citations=False)
    with db(env) as conn:
        conn.execute("UPDATE collection_papers SET added_at = '2001-01-01 00:00:00'"
                     " WHERE origin = 'run'")
    body = env.get(f"/api/collections/{cid}/new", params={"since": "2020-01-01"}).json()
    assert [p["id"] for p in body["papers"]] == ["W4", "W3"] and body["total"] == 2
    assert body["papers"][0]["velocity"] == 40
    assert env.get(f"/api/collections/{cid}/new", params={"since": "soon"}).status_code == 422


def test_one_refresh_at_a_time_and_owner_only(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        rf.queue_refresh(conn, cid, 1, 5, True)
        conn.commit()
    assert env.post(f"/api/collections/{cid}/refresh", json={}).status_code == 409
    sign_in(env, "b@example.org")
    assert env.post(f"/api/collections/{cid}/refresh", json={}).status_code == 404
    assert env.get(f"/api/collections/{cid}/refreshes").status_code == 404
    assert env.get(f"/api/collections/{cid}/new", params={"since": "2000-01-01"}
                   ).status_code == 404
    assert env.post(f"/api/collections/{cid}/refresh",
                    json={"max_requests": 0}).status_code == 422


def test_auto_refresh_is_off_by_default_and_queues_once(env):
    assert Settings(database_path=env.db).auto_refresh_days == 0
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        assert rf.queue_auto_refreshes(conn, 0, 20) == []
        assert rf.queue_auto_refreshes(conn, 7, 20) == []  # created today
        conn.execute("UPDATE collections SET created_at = datetime('now', '-30 days')")
        conn.commit()
        jobs = rf.queue_auto_refreshes(conn, 7, 20)
        assert len(jobs) == 1
        assert rf.queue_auto_refreshes(conn, 7, 20) == []  # one attempt per window
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (jobs[0],)).fetchone()[0])
    assert payload["max_requests"] == 20 and payload["collection_id"] == cid
    assert rf.run_waiting_refreshes(env.db, env.factory, 50, 30, 900) == jobs
    assert history(env, cid)[0]["trigger"] == "auto"


def test_other_libraries_build_their_date_filters():
    profile = SearchProfile(query="reactor", year_from=2000, year_to=2026)
    q = epmc_query(profile, "2026-04-10")
    assert "FIRST_PDATE:[2026-04-10 TO 3000-12-31]" in q
    assert "CREATION_DATE:[2026-04-10 TO 3000-12-31]" in q
    assert "submittedDate:[202604100000 TO 202612312359]" in arxiv_query(profile, "2026-04-10")
    assert "submittedDate:[200001010000" in arxiv_query(profile, "1999-01-01")


def test_a_refused_created_date_filter_is_reported():
    def handler(request):
        if "from_created_date" in request.url.params["filter"]:
            return httpx.Response(403)
        return httpx.Response(200, json={"results": [wd("W3")], "meta": {}})

    oa = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, max_attempts=1)
    budget = RequestBudget(5)
    got = list(oa.search_since(SearchProfile(query="x"), "2026-01-01", budget))
    assert [p.id for p in got] == ["W3"] and budget.used == 2
    assert "created-date" in oa.warnings[0]


def test_an_old_database_gets_the_refresh_table(tmp_path):
    path = tmp_path / "old.sqlite"
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE collection_refreshes")
        conn.execute("INSERT INTO users(email, password_hash) VALUES('x@example.org', 'h')")
    init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_refreshes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_a_track_that_was_current_goes_on_to_this_year(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        profile = json.loads(conn.execute("SELECT profile FROM collections").fetchone()[0])
        profile["year_to"] = NOW - 1  # stored last year
        conn.execute("UPDATE collections SET profile = ?, last_refreshed_at = ?",
                     (json.dumps(profile), f"{NOW - 1}-12-20T00:00:00Z"))
    env.stub.requests.clear()
    done = refresh(env, cid, citations=False)
    assert all(f"publication_year:1990-{NOW}," in f for f in search_filters(env.stub))
    with db(env) as conn:
        summary = json.loads(conn.execute("SELECT summary FROM search_runs WHERE id = ?",
                                          (done["run_id"],)).fetchone()[0])
    assert summary["year_to_extended"] == [NOW - 1, NOW]


def test_a_closed_period_keeps_its_end_year(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        profile = json.loads(conn.execute("SELECT profile FROM collections").fetchone()[0])
        profile["year_to"] = 2010
        conn.execute("UPDATE collections SET profile = ?", (json.dumps(profile),))
    env.stub.requests.clear()
    refresh(env, cid, citations=False)
    assert all("publication_year:1990-2010," in f for f in search_filters(env.stub))


def test_a_partial_first_refresh_does_not_move_the_start(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        conn.execute("UPDATE search_runs SET created_at = '2025-01-01 00:00:00'")
    env.stub.pub_pages = [[wd("W3")], [wd("W4")]]
    first = refresh(env, cid, max_requests=1, citations=False)
    assert first["since"] == "2024-12-02" and first["new_papers"] == 1
    assert refresh(env, cid, max_requests=1, citations=False)["since"] == "2024-12-02"


def test_a_first_page_only_answer_does_not_count_as_finished(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    env.stub.pub_pages = [[wd("W3")], [wd("W4")]]
    env.stub.first_page_only = True
    done = refresh(env, cid, citations=False)
    assert done["shortfalls"][0]["reason"] == "truncated"
    with db(env) as conn:
        assert conn.execute("SELECT last_refreshed_at FROM collections").fetchone()[0] is None


def test_retries_are_counted_against_the_budget():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    oa = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, max_attempts=5)
    budget = RequestBudget(3)
    with pytest.raises(Exception, match="budget"):
        list(oa.search_since(SearchProfile(query="x"), "2026-01-01", budget))
    assert len(calls) == 3 and budget.used == 3


def test_manual_refreshes_have_an_hourly_limit(env):
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    refresh(env, cid, max_requests=150, citations=False)
    r = env.post(f"/api/collections/{cid}/refresh", json={"max_requests": 100})
    assert r.status_code == 429


def test_the_auto_refresh_budget_is_kept_in_range(env):
    sign_in(env, "a@example.org")
    make_collection(env)
    with db(env) as conn:
        conn.execute("UPDATE collections SET created_at = datetime('now', '-30 days')")
        conn.commit()
        job = rf.queue_auto_refreshes(conn, 7, 5000)[0]
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id = ?",
                                          (job,)).fetchone()[0])
    assert payload["max_requests"] == rf.MAX_REFRESH_REQUESTS
