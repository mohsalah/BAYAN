"""M29: the citation network (references, yearly citations) and the backfill job."""

import csv
import io
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors.openalex import SELECT, OpenAlexConnector, parse_work
from bayan.db import connect, init_db
from bayan.library import apply_work, resume_stale_backfills
from bayan.models import Paper
from bayan.search import _store_paper

PASSWORD = "correct horse battery"
OLD_PAPERS = """
CREATE TABLE papers(
  source TEXT NOT NULL, id TEXT NOT NULL, doi TEXT, title TEXT, abstract TEXT, year INTEGER,
  document_type TEXT, language TEXT, venue TEXT, first_author TEXT, countries TEXT,
  citations INTEGER, is_oa INTEGER, oa_url TEXT, landing_url TEXT, keywords TEXT, topic TEXT,
  PRIMARY KEY(source, id)
);
"""


def work(i, refs=None, network=True):
    w = {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/10.1/w{i}",
         "title": f"Work {i}", "publication_year": 2020, "type": "article",
         "cited_by_count": 100 + i, "language": "en"}
    if network:
        w["referenced_works"] = [f"https://openalex.org/W{r}" for r in (refs or [])]
        w["counts_by_year"] = [{"year": 2024, "cited_by_count": 3},
                               {"year": 2023, "cited_by_count": 2}]
    return w


def test_parse_work_maps_the_network_fields():
    p = parse_work(work(7, refs=[1, 2, 2]))
    assert p.openalex_id == "W7" and p.referenced_works == ["W1", "W2"]
    assert p.counts_by_year == [{"year": 2023, "cited_by_count": 2},
                                {"year": 2024, "cited_by_count": 3}]
    bare = parse_work(work(8, network=False))
    assert bare.referenced_works is None and bare.counts_by_year is None
    assert "referenced_works" in SELECT and "counts_by_year" in SELECT


def refs_of(conn, citing):
    return sorted(r[0] for r in conn.execute(
        "SELECT cited FROM paper_references WHERE citing = ?", (citing,)))


def test_references_are_replaced_only_when_a_list_is_supplied(tmp_path):
    init_db(tmp_path / "db.sqlite")
    conn = connect(tmp_path / "db.sqlite")
    _store_paper(conn, parse_work(work(1, refs=[2, 3])))
    assert refs_of(conn, "W1") == ["W2", "W3"]
    _store_paper(conn, parse_work(work(1, network=False)))  # a later record without a list
    assert refs_of(conn, "W1") == ["W2", "W3"]
    row = conn.execute("SELECT references_count, counts_by_year, openalex_id,"
                       " citations_updated_at FROM papers WHERE id = 'W1'").fetchone()
    assert row["references_count"] == 2 and row["openalex_id"] == "W1"
    assert json.loads(row["counts_by_year"])[0]["year"] == 2023 and row["citations_updated_at"]
    _store_paper(conn, parse_work(work(1, refs=[4])))
    assert refs_of(conn, "W1") == ["W4"]


def test_other_libraries_records_share_the_w_id_by_doi(tmp_path):
    init_db(tmp_path / "db.sqlite")
    conn = connect(tmp_path / "db.sqlite")
    _store_paper(conn, Paper(source="crossref", id="10.1/w1", doi="10.1/w1", title="A"))
    _store_paper(conn, parse_work(work(1, refs=[2])))  # matched afterwards
    _store_paper(conn, Paper(source="europepmc", id="E1", doi="10.1/w1", title="A"))  # before
    ids = dict(conn.execute("SELECT source, openalex_id FROM papers").fetchall())
    assert ids == {"crossref": "W1", "openalex": "W1", "europepmc": "W1"}
    # arXiv reports no citation count, so storing its record is not a citation check.
    _store_paper(conn, Paper(source="arxiv", id="2101.1", title="B"))
    assert conn.execute("SELECT citations_updated_at FROM papers WHERE source = 'arxiv'"
                        ).fetchone()[0] is None


