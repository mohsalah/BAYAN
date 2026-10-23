import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app, default_connector_factory
from bayan.config import Settings
from bayan.connectors import (
    ArxivConnector,
    ConnectorError,
    CrossrefConnector,
    EuropePMCConnector,
    OpenAlexConnector,
)
from bayan.connectors.crossref import build_filter as crossref_filter
from bayan.connectors.crossref import parse_item
from bayan.connectors.europepmc import build_query as epmc_query
from bayan.connectors.europepmc import parse_result
from bayan.models import SearchProfile

SHARED_DOI = "10.1234/shared.2021"

CROSSREF_ITEM = {
    "DOI": "10.1234/Shared.2021",
    "title": ["Neutron transport in lattices"],
    "abstract": "<jats:p>Hello <jats:italic>world</jats:italic> &amp; more.</jats:p>",
    "issued": {"date-parts": [[2021, 5]]},
    "type": "journal-article",
    "language": "en",
    "container-title": ["Ann. Nucl. Energy"],
    "author": [{"given": "Ada", "family": "Lovelace"}],
    "is-referenced-by-count": 4,
    "URL": "https://doi.org/10.1234/shared.2021",
    "subject": ["Nuclear Energy"],
}
CROSSREF_OTHER = {"DOI": "10.5555/cr-only", "title": ["Crossref only"],
                  "issued": {"date-parts": [[2019]]}, "type": "journal-article"}

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>{total}</opensearch:totalResults>
  {entries}
</feed>"""
ARXIV_ENTRY = """<entry>
  <id>http://arxiv.org/abs/{aid}v2</id>
  <published>2021-03-04T10:00:00Z</published>
  <title>Spectral
   methods</title>
  <summary> An abstract. </summary>
  <author><name>Grace Hopper</name></author>
  {doi}
  <link title="pdf" href="http://arxiv.org/pdf/{aid}v2" rel="related" type="application/pdf"/>
  <arxiv:primary_category term="physics.comp-ph"/>
  <category term="physics.comp-ph"/><category term="math.NA"/>
