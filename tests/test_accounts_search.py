import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors import ConnectorError
from bayan.models import Paper

EMAIL, PASSWORD = "ada@example.org", "correct horse battery"


class FakeConnector:
    name = "openalex"
    tier = "open"
    supported_filters = frozenset({"years", "document_types", "languages", "countries"})

    def __init__(self, papers=None, error=None):
        self.papers, self.error, self.profiles = papers or [], error, []

    def search(self, profile):
        self.profiles.append(profile)
        if self.error:
            raise self.error
        yield from self.papers


def make_paper(i, year=2020, **kw):
    return Paper(source="openalex", id=f"W{i}", doi=f"10.1/{i}", title=f"Paper {i}", year=year,
                 native_type="article", category="peer_reviewed", language="en",
                 oa_url=kw.pop("oa_url", ""), **kw)


@pytest.fixture
def setup(tmp_path):
    fake = FakeConnector([make_paper(i, 2020 + i % 2) for i in range(10)])
    settings = Settings(database_path=tmp_path / "t.sqlite", search_max_results=50,
                        searches_per_hour=3)
    app = create_app(settings, connector_factory=lambda lib: fake)
    with TestClient(app) as client:
        yield client, fake, app


def register(client, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def search(client, **extra):
    body = {"profile": {"query": "reactor", "year_from": 2020, "year_to": 2021}, **extra}
    return client.post("/api/search", json=body)


def test_register_login_logout_cycle(setup):
    client, _, _ = setup
    assert client.get("/api/me").status_code == 401
    assert register(client).status_code == 201
    assert client.get("/api/me").json() == {"email": EMAIL}
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401
    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    assert client.get("/api/me").status_code == 200


def test_register_validation_and_duplicates(setup):
    client, _, _ = setup
    assert register(client, password="short").status_code == 422
    assert register(client, email="not-an-email").status_code == 422
    assert register(client).status_code == 201
    assert register(client, email="ADA@example.org").status_code == 409


def test_login_failures_do_not_reveal_which_part_was_wrong(setup):
    client, _, _ = setup
    register(client)
    wrong_pw = client.post("/api/auth/login", json={"email": EMAIL, "password": "nope-nope-nope"})
    no_user = client.post("/api/auth/login", json={"email": "x@y.org", "password": PASSWORD})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_password_is_not_stored_in_clear(setup, tmp_path):
    client, _, _ = setup
    register(client)
    from bayan.db import connect
    row = connect(tmp_path / "t.sqlite").execute("SELECT password_hash FROM users").fetchone()
    assert PASSWORD not in row[0] and row[0].startswith("scrypt$")


def test_search_requires_login(setup):
    client, _, _ = setup
    assert search(client).status_code == 401


def test_search_stores_run_and_returns_papers(setup):
    client, fake, _ = setup
    register(client)
    run = search(client, libraries=["openalex"]).json()
    assert run["summary"]["found"] == 10 and run["summary"]["stopped"] == "exhausted"
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert len(detail["papers"]) == 10 and detail["papers"][0]["access"] == "no_open_copy_listed"
    assert detail["summary"]["by_category"] == {"peer_reviewed": 10}
    assert detail["summary"]["filtered_by_type"] == 0
    assert detail["papers"][0]["category"] == "peer_reviewed"
    assert detail["papers"][0]["native_type"] == "article"
    assert fake.profiles[0].query == "reactor"
    assert [r["id"] for r in client.get("/api/runs").json()] == [run["id"]]


def test_open_copy_is_described_as_listed_not_downloaded(setup):
    client, fake, _ = setup
    fake.papers = [make_paper(1, oa_url="https://x/p.pdf")]
    register(client)
    run = search(client).json()
    assert client.get(f"/api/runs/{run['id']}").json()["papers"][0]["access"] == "open_copy_listed"


def test_quotas_report_shortfalls_without_padding(setup):
    client, fake, _ = setup
    fake.papers = [make_paper(i, 2020) for i in range(6)]
    register(client)
    run = search(client, quota_total=10).json()
    s = run["summary"]
    assert s["found"] == 5 and s["skipped_over_quota"] == 1
    assert s["shortfalls"] == {"2021": {"quota": 5, "collected": 0}}
    assert s["stopped"] == "exhausted"


def test_result_limit_is_reported(setup):
    client, fake, _ = setup
    fake.papers = [make_paper(i) for i in range(80)]
    register(client)
    s = search(client).json()["summary"]
    assert s["found"] == 50 and s["stopped"] == "limit"


def test_unavailable_or_unknown_library_is_refused(setup):
    client, _, _ = setup
    register(client)
    r = search(client, libraries=["ieee"])
    assert r.status_code == 422 and "not available yet" in r.json()["detail"]
    assert search(client, libraries=["nope"]).status_code == 422
    assert search(client, quota_total=500).status_code == 422


def test_connector_failure_becomes_502_without_details(setup):
    client, fake, _ = setup
    fake.error = ConnectorError("OpenAlex request failed: HTTP 503")
    register(client)
    r = search(client)
    assert r.status_code == 502 and "503" in r.json()["detail"]


def test_runs_are_private_to_each_user(setup):
    client, _, app = setup
    register(client)
    run_id = search(client).json()["id"]
    other = TestClient(app)
    register(other, email="bob@example.org")
    assert other.get(f"/api/runs/{run_id}").status_code == 404
    assert other.get("/api/runs").json() == []


def test_hourly_search_limit(setup):
    client, _, _ = setup
    register(client)
    assert [search(client).status_code for _ in range(3)] == [200, 200, 200]
    assert search(client).status_code == 429


def test_invalid_profile_is_rejected(setup):
    client, _, _ = setup
    register(client)
    bad = {"profile": {"languages": ["ru"]}}
    assert client.post("/api/search", json=bad).status_code == 422


def test_web_page_is_served(setup):
    client, _, _ = setup
    r = client.get("/")
    assert r.status_code == 200 and "BAYAN" in r.text