def test_an_old_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_PAPERS)
        conn.execute("INSERT INTO papers(source, id, title, citations, countries, keywords)"
                     " VALUES('openalex', 'W1', 'Old', 5, '[]', '[]')")
    init_db(path)
    init_db(path)  # a second run changes nothing
    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)")]
        row = conn.execute("SELECT title, citations, openalex_id, references_count FROM papers"
                           ).fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"openalex_id", "counts_by_year", "citations_updated_at", "references_count"} <= set(
        cols) and cols.count("openalex_id") == 1
    assert row == ("Old", 5, None, None)
    assert {"paper_references", "paper_references_cited", "papers_openalex_id"} <= tables


class OpenAlexWorld:
    """A mock OpenAlex: text searches return records without network data (as stored before
    M29); ID and DOI lookups return the full record. Records every request's filter."""

    def __init__(self, search_works, known, fail=False):
        self.search_works, self.known, self.fail = search_works, known, fail
        self.lookups: list[list[str]] = []

    def handler(self, request):
        params = parse_qs(urlparse(str(request.url)).query)
        f = params["filter"][0]
        if f.startswith(("openalex:", "doi:")):
            kind, values = f.split(":", 1)
            values = values.split("|")
            self.lookups.append(values)
            if self.fail:
                return httpx.Response(403)
            hits = [self.known[v] for v in values if v in self.known]
            return httpx.Response(200, json={"results": hits, "meta": {}})
        return httpx.Response(200, json={"results": self.search_works, "meta": {}})


class Doi:
    """A library whose records carry a DOI only (no W-id)."""
    name, tier = "europepmc", "open"
    supported_filters = frozenset({"years", "document_types", "languages"})

    def __init__(self, dois):
        self.dois = dois

    def search(self, profile):
        for d in self.dois:
            yield Paper(source="europepmc", id=d, doi=d, title=d, year=2020,
                        category="peer_reviewed", language="en")


def make_client(tmp_path, world, dois=()):
    oa = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(world.handler)),
                           sleep=lambda s: None, max_attempts=1)
    by_name = {"openalex": oa, "europepmc": Doi(list(dois))}
    settings = Settings(database_path=tmp_path / "t.sqlite", search_max_results=500,
                        job_resume_interval=0)
    return TestClient(create_app(settings, connector_factory=lambda lib: by_name.get(lib)))


def register_and_search(client, email="a@example.org", libraries=("openalex",)):
    client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    res = client.post("/api/search", json={"profile": {"query": "r", "year_from": 2020,
                                                       "year_to": 2020, "languages": ["en"],
                                                       "document_types": ["peer_reviewed",
                                                                          "other"]},
                                           "libraries": list(libraries)})
    assert res.status_code == 200, res.text
    return res.json()["id"]


@pytest.fixture
def world():
    search = [work(i, network=False) for i in range(1, 121)]
    known = {f"W{i}": work(i, refs=[i + 1000]) for i in range(1, 120)}  # W120 is unknown
    known["10.5555/c1"] = {**work(9001, refs=[1]), "doi": "https://doi.org/10.5555/c1"}
    return OpenAlexWorld(search, known)


