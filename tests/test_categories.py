"""Reference categories: per-library mappings, type filtering, DB upgrade, old runs."""

import json
import sqlite3
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors.arxiv import classify_entry
from bayan.connectors.arxiv import parse_entry as arxiv_parse
from bayan.connectors.crossref import classify_item
from bayan.connectors.europepmc import classify_result
from bayan.connectors.openalex import classify_work
from bayan.db import connect, init_db
from bayan.models import DOCUMENT_TYPES, Paper, SearchProfile
from bayan.search import SearchRequest, run_search

CATEGORIES = set(DOCUMENT_TYPES)


def work(type_, source_type=None):
    w = {"type": type_}
    if source_type is not None:
        w["primary_location"] = {"source": {"type": source_type}}
    return w


# ---- one mapping function per library ----

@pytest.mark.parametrize("record, expected", [
    (work("article", "journal"), "peer_reviewed"),
    (work("review", "journal"), "peer_reviewed"),
    (work("article", "conference"), "proceedings"),
    (work("review", "conference"), "proceedings"),
    (work("article", "repository"), "other"),
    (work("article", "book series"), "other"),
    (work("article"), "other"),
    (work("dissertation", "repository"), "thesis"),
    (work("report"), "report"),
    (work("preprint", "repository"), "other"),
    (work("preprint", "journal"), "other"),
    (work("book"), "other"),
    (work("book-chapter"), "other"),
    (work("dataset"), "other"),
    (work("standard"), "other"),
    (work("editorial", "journal"), "other"),
    (work(None), "other"),
    ({}, "other"),
])
def test_openalex_mapping(record, expected):
    category, basis = classify_work(record)
    assert category == expected and basis


def test_openalex_undecided_records_say_why():
    assert "no source type" in classify_work(work("article"))[1]
    assert "repository" in classify_work(work("article", "repository"))[1]
    assert classify_work({})[1] == "no type in the record"


@pytest.mark.parametrize("pub_types, source, expected", [
    (["Journal Article"], "MED", "peer_reviewed"),
    (["Journal Article", "Review"], "MED", "peer_reviewed"),
    (["Review"], "PMC", "peer_reviewed"),
    (["research-article"], "PMC", "peer_reviewed"),
    (["Journal Article"], "PPR", "other"),
    ([], "PPR", "other"),
    (["Preprint"], "MED", "other"),
    (["Letter"], "MED", "other"),
    (["Peer Review", "Journal Article"], "PMC", "other"),
    ([], "MED", "other"),
])
def test_europepmc_mapping(pub_types, source, expected):
    record = {"source": source, "pubTypeList": {"pubType": pub_types}}
    category, basis = classify_result(record)
    assert category == expected and basis


def test_europepmc_missing_publication_type_is_other_with_basis():
    assert classify_result({"source": "MED"}) == ("other", "no publication type in the record")


@pytest.mark.parametrize("type_, expected", [
    ("journal-article", "peer_reviewed"),
    ("proceedings-article", "proceedings"),
    ("dissertation", "thesis"),
    ("report", "report"),
    ("posted-content", "other"),
    ("book", "other"),
    ("book-chapter", "other"),
    ("dataset", "other"),
    ("standard", "other"),
    ("peer-review", "other"),
    ("", "other"),
])
def test_crossref_mapping(type_, expected):
    category, basis = classify_item({"type": type_})
    assert category == expected and basis
    assert classify_item({})[0] == "other"


def test_arxiv_is_always_other_even_with_a_journal_ref():
    ns = "http://www.w3.org/2005/Atom"
    entry = ET.fromstring(
        f'<entry xmlns="{ns}" xmlns:arxiv="http://arxiv.org/schemas/atom">'
        "<id>http://arxiv.org/abs/2101.00001v1</id><published>2021-01-01T00:00:00Z</published>"
        "<title>T</title><arxiv:journal_ref>J. Phys. 1 (2021)</arxiv:journal_ref></entry>")
    assert classify_entry(entry) == ("other", "arXiv preprint")
    p = arxiv_parse(entry)
    assert (p.category, p.native_type) == ("other", "preprint")


def test_every_mapping_returns_only_the_five_ids():
    results = [classify_work(work(t, s)) for t in ("article", "report", "x", None)
               for s in ("journal", "conference", None)]
    results += [classify_item({"type": t}) for t in ("journal-article", "x")]
    results += [classify_result({"source": s}) for s in ("MED", "PPR")]
    assert {c for c, _ in results} <= CATEGORIES


# ---- model ----

def test_profile_defaults_to_peer_reviewed_and_reads_old_keys():
    assert SearchProfile().document_types == ["peer_reviewed"]
    old = SearchProfile(document_types=["article", "review", "report", "manual"])
    assert old.document_types == ["peer_reviewed", "report", "other"]
    with pytest.raises(ValueError):
        SearchProfile(document_types=["preprint"])


# ---- pre-filter + classify path ----

class Fake:
    name = "openalex"
    tier = "open"
    supported_filters = frozenset({"years", "document_types"})

    def __init__(self, papers):
        self.papers = papers

    def search(self, profile):
        yield from self.papers


def paper(i, category, native="x"):
    return Paper(source="openalex", id=f"W{i}", doi=f"10.1/{i}", title=f"P{i}", year=2020,
                 native_type=native, category=category, category_basis="test")


def run(tmp_path, papers, types, **kw):
    conn = connect(tmp_path / "r.sqlite")
    init_db(tmp_path / "r.sqlite")
    conn.execute("INSERT INTO users(email, password_hash) VALUES('a@b.c', 'x')")
    req = SearchRequest(profile=SearchProfile(document_types=types, year_from=2020, year_to=2020),
                        **kw)
    return conn, run_search(conn, 1, req, [Fake(papers)], 50)