</entry>"""


def arxiv_entry(aid, doi=""):
    tag = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return ARXIV_ENTRY.format(aid=aid, doi=tag)


EPMC_RECORD = {
    "id": "123", "source": "MED", "doi": "10.1234/SHARED.2021",
    "title": "Neutrons in medicine", "abstractText": "Abstract text.",
    "authorString": "Curie M, Curie P.", "pubYear": "2021", "language": "eng",
    "journalInfo": {"journal": {"title": "J Med Phys"}},
    "pubTypeList": {"pubType": ["Journal Article", "Review"]},
    "isOpenAccess": "Y", "citedByCount": 9,
    "fullTextUrlList": {"fullTextUrl": [
        {"availability": "Open access", "documentStyle": "html", "url": "https://x/h"},
        {"availability": "Open access", "documentStyle": "pdf", "url": "https://x/p.pdf"},
        {"availability": "Subscription required", "documentStyle": "pdf", "url": "https://x/s"},
    ]},
    "keywordList": {"keyword": ["neutron"]},
}


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def sequence(responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        status, body, headers = queue.pop(0)
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return client_for(handler)


# ---- Crossref ----

def test_crossref_parse_item():
    p = parse_item(CROSSREF_ITEM)
    assert (p.source, p.id, p.doi, p.year) == ("crossref", SHARED_DOI, SHARED_DOI, 2021)
    assert p.abstract == "Hello world & more."
    assert p.first_author == "Ada Lovelace" and p.venue == "Ann. Nucl. Energy"
    assert p.native_type == "journal-article" and p.citations == 4
    assert p.category == "peer_reviewed"
    assert p.countries == [] and p.oa_url == "" and not p.is_oa


def test_crossref_leaves_missing_fields_empty():
    p = parse_item({"DOI": "10.1/x"})
    assert (p.title, p.abstract, p.year, p.first_author, p.venue) == ("", "", None, "", "")


def test_crossref_filter_mapping():
    f = crossref_filter(SearchProfile(year_from=2000, year_to=2010,
                                      document_types=["peer_reviewed", "thesis"]))
    assert "from-pub-date:2000" in f and "until-pub-date:2010" in f
    assert "type:journal-article" in f and "type:dissertation" in f
    assert "proceedings-article" not in f and "type:report" not in f
    # `other` cannot be expressed as a type list, so it sends no type filter at all
    f = crossref_filter(SearchProfile(document_types=["peer_reviewed", "other"]))
    assert "type:" not in f


def test_crossref_pages_with_cursor_and_sends_polite_email():
    seen = []
    client = sequence([
        (200, {"message": {"items": [CROSSREF_ITEM], "next-cursor": "c2"}}, {}),
        (200, {"message": {"items": [CROSSREF_OTHER], "next-cursor": "c3"}}, {}),
        (200, {"message": {"items": [], "next-cursor": "c4"}}, {}),
    ], seen)
    conn = CrossrefConnector(contact_email="me@example.org", client=client)
    papers = list(conn.search(SearchProfile(query="neutron", document_types=[])))
    assert [p.doi for p in papers] == [SHARED_DOI, "10.5555/cr-only"]
    assert [r.url.params["cursor"] for r in seen] == ["*", "c2", "c3"]
    assert seen[0].url.params["mailto"] == "me@example.org"
    assert seen[0].url.params["query"] == "neutron"
    assert "me@example.org" in seen[0].headers["user-agent"]


def test_crossref_skips_records_without_doi():
    client = sequence([(200, {"message": {"items": [{"title": ["No DOI"]}], "next-cursor": "n"}},
                        {}), (200, {"message": {"items": []}}, {})])
    assert list(CrossrefConnector(client=client).search(SearchProfile(document_types=[]))) == []


# ---- retries (all three share the helper) ----

def make_all(client, sleeps):
    kw = {"client": client, "sleep": sleeps.append, "max_attempts": 3}
    return [CrossrefConnector(contact_email="secret@example.org", **kw),
            ArxivConnector(contact_email="secret@example.org", **kw),
            EuropePMCConnector(contact_email="secret@example.org", **kw)]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_retries_429_with_retry_after_then_succeeds(index):
    bodies = [
        {"message": {"items": []}},
        ARXIV_FEED.format(total=0, entries=""),
        {"resultList": {"result": []}},
    ]
    sleeps = []
    client = sequence([(429, {}, {"Retry-After": "7"}), (503, {}, {}), (200, bodies[index], {})])
    assert list(make_all(client, sleeps)[index].search(SearchProfile(document_types=[]))) == []
    assert sleeps == [7.0, 4.0]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_failure_message_has_no_keys_email_or_params(index):
    sleeps = []
    client = sequence([(500, {}, {})] * 3)
    with pytest.raises(ConnectorError) as exc:
        profile = SearchProfile(query="topsecretquery", document_types=[])
        list(make_all(client, sleeps)[index].search(profile))
    text = str(exc.value)
    assert "HTTP 500" in text
    assert "secret@example.org" not in text and "topsecretquery" not in text
    assert "http" not in text.replace("HTTP", "")


def test_non_retryable_status_fails_immediately():
    sleeps = []
    client = sequence([(400, {}, {})])
    with pytest.raises(ConnectorError, match="HTTP 400"):
        conn = CrossrefConnector(client=client, sleep=sleeps.append)
        list(conn.search(SearchProfile(document_types=[])))
    assert sleeps == []


def test_transport_error_is_reported_by_type_only():
    def boom(request):
        raise httpx.ConnectError("dns failure for secret.host")

    with pytest.raises(ConnectorError) as exc:
        list(EuropePMCConnector(client=client_for(boom), sleep=lambda s: None,
                                max_attempts=2).search(SearchProfile(document_types=[])))
    assert "ConnectError" in str(exc.value) and "secret.host" not in str(exc.value)


# ---- arXiv ----

def test_arxiv_parse_and_query():
    seen = []
    feed = ARXIV_FEED.format(total=1, entries=arxiv_entry("2103.00001", "10.1234/SHARED.2021"))
    conn = ArxivConnector(contact_email="me@example.org", client=sequence([(200, feed, {})], seen),
                          sleep=lambda s: None)
    (p,) = conn.search(SearchProfile(query="spectral methods", year_from=2020, year_to=2022,
                                      document_types=[]))
    assert (p.source, p.id, p.doi, p.year) == ("arxiv", "2103.00001", SHARED_DOI, 2021)
    assert p.title == "Spectral methods" and p.abstract == "An abstract."
    assert p.first_author == "Grace Hopper" and p.native_type == "preprint"
    assert p.category == "other" and p.category_basis
    assert p.is_oa and p.oa_url == "http://arxiv.org/pdf/2103.00001v2"
    assert p.keywords == ["physics.comp-ph", "math.NA"] and p.topic == "physics.comp-ph"
    assert p.language == "" and p.countries == []
    q = seen[0].url.params["search_query"]
    assert "all:spectral AND all:methods" in q
    assert "submittedDate:[202001010000 TO 202212312359]" in q
    assert "me@example.org" in seen[0].headers["user-agent"]


def test_arxiv_without_doi_leaves_it_empty():
    feed = ARXIV_FEED.format(total=1, entries=arxiv_entry("2103.00002"))
    conn = ArxivConnector(client=sequence([(200, feed, {})]))
    (p,) = conn.search(SearchProfile(document_types=[]))
    assert p.doi == ""


def test_arxiv_paging_waits_between_pages_and_stops_at_total():
    seen, sleeps = [], []
    page1 = ARXIV_FEED.format(total=3, entries=arxiv_entry("1") + arxiv_entry("2"))
    page2 = ARXIV_FEED.format(total=3, entries=arxiv_entry("3"))
    conn = ArxivConnector(client=sequence([(200, page1, {}), (200, page2, {})], seen),
                          sleep=sleeps.append, page_size=2)
    assert [p.id for p in conn.search(SearchProfile(document_types=[]))] == ["1", "2", "3"]
    assert [r.url.params["start"] for r in seen] == ["0", "2"]
    assert sleeps == [3.0]


def test_arxiv_error_entry_raises():
    feed = ARXIV_FEED.format(total=1, entries=(
        "<entry><id>http://arxiv.org/api/errors#bad</id><title>Error</title>"
        "<summary>bad query text</summary></entry>"))
    with pytest.raises(ConnectorError, match="rejected"):
        conn = ArxivConnector(client=sequence([(200, feed, {})]))
        list(conn.search(SearchProfile(document_types=[])))


def test_arxiv_garbage_response_raises():
    with pytest.raises(ConnectorError, match="unreadable"):
        conn = ArxivConnector(client=sequence([(200, "<html>not xml", {})]))
        list(conn.search(SearchProfile(document_types=[])))


# ---- Europe PMC ----

def test_europepmc_parse_result():
    p = parse_result(EPMC_RECORD)
    assert (p.source, p.id, p.doi, p.year) == ("europepmc", "MED:123", SHARED_DOI, 2021)
    assert p.language == "en" and p.native_type == "Journal Article; Review"
    assert p.category == "peer_reviewed" and p.venue == "J Med Phys"
    assert p.first_author == "Curie M" and p.is_oa and p.oa_url == "https://x/p.pdf"
    assert p.landing_url == "https://europepmc.org/article/MED/123" and p.keywords == ["neutron"]


def test_europepmc_missing_fields_and_preprint():
    p = parse_result({"id": "PPR1", "source": "PPR"})
    assert (p.doi, p.year, p.language, p.oa_url, p.first_author) == ("", None, "", "", "")
    assert p.native_type == "preprint" and not p.is_oa
    assert p.category == "other" and "PPR" in p.category_basis


def test_europepmc_query_maps_years_and_languages():
    q = epmc_query(SearchProfile(query="neutron", year_from=2000, year_to=2010,
                                 languages=["en", "fr"], countries=["EG"]))
    assert q == ("(neutron) AND (PUB_YEAR:[2000 TO 2010]) AND (LANG:eng OR LANG:fre)"
                 " AND (NOT SRC:PPR)")
    # no preprint pre-filter when `other` is selected or nothing is selected
    for types in (["other"], ["peer_reviewed", "other"], []):
        assert "SRC:PPR" not in epmc_query(SearchProfile(document_types=types))


def test_europepmc_cursor_paging_stops_on_repeat_or_empty():
    seen = []
    client = sequence([
        (200, {"nextCursorMark": "AoE2", "resultList": {"result": [EPMC_RECORD]}}, {}),
        (200, {"nextCursorMark": "AoE2", "resultList": {"result": [{"id": "9", "source": "MED"}]}},
         {}),
    ], seen)
    papers = list(EuropePMCConnector(contact_email="me@example.org", client=client)
                  .search(SearchProfile(document_types=[])))
    assert [p.id for p in papers] == ["MED:123", "MED:9"]
    assert [r.url.params["cursorMark"] for r in seen] == ["*", "AoE2"]
    assert seen[0].url.params["format"] == "json"


# ---- supported_filters ----

def test_supported_filters_declared_everywhere():
    assert OpenAlexConnector.supported_filters == {"years", "document_types", "languages",
                                                   "countries"}
    assert CrossrefConnector.supported_filters == {"years", "document_types"}
    assert ArxivConnector.supported_filters == {"years", "document_types"}
    assert EuropePMCConnector.supported_filters == {"years", "languages", "document_types"}


# ---- through /api/search: dedupe, per-library counts, unapplied filters ----

def openalex_client():
    journal = {"primary_location": {"source": {"type": "journal", "display_name": "J"}}}
    work = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/SHARED.2021",
            "title": "Shared work", "publication_year": 2021, "type": "article",
            "language": "en", **journal}
    own = {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.9/oa-only",
           "title": "OpenAlex only", "publication_year": 2020, "type": "article",
           "language": "en", **journal}
    preprint = {"id": "https://openalex.org/W3", "title": "A preprint",
                "publication_year": 2020, "type": "preprint", "language": "en"}
    return sequence([(200, {"results": [work, own, preprint], "meta": {"next_cursor": None}},
                      {})])


def make_factory(tmp_path):
    settings = Settings(database_path=tmp_path / "t.sqlite", search_max_results=50,
                        searches_per_hour=5, contact_email="me@example.org")
    real = default_connector_factory(settings)
    clients = {
        "openalex": openalex_client(),
        "crossref": sequence([(200, {"message": {"items": [CROSSREF_ITEM, CROSSREF_OTHER],
                                                 "next-cursor": None}}, {})]),
        "arxiv": sequence([(200, ARXIV_FEED.format(total=2, entries=(
            arxiv_entry("2103.00001", SHARED_DOI) + arxiv_entry("2103.00009"))), {})]),
        "europepmc": sequence([(200, {"resultList": {"result": [EPMC_RECORD]}}, {})]),
    }

    def factory(lib):
        conn = real(lib)
        conn._client = clients[lib]
        return conn

    return settings, factory


def test_default_factory_builds_the_new_connectors(tmp_path):
    real = default_connector_factory(Settings(database_path=tmp_path / "x.sqlite"))
    assert [real(i).name for i in ("crossref", "arxiv", "europepmc")] == [
        "crossref", "arxiv", "europepmc"]


def test_three_library_search_stores_each_work_once(tmp_path):
    settings, factory = make_factory(tmp_path)
    with TestClient(create_app(settings, connector_factory=factory)) as client:
        client.post("/api/auth/register", json={"email": "ada@example.org",
                                                "password": "correct horse battery"})
        body = {"libraries": ["openalex", "arxiv", "europepmc"],
                "profile": {"query": "neutron", "year_from": 2015, "year_to": 2022,
                            "document_types": ["peer_reviewed"], "languages": ["en"],
                            "countries": ["EG"]}}
        res = client.post("/api/search", json=body)
        assert res.status_code == 200, res.text
        run = res.json()
        s = run["summary"]
        # openalex scans 3 (one preprint dropped by the type filter), europepmc 1 (a
        # duplicate of the shared DOI); arXiv holds only preprints, so it is not even asked
        assert s["scanned"] == 4 and s["duplicates"] == 1 and s["found"] == 2
        assert s["filtered_by_type"] == 1
        assert s["scanned_by_library"] == {"openalex": 3, "arxiv": 0, "europepmc": 1}
        assert s["by_library"] == {"openalex": 2, "arxiv": 0, "europepmc": 0}
        assert s["by_category"] == {"peer_reviewed": 2}
        assert s["unapplied_filters"] == {
            "openalex": [],
            "arxiv": ["languages", "countries"],
            "europepmc": ["countries"],
        }
        detail = client.get(f"/api/runs/{run['id']}").json()
        dois = [p["doi"] for p in detail["papers"] if p["doi"]]
        assert len(dois) == len(set(dois)) and SHARED_DOI in dois
        assert {p["source"] for p in detail["papers"]} == {"openalex"}
        assert {p["category"] for p in detail["papers"]} == {"peer_reviewed"}


def test_interleaving_lets_every_library_contribute_when_limit_is_small(tmp_path):
    settings, factory = make_factory(tmp_path)
    settings = Settings(database_path=settings.database_path, search_max_results=1,
                        searches_per_hour=5)
    with TestClient(create_app(settings, connector_factory=factory)) as client:
        client.post("/api/auth/register", json={"email": "ada@example.org",
                                                "password": "correct horse battery"})
        body = {"libraries": ["openalex", "arxiv"],
                "profile": {"year_from": 2015, "document_types": ["other"]}}
        s = client.post("/api/search", json=body).json()["summary"]
        assert s["stopped"] == "scan_limit" or s["found"] >= 1
        assert s["scanned_by_library"]["openalex"] >= 1 and s["scanned_by_library"]["arxiv"] >= 1


def test_unavailable_planned_libraries_stay_refused(tmp_path):
    settings, factory = make_factory(tmp_path)
    with TestClient(create_app(settings, connector_factory=factory)) as client:
        client.post("/api/auth/register", json={"email": "ada@example.org",
                                                "password": "correct horse battery"})
        for lib in ("semanticscholar", "core", "unpaywall"):
            res = client.post("/api/search", json={"libraries": [lib], "profile": {}})
            assert res.status_code == 422