def test_backfill_batches_50_ids_stops_at_the_budget_and_resumes(tmp_path, world):
    with make_client(tmp_path, world, dois=["10.5555/c1", "10.5555/c2"]) as client:
        register_and_search(client, libraries=("openalex", "europepmc"))
        first = client.post("/api/library/backfill", json={"max_requests": 2}).json()
        job = client.get(f"/api/library/backfill/jobs/{first['job_id']}").json()
        assert [len(v) for v in world.lookups] == [50, 50]
        assert job["status"] == "done" and job["requests_made"] == 2
        assert job["updated"] + job["not_found"] == 100  # IDs go in text order (W1, W10, ...)
        before = job["updated"], job["not_found"]
        assert job["remaining"] == 22  # 20 W-ids and two DOIs
        second = client.post("/api/library/backfill", json={"max_requests": 5}).json()
        job = client.get(f"/api/library/backfill/jobs/{second['job_id']}").json()
    assert [len(v) for v in world.lookups] == [50, 50, 20, 2]
    assert world.lookups[3] == ["10.5555/c1", "10.5555/c2"]
    assert job["requests_made"] == 2 and job["remaining"] == 0
    # Over both jobs: W1-W119 and the DOI 10.5555/c1 found; W120 and 10.5555/c2 not found.
    assert (before[0] + job["updated"], before[1] + job["not_found"]) == (120, 2)
    with sqlite3.connect(tmp_path / "t.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        w5 = conn.execute("SELECT * FROM papers WHERE id = 'W5'").fetchone()
        c1 = conn.execute("SELECT * FROM papers WHERE id = '10.5555/c1'").fetchone()
        assert refs_of(conn, "W5") == ["W1005"] and w5["references_count"] == 1
        assert w5["citations_updated_at"] and json.loads(w5["counts_by_year"])
        assert c1["openalex_id"] == "W9001" and c1["citations"] == 9101


def test_a_third_backfill_does_not_look_up_not_found_papers_again(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        client.post("/api/library/backfill", json={"max_requests": 10})
        sent = len(world.lookups)
        again = client.post("/api/library/backfill", json={"max_requests": 10}).json()
    assert len(world.lookups) == sent and again["status"] == "nothing_to_do"


def test_an_interrupted_backfill_resumes_within_its_budget(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
    path = tmp_path / "t.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO jobs(kind, payload, status, updated_at) VALUES("
                     "'library_backfill', ?, 'running', datetime('now', '-1 hour'))",
                     (json.dumps({"user_id": 1, "max_requests": 2, "requests_made": 1,
                                  "updated": 0, "not_found": 0}),))
    oa = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(world.handler)),
                           sleep=lambda s: None)
    assert resume_stale_backfills(path, lambda: oa, 900) == [1]
    assert len(world.lookups) == 1  # one request left of two
    assert resume_stale_backfills(path, lambda: oa, 900) == []


def test_a_failed_lookup_stops_the_job_with_the_reason(tmp_path, world):
    world.fail = True
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        job_id = client.post("/api/library/backfill", json={"max_requests": 3}).json()["job_id"]
        job = client.get(f"/api/library/backfill/jobs/{job_id}").json()
    assert job["status"] == "failed" and "HTTP 403" in job["error"]
    assert job["requests_made"] == 1 and job["remaining"] == 120