def test_filtered_by_type_and_by_category_with_zero_counts(tmp_path):
    papers = [paper(1, "peer_reviewed"), paper(2, "other"), paper(3, "thesis"),
              paper(4, "other"), paper(5, "report")]
    conn, result = run(tmp_path, papers, ["peer_reviewed", "proceedings"])
    s = result["summary"]
    assert s["found"] == 1 and s["scanned"] == 5 and s["filtered_by_type"] == 4
    assert s["by_category"] == {"peer_reviewed": 1, "proceedings": 0}
    rows = conn.execute("SELECT category FROM papers").fetchall()
    assert [r["category"] for r in rows] == ["peer_reviewed"]


def test_other_means_not_one_of_the_four_never_everything(tmp_path):
    papers = [paper(1, "peer_reviewed"), paper(2, "other"), paper(3, "thesis")]
    _, result = run(tmp_path, papers, ["other"])
    s = result["summary"]
    assert s["found"] == 1 and s["filtered_by_type"] == 2 and s["by_category"] == {"other": 1}


def test_no_selection_keeps_everything_and_lists_all_five(tmp_path):
    papers = [paper(1, "peer_reviewed"), paper(2, "other")]
    _, result = run(tmp_path, papers, [])
    s = result["summary"]
    assert s["found"] == 2 and s["filtered_by_type"] == 0
    assert s["by_category"] == {"peer_reviewed": 1, "proceedings": 0, "thesis": 0,
                                "report": 0, "other": 1}


def test_filtered_records_do_not_use_up_the_result_limit(tmp_path):
    papers = [paper(i, "other") for i in range(10)] + [paper(99, "peer_reviewed")]
    conn = connect(tmp_path / "r.sqlite")
    init_db(tmp_path / "r.sqlite")
    conn.execute("INSERT INTO users(email, password_hash) VALUES('a@b.c', 'x')")
    req = SearchRequest(profile=SearchProfile(year_from=2020, year_to=2020))
    s = run_search(conn, 1, req, [Fake(papers)], 1)["summary"]
    assert s["found"] == 1 and s["filtered_by_type"] == 10


def test_skipped_libraries_when_their_only_category_is_not_selected():
    import httpx

    from bayan.connectors import ArxivConnector, EuropePMCConnector

    def boom(request):
        raise AssertionError("no request expected")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    thesis = SearchProfile(document_types=["thesis", "report"])
    assert list(ArxivConnector(client=client).search(thesis)) == []
    assert list(EuropePMCConnector(client=client).search(thesis)) == []


# ---- database upgrade and old runs ----

OLD_PAPERS = """
CREATE TABLE papers(
  source TEXT NOT NULL, id TEXT NOT NULL, doi TEXT, title TEXT, abstract TEXT, year INTEGER,
  document_type TEXT, language TEXT, venue TEXT, first_author TEXT, countries TEXT,
  citations INTEGER, is_oa INTEGER, oa_url TEXT, landing_url TEXT, keywords TEXT, topic TEXT,
  PRIMARY KEY(source, id)
);
"""


def columns(path):
    with sqlite3.connect(path) as conn:
        return [r[1] for r in conn.execute("PRAGMA table_info(papers)")]


def test_init_db_upgrades_an_existing_file_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_PAPERS)
        conn.execute("INSERT INTO papers(source, id, title, document_type, countries, keywords)"
                     " VALUES('openalex', 'W1', 'Old paper', 'article', '[]', '[]')")
    assert "category" not in columns(path)
    init_db(path)
    assert {"category", "native_type", "category_basis", "document_type"} <= set(columns(path))
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT title, document_type, category FROM papers").fetchone()
    assert row == ("Old paper", "article", None)  # data kept, new columns empty
    init_db(path)  # running it again changes nothing
    assert columns(path).count("category") == 1


def test_fresh_database_has_the_new_columns(tmp_path):
    init_db(tmp_path / "new.sqlite")
    assert {"category", "native_type", "category_basis"} <= set(columns(tmp_path / "new.sqlite"))


def test_old_stored_runs_still_open(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_PAPERS)
        conn.execute("INSERT INTO papers(source, id, doi, title, year, document_type, language,"
                     " countries, keywords, oa_url) VALUES('openalex', 'W1', '10.1/a', 'Old', 2019,"
                     " 'article', 'en', '[]', '[]', '')")
    old_request = {"profile": {"query": "x", "document_types": ["article", "review", "manual"],
                               "year_from": 2015, "year_to": 2020, "languages": ["en"],
                               "countries": []}, "libraries": ["openalex"],
                   "quota_total": None, "weighting": "equal"}
    old_summary = {"found": 1, "scanned": 1, "by_document_type": {"article": 1},
                   "by_language": {"en": 1}, "doi_matches": []}
    settings = Settings(database_path=path, search_max_results=10, searches_per_hour=5)
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/register", json={"email": "ada@example.org",
                                                "password": "correct horse battery"})
        with sqlite3.connect(path) as conn:
            cur = conn.execute("INSERT INTO search_runs(user_id, request, summary)"
                               " VALUES(1, ?, ?)", (json.dumps(old_request),
                                                    json.dumps(old_summary)))
            conn.execute("INSERT INTO run_papers VALUES(?, 'openalex', 'W1', 0)", (cur.lastrowid,))
        assert client.get("/api/runs").status_code == 200
        detail = client.get(f"/api/runs/{cur.lastrowid}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["request"]["profile"]["document_types"] == ["peer_reviewed", "other"]
        assert body["summary"]["by_document_type"] == {"article": 1}
        (p,) = body["papers"]
        assert p["native_type"] == "article" and p["category"] == "other"
        assert "before reference categories" in p["category_basis"]
        assert "document_type" not in p
