import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app, default_connector_factory
from bayan.config import Settings
from bayan.connectors import CrossrefConnector
from bayan.db import SCHEMA
from bayan.doi_lookup import DoiLookup
from bayan.models import Paper
from bayan.retrieval import collect

TITLE = "Neutron transport in molten salt lattices"


def cr_item(title=TITLE, year=2021, family="Lovelace", doi="10.1234/Found.1"):
    return {"DOI": doi, "title": [title], "issued": {"date-parts": [[year]]},
            "author": [{"given": "Ada", "family": family}]}


def connector(items=None, status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        if status != 200:
            return httpx.Response(status, text="boom")
        return httpx.Response(200, json={"message": {"items": items or []}})

    return CrossrefConnector(contact_email="me@example.org", sleep=lambda s: None,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def paper(pid="1", title=TITLE, year=2021, author="Ada Lovelace", doi="", source="arxiv"):
    return Paper(source=source, id=pid, title=title, year=year, first_author=author, doi=doi)


# ---- acceptance rules ----

def test_exact_match_is_accepted_and_recorded():
    calls = []
    p = paper()
    lookup = DoiLookup(db(), connector([cr_item()], calls=calls), budget=5)
    lookup(p)
    assert (p.doi, p.doi_source, p.doi_match_score) == ("10.1234/found.1", "crossref_lookup", 1.0)
    assert lookup.counts["matched"] == 1 and lookup.counts["attempted"] == 1
    params = calls[0].url.params
    assert params["mailto"] == "me@example.org"
    query = params["query.bibliographic"]
    assert TITLE in query and "Ada Lovelace" in query and "2021" in query


@pytest.mark.parametrize("item", [
    cr_item(title="Neutron transport in molten salt reactors: a review of codes"),
    cr_item(title="Neutron transport in molten salt lattices: a benchmark study"),  # ~0.80
    cr_item(year=2015),
    cr_item(year=2023),
    cr_item(family="Hopper"),
    {"DOI": "10.1/x", "title": [TITLE], "issued": {"date-parts": [[2021]]}},  # no author
    {"DOI": "10.1/x", "title": [TITLE], "author": [{"family": "Lovelace"}]},  # no year
])
def test_near_misses_are_rejected(item):
    p = paper()
    lookup = DoiLookup(db(), connector([item]), budget=5)
    lookup(p)
    assert p.doi == "" and p.doi_source == "" and p.doi_match_score is None
    assert lookup.counts["no_match"] == 1


def test_year_within_one_is_accepted_and_family_name_matches_either_order():
    p = paper(year=2020, author="Lovelace A")
    DoiLookup(db(), connector([cr_item(year=2021)]), budget=5)(p)
    assert p.doi == "10.1234/found.1"


def test_paper_without_first_author_matches_on_title_and_year():
    p = paper(author="")
    DoiLookup(db(), connector([cr_item(family="Anyone")]), budget=5)(p)
    assert p.doi == "10.1234/found.1"


def test_existing_doi_is_never_overwritten_or_looked_up():
    calls = []
    p = paper(doi="10.9/original")
    lookup = DoiLookup(db(), connector([cr_item()], calls=calls), budget=5)
    lookup(p)
    assert p.doi == "10.9/original" and p.doi_source == "" and calls == []
    assert lookup.counts["attempted"] == 0


# ---- cache, budget, failures ----

def test_cache_hit_and_cached_no_match_are_not_repeated():
    conn, calls = db(), []
    hit, miss = paper("1"), paper("2", title="Something else entirely", author="Zed Zed")
    first = DoiLookup(conn, connector([cr_item()], calls=calls), budget=5)
    first(hit)
    first(miss)
    assert len(calls) == 2 and first.counts["matched"] == 1 and first.counts["no_match"] == 1

    again_hit, again_miss = paper("3"), paper("4", title="Something else entirely",
                                              author="Zed Zed")
    second = DoiLookup(conn, connector([cr_item()], calls=calls), budget=5)
    second(again_hit)
    second(again_miss)
    assert len(calls) == 2  # nothing was sent again
    assert again_hit.doi == "10.1234/found.1" and again_hit.doi_source == "crossref_lookup"
    assert again_miss.doi == ""
    assert second.counts == {"attempted": 0, "matched": 0, "no_match": 0, "failed": 0,
                             "skipped_over_budget": 0, "cache_hits": 2}


def test_budget_is_enforced_and_reported():
    calls = []
    lookup = DoiLookup(db(), connector([], calls=calls), budget=2)
    for i in range(5):
        lookup(paper(str(i), title=f"Distinct title number {i} on reactors"))
    assert len(calls) == 2
    assert lookup.counts["attempted"] == 2 and lookup.counts["skipped_over_budget"] == 3


def test_crossref_5xx_is_counted_not_raised_and_not_cached():
    conn = db()
    p = paper()
    lookup = DoiLookup(conn, connector(status=503), budget=5)
    lookup(p)
    assert p.doi == "" and lookup.counts["failed"] == 1 and lookup.counts["attempted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM doi_lookup_cache").fetchone()[0] == 0


# ---- dedupe and quotas ----

def test_cross_library_merge_after_lookup_keeps_one_richer_paper():
    native = Paper(source="openalex", id="W1", doi="10.1234/found.1", title=TITLE, year=2021,
                   first_author="Ada Lovelace", citations=7)
    looked_up = paper("2301.1", source="arxiv")
    looked_up.abstract = "A real abstract."
    looked_up.oa_url = "https://arxiv.org/pdf/2301.1"
    lookup = DoiLookup(db(), connector([cr_item()]), budget=5)
    result = collect([native, looked_up], enrich=lookup)
    assert len(result.papers) == 1 and result.duplicates == 1
    kept = result.papers[0]
    assert kept.source == "openalex" and kept.doi == "10.1234/found.1"
    assert kept.citations == 7 and kept.abstract == "A real abstract."
    assert kept.oa_url == "https://arxiv.org/pdf/2301.1"
    assert kept.doi_source == ""  # the DOI came from the source library itself


def test_duplicate_found_by_lookup_does_not_use_a_quota_slot_and_shortfall_is_honest():
    native = Paper(source="openalex", id="W1", doi="10.1234/found.1", title=TITLE, year=2021,
                   first_author="Ada Lovelace")
    dup = paper("a")  # same DOI once looked up
    lookup = DoiLookup(db(), connector([cr_item()]), budget=5)
    result = collect([native, dup], {2021: 2}, enrich=lookup)
    assert len(result.papers) == 1 and result.duplicates == 1
    assert result.shortfalls == {2021: (2, 1)}


def test_no_lookup_is_spent_on_papers_the_quota_drops():
    calls = []
    lookup = DoiLookup(db(), connector([], calls=calls), budget=10)
    papers = [paper(str(i), title=f"Distinct title number {i} on reactors") for i in range(3)]
    papers.append(paper("d", year=2020, title="A 2020 paper on reactors"))
    papers.append(paper("old", year=1990, title="An old paper nobody wants"))
    result = collect(papers, {2021: 1, 2020: 1}, enrich=lookup)
    assert len(result.papers) == 2 and result.skipped_over_quota == 2
    assert len(calls) == 2  # only the two papers that were kept


# ---- through the API ----

def api_settings(tmp_path, budget=50):
    return Settings(database_path=tmp_path / "t.sqlite", search_max_results=50,
                    searches_per_hour=5, contact_email="me@example.org",
                    doi_lookup_budget=budget)


def sign_in(client):
    client.post("/api/auth/register", json={"email": "ada@example.org",
                                            "password": "correct horse battery"})


def test_search_rejects_crossref_as_a_source(tmp_path):
    with TestClient(create_app(api_settings(tmp_path))) as client:
        sign_in(client)
        res = client.post("/api/search", json={"libraries": ["openalex", "crossref"],
                                               "profile": {}})
        assert res.status_code == 422
        assert res.json()["detail"] == (
            "Crossref is used automatically to find missing DOIs, not as a search source")
        libs = {lib["id"]: lib for lib in client.get("/api/libraries").json()}
        assert libs["crossref"]["role"] == "lookup" and libs["openalex"]["role"] == "search"


def test_search_fills_missing_dois_stores_duplicates_once_and_reports_counts(tmp_path):
    settings = api_settings(tmp_path)
    real = default_connector_factory(settings)
    work = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/Found.1",
            "title": TITLE, "publication_year": 2021, "type": "article", "language": "en",
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}]}
    no_doi = {"id": "https://openalex.org/W2", "doi": None, "title": "Unrelated spectral work",
              "publication_year": 2020, "type": "article", "language": "en"}

    def openalex(request):
        return httpx.Response(200, json={"results": [work, no_doi],
                                         "meta": {"next_cursor": None}})

    def arxiv(request):
        entry = ("<entry><id>http://arxiv.org/abs/2301.00001v1</id>"
                 "<published>2021-01-02T00:00:00Z</published>"
                 f"<title>{TITLE}</title><summary>s</summary>"
                 "<author><name>Ada Lovelace</name></author></entry>")
        return httpx.Response(200, text=(
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
            'xmlns:arxiv="http://arxiv.org/schemas/atom">'
            f"<opensearch:totalResults>1</opensearch:totalResults>{entry}</feed>"))

    def crossref(request):
        # Answers the arXiv paper only; the other query finds nothing close.
        items = [cr_item()] if TITLE in request.url.params["query.bibliographic"] else []
        return httpx.Response(200, json={"message": {"items": items}})

    handlers = {"openalex": openalex, "arxiv": arxiv, "crossref": crossref}

    def factory(lib):
        conn = real(lib)
        conn._client = httpx.Client(transport=httpx.MockTransport(handlers[lib]))
        return conn

    with TestClient(create_app(settings, connector_factory=factory)) as client:
        sign_in(client)
        res = client.post("/api/search", json={"libraries": ["openalex", "arxiv"],
                                               "profile": {"year_from": 2015,
                                                           "document_types": ["other"]}})
        assert res.status_code == 200, res.text
        run = res.json()
        s = run["summary"]
        assert s["found"] == 2 and s["duplicates"] == 1
        assert s["by_library"] == {"openalex": 2, "arxiv": 0} and "crossref" not in s["by_library"]
        # arXiv paper: matched (and merged into the OpenAlex work); W2: no confident match
        assert s["doi_lookup"] == {"attempted": 2, "matched": 1, "no_match": 1, "failed": 0,
                                   "skipped_over_budget": 0, "cache_hits": 0}
        detail = client.get(f"/api/runs/{run['id']}").json()
        assert sorted(p["doi"] for p in detail["papers"]) == ["", "10.1234/found.1"]


def test_search_survives_crossref_outage(tmp_path):
    settings = api_settings(tmp_path)
    real = default_connector_factory(settings)

    def arxiv(request):
        return httpx.Response(200, text=(
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            "<opensearch:totalResults>1</opensearch:totalResults><entry>"
            "<id>http://arxiv.org/abs/2301.00001v1</id><published>2021-01-02T00:00:00Z</published>"
            f"<title>{TITLE}</title><summary>s</summary><author><name>Ada Lovelace</name></author>"
            "</entry></feed>"))

    handlers = {"arxiv": arxiv, "crossref": lambda request: httpx.Response(503)}

    def factory(lib):
        conn = real(lib)
        conn._client = httpx.Client(transport=httpx.MockTransport(handlers[lib]))
        conn._sleep = lambda s: None
        return conn

    with TestClient(create_app(settings, connector_factory=factory)) as client:
        sign_in(client)
        res = client.post("/api/search", json={"libraries": ["arxiv"],
                                               "profile": {"year_from": 2015,
                                                           "document_types": ["other"]}})
        assert res.status_code == 200, res.text
        s = res.json()["summary"]
        assert s["found"] == 1 and s["doi_lookup"]["failed"] == 1