def test_backfill_is_owner_only(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        job_id = client.post("/api/library/backfill", json={"max_requests": 1}).json()["job_id"]
        client.post("/api/auth/logout")
        client.post("/api/auth/register", json={"email": "b@example.org", "password": PASSWORD})
        assert client.get(f"/api/library/backfill/jobs/{job_id}").status_code == 404
        # User B has no runs: none of A's papers are B's to backfill.
        assert client.post("/api/library/backfill", json={}).json()["status"] == "nothing_to_do"


def test_budget_limits(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        assert client.post("/api/library/backfill", json={"max_requests": 0}).status_code == 422
        assert client.post("/api/library/backfill", json={"max_requests": 201}).status_code == 422


def test_export_has_the_network_columns(tmp_path, world):
    with make_client(tmp_path, world) as client:
        run_id = register_and_search(client)
        client.post("/api/library/backfill", json={"max_requests": 1})
        body = client.post(f"/api/runs/{run_id}/export", json={"format": "csv"}).content
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    by_title = {r["Title"]: r for r in rows}
    assert by_title["Work 1"]["OpenAlex ID"] == "W1"
    assert by_title["Work 1"]["References (count)"] == "1"
    assert by_title["Work 1"]["Citations updated"].endswith("Z")
    assert by_title["Work 99"]["References (count)"] == "not stored"


def test_a_merged_w_id_is_looked_up_again_by_doi(tmp_path):
    # OpenAlex merged W1 into W77: the ID lookup misses, the DOI lookup finds the new work.
    merged = {**work(77, refs=[5]), "doi": "https://doi.org/10.5555/m1"}
    world = OpenAlexWorld([{**work(1, network=False), "doi": "https://doi.org/10.5555/m1"}],
                          {"10.5555/m1": merged})
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        job_id = client.post("/api/library/backfill", json={"max_requests": 5}).json()["job_id"]
        job = client.get(f"/api/library/backfill/jobs/{job_id}").json()
    assert world.lookups == [["W1"], ["10.5555/m1"]]
    assert job["updated"] == 1 and job["not_found"] == 0 and job["remaining"] == 0
    with sqlite3.connect(tmp_path / "t.sqlite") as conn:
        assert conn.execute("SELECT openalex_id FROM papers").fetchone()[0] == "W77"
        assert refs_of(conn, "W77") == ["W5"]


def test_a_lookup_without_a_reference_list_keeps_the_stored_references(tmp_path):
    init_db(tmp_path / "db.sqlite")
    conn = connect(tmp_path / "db.sqlite")
    _store_paper(conn, parse_work(work(1, refs=[2, 3])))
    apply_work(conn, parse_work(work(1, network=False)), [("openalex", "W1")])
    assert refs_of(conn, "W1") == ["W2", "W3"]
    row = conn.execute("SELECT references_count, openalex_status FROM papers").fetchone()
    assert tuple(row) == (2, "no_reference_list")


def test_a_record_linked_by_doi_takes_the_stored_references_and_is_looked_up_again(tmp_path):
    init_db(tmp_path / "db.sqlite")
    conn = connect(tmp_path / "db.sqlite")
    conn.execute("INSERT INTO papers(source, id, doi, openalex_status) VALUES("
                 "'europepmc', 'E1', '10.1/w1', 'not_found')")  # an earlier failed DOI lookup
    _store_paper(conn, parse_work(work(1, refs=[2])))
    row = conn.execute("SELECT openalex_id, references_count, openalex_status FROM papers"
                       " WHERE id = 'E1'").fetchone()
    assert tuple(row) == ("W1", 1, None)
    _store_paper(conn, Paper(source="crossref", id="10.1/w1", doi="10.1/w1"))  # stored after
    assert conn.execute("SELECT references_count FROM papers WHERE source = 'crossref'"
                        ).fetchone()[0] == 1


def test_a_failing_connector_does_not_strand_an_interrupted_job(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
    path = tmp_path / "t.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO jobs(kind, payload, status, updated_at) VALUES("
                     "'library_backfill', ?, 'running', datetime('now', '-1 hour'))",
                     (json.dumps({"user_id": 1, "max_requests": 2, "requests_made": 0,
                                  "updated": 0, "not_found": 0}),))

    def broken():
        raise RuntimeError("no connector")

    assert resume_stale_backfills(path, broken, 900) == []
    with sqlite3.connect(path) as conn:
        # Still stale, so the next pass (with a working connector) takes it.
        assert conn.execute("SELECT updated_at < datetime('now', '-10 minutes') FROM jobs"
                            ).fetchone()[0] == 1


def test_a_second_backfill_while_one_runs_is_refused(tmp_path, world):
    with make_client(tmp_path, world) as client:
        register_and_search(client)
        with sqlite3.connect(tmp_path / "t.sqlite") as conn:
            conn.execute("INSERT INTO jobs(kind, payload, status) VALUES('library_backfill', ?,"
                         " 'running')", (json.dumps({"user_id": 1}),))
        res = client.post("/api/library/backfill", json={"max_requests": 1})
    assert res.status_code == 409 and world.lookups == []
