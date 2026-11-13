"""OSTI, INIS, DOAJ, Semantic Scholar, CORE and Scopus connectors. No live network: every
response is a fixture shaped like the live answers observed when the connectors were written."""

import httpx
import pytest
from fastapi.testclient import TestClient
from test_collection_refresh import db, env, history, make_collection, sign_in  # noqa: F401

from bayan import refresh as rf
from bayan.api import create_app, default_connector_factory
from bayan.config import Settings
from bayan.connectors import (
    ConnectorError,
    CoreConnector,
    DoajConnector,
    InisConnector,
    OstiConnector,
    ScopusConnector,
    SemanticScholarConnector,
    build_connector,
    core,
    doaj,
    inis,
    osti,
    scopus,
    semanticscholar,
)
from bayan.doctypes import CAPABILITIES
from bayan.libraries import LIBRARIES
from bayan.models import SearchProfile
from bayan.search import CITATION_LIBRARIES

# Made-up secrets for the tests; they are not credentials for anything.
FAKE_KEY = "fake-scopus-key-0000aaaa1111bbbb"
FAKE_TOKEN = "fake-insttoken-2222cccc3333dddd"
SECRET = "test-secret-0123456789-abcdefghij"
PASSWORD = "correct horse battery"


def recorder(responses, seen):
    queue = list(responses)

    def handler(request):
        seen.append(request)
        status, body, headers = queue.pop(0)
        return httpx.Response(status, json=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def no_sleep(_):
    return None


PROFILE = SearchProfile(query="CANDU", year_from=2010, year_to=2020, document_types=[])

# ---------------------------------------------------------------- OSTI

OSTI_RECORD = {
    "osti_id": "3484793", "title": "Initialization Improvements in MCNP6.4",
    "authors": ["Swaminarayan, Sriram [Los Alamos National Laboratory] (ORCID:0000000318095231)",
                "Doe, Jane [Idaho National Laboratory (INL)]"],
    "doi": "10.2172/3484793", "publication_date": "2026-08-25T00:00:00Z",
    "product_type": "Conference", "language": "English", "description": "An abstract.",
    "subjects": ["97 MATHEMATICS AND COMPUTING"], "report_number": "LA-UR-26-27281",
    "links": [{"rel": "citation", "href": "https://www.osti.gov/biblio/3484793"},
              {"rel": "fulltext", "href": "https://www.osti.gov/servlets/purl/3484793"}],
}


def test_osti_parse_cleans_authors_and_maps_fields():
    p = osti.parse_record(OSTI_RECORD)
    assert (p.source, p.id, p.doi, p.year) == ("osti", "3484793", "10.2172/3484793", 2026)
    assert p.authors == ["Swaminarayan, Sriram", "Doe, Jane"]
    assert p.first_author == "Swaminarayan, Sriram"
    assert (p.category, p.native_type, p.language) == ("proceedings", "Conference", "en")
    assert p.landing_url == "https://www.osti.gov/biblio/3484793"
    assert p.is_oa and p.oa_url == "https://www.osti.gov/servlets/purl/3484793"
    assert p.keywords == ["97 MATHEMATICS AND COMPUTING"]


@pytest.mark.parametrize("native,category", [
    ("Journal Article", "peer_reviewed"), ("Conference", "proceedings"),
    ("Technical Report", "report"), ("Thesis/Dissertation", "thesis"), ("Book", "other"),
    ("", "other")])
def test_osti_classification(native, category):
    cat, basis = osti.classify_record({"product_type": native})
    assert cat == category and basis


def test_osti_without_fulltext_is_not_open_access():
    p = osti.parse_record({**OSTI_RECORD, "links": OSTI_RECORD["links"][:1]})
    assert not p.is_oa and p.oa_url == ""


def test_osti_sends_dates_and_pages_by_total_header():
    seen = []
    client = recorder([(200, [OSTI_RECORD, {**OSTI_RECORD, "osti_id": "2"}],
                        {"x-total-count": "3"}),
                       (200, [{**OSTI_RECORD, "osti_id": "3"}], {"x-total-count": "3"})], seen)
    papers = list(OstiConnector(client=client, rows=2).search(PROFILE))
    assert [p.id for p in papers] == ["3484793", "2", "3"]
    params = seen[0].url.params
    assert params["q"] == "CANDU" and params["page"] == "1" and params["rows"] == "2"
    assert params["publication_date_start"] == "01/01/2010"
    assert params["publication_date_end"] == "12/31/2020"
    assert seen[1].url.params["page"] == "2"
    assert seen[0].headers["accept"] == "application/json"


# ---------------------------------------------------------------- INIS

INIS_HIT = {
    "id": "fy045-gn294",
    "links": {"self_html": "https://inis.iaea.org/records/fy045-gn294"},
    "access": {"record": "public", "files": "public"},
    "files": {"enabled": True, "entries": {"32053242.pdf": {
        "ext": "pdf", "mimetype": "application/pdf", "key": "32053242.pdf"}}},
    "metadata": {
        "resource_type": {"id": "publication-report", "title": {"en": "Report"}},
        "creators": [
            {"person_or_org": {"type": "personal", "name": "Candu, Constantin"}},
            {"person_or_org": {"type": "organizational", "name": "DESY, Hamburg (Germany)"}}],
        "title": "Continuum limit of spin chains", "publisher": "",
        "publication_date": "2011-03", "languages": [{"id": "en"}],
        "description": "We study <b>n&gt;0</b> chains.",
        "identifiers": [{"identifier": "10.1142/S0217751X23501452", "scheme": "doi"}]},
    "custom_fields": {"journal:journal": {"title": "Nucl. Phys. B"}},
}


def test_inis_parse_keeps_personal_creators_and_public_files_only():
    p = inis.parse_record(INIS_HIT)
    assert (p.source, p.id, p.year, p.language) == ("inis", "fy045-gn294", 2011, "en")
    assert p.authors == ["Candu, Constantin"]
    assert p.doi == "10.1142/s0217751x23501452"
    assert p.abstract == "We study n>0 chains."
    assert (p.category, p.venue) == ("report", "Nucl. Phys. B")
    assert p.landing_url == "https://inis.iaea.org/records/fy045-gn294"
    assert p.is_oa and p.oa_url == (
        "https://inis.iaea.org/records/fy045-gn294/files/32053242.pdf?download=1")
    restricted = inis.parse_record({**INIS_HIT, "access": {"files": "restricted"},
                                    "files": {"enabled": True}})
    assert not restricted.is_oa and restricted.oa_url == ""


@pytest.mark.parametrize("rtype,category", [
    ("publication-article", "peer_reviewed"), ("publication-conferencepaper", "proceedings"),
    ("publication-report", "report"), ("publication-book", "other"), ("software", "other")])
def test_inis_classification(rtype, category):
    assert inis.classify_record({"metadata": {"resource_type": {"id": rtype}}})[0] == category


def test_inis_query_has_dates_and_languages():
    q = inis.build_query(SearchProfile(query="CANDU", year_from=1990, year_to=1995,
                                       languages=["en", "fr"]))
    assert q == ("(CANDU) AND metadata.publication_date:[1990-01-01 TO 1995-12-31]"
                 " AND metadata.languages.id:(en OR fr)")


def test_inis_pages_and_stops_at_the_record_cap(monkeypatch):
    monkeypatch.setattr(inis, "MAX_RECORDS", 4)
    seen = []
    page = {"hits": {"hits": [INIS_HIT, {**INIS_HIT, "id": "b"}], "total": 50}}
    conn = InisConnector(client=recorder([(200, page, {}), (200, page, {})], seen), size=2)
    assert len(list(conn.search(PROFILE))) == 4
    assert [r.url.params["page"] for r in seen] == ["1", "2"]
    assert seen[0].url.params["sort"] == "bestmatch" and seen[0].url.params["size"] == "2"
    assert conn.truncated and "10000" not in conn.warnings[0] and "4" in conn.warnings[0]


def test_inis_is_not_asked_for_theses_only():
    seen = []
    conn = InisConnector(client=recorder([], seen))
    assert list(conn.search(SearchProfile(query="x", document_types=["thesis"]))) == []
    assert seen == []


# ---------------------------------------------------------------- DOAJ

DOAJ_RECORD = {
    "id": "000e2b39f83048ac8fc9df6c45f78401",
    "bibjson": {
        "title": "Sophorolipids Production", "abstract": "Biosurfactants.", "year": "2021",
        "author": [{"name": "Alejandra Rodríguez"}, {"name": "Teresa Gea"}],
        "identifier": [{"id": "2673-2718", "type": "eissn"},
                       {"id": "10.3389/FCENG.2021.632752", "type": "doi"}],
        "journal": {"title": "Frontiers in Chemical Engineering", "language": ["EN"]},
        "keywords": ["biosurfactant"],
        "link": [{"type": "fulltext", "url": "https://www.frontiersin.org/a/full"}]},
}


def test_doaj_parse_is_open_access_and_peer_reviewed():
    p = doaj.parse_record(DOAJ_RECORD)
    assert (p.source, p.year, p.doi) == ("doaj", 2021, "10.3389/fceng.2021.632752")
    assert p.authors == ["Alejandra Rodríguez", "Teresa Gea"]
    assert p.category == "peer_reviewed" and "DOAJ" in p.category_basis
    assert p.is_oa and p.oa_url == "https://www.frontiersin.org/a/full"
    assert p.landing_url == "https://doi.org/10.3389/fceng.2021.632752"
    assert p.venue == "Frontiers in Chemical Engineering" and p.language == ""


def test_doaj_query_goes_in_the_path_and_stops_at_1000(monkeypatch):
    monkeypatch.setattr(doaj, "PAGE_SIZE", 1)
    monkeypatch.setattr(doaj, "MAX_RECORDS", 2)
    seen = []
    page = {"total": 9, "results": [DOAJ_RECORD]}
    conn = DoajConnector(client=recorder([(200, page, {}), (200, page, {})], seen))
    assert len(list(conn.search(PROFILE))) == 2
    assert seen[0].url.raw_path.decode().startswith(
        "/api/search/articles/%28CANDU%29%20AND%20bibjson.year%3A%5B2010%20TO%202020%5D?")
    assert [r.url.params["page"] for r in seen] == ["1", "2"]
    assert conn.truncated


def test_doaj_skips_when_peer_reviewed_is_not_selected():
    seen = []
    conn = DoajConnector(client=recorder([], seen))
    assert list(conn.search(SearchProfile(query="x", document_types=["thesis"]))) == []
    assert seen == []


# ---------------------------------------------------------------- Semantic Scholar

S2_PAPER = {
    "paperId": "abc123", "url": "https://www.semanticscholar.org/paper/abc123",
    "title": "CANDU lattice", "abstract": "Text.", "year": 2018, "venue": "Ann. Nucl. Energy",
    "authors": [{"authorId": "1", "name": "Ada Lovelace"}], "citationCount": 7,
    "externalIds": {"DOI": "10.1016/J.ANUCENE.2018.01"}, "isOpenAccess": True,
    "openAccessPdf": {"url": "https://example.org/p.pdf", "status": "GREEN"},
    "publicationTypes": ["JournalArticle"],
}


def test_semanticscholar_parse():
    p = semanticscholar.parse_record(S2_PAPER)
    assert (p.source, p.id, p.doi, p.year, p.citations) == (
        "semanticscholar", "abc123", "10.1016/j.anucene.2018.01", 2018, 7)
    assert p.category == "peer_reviewed" and p.is_oa and p.oa_url == "https://example.org/p.pdf"
    assert p.landing_url == S2_PAPER["url"] and p.first_author == "Ada Lovelace"
    none = semanticscholar.parse_record({"paperId": "x", "openAccessPdf": None})
    assert none.oa_url == "" and none.category == "other" and none.year is None


@pytest.mark.parametrize("types,category", [
    (["JournalArticle"], "peer_reviewed"), (["Review"], "peer_reviewed"),
    (["Conference"], "proceedings"), (["JournalArticle", "Conference"], "proceedings"),
    (["Book"], "other"), (None, "other")])
def test_semanticscholar_classification(types, category):
    assert semanticscholar.classify_record({"publicationTypes": types})[0] == category


def test_semanticscholar_sends_years_key_header_and_pages_with_next():
    seen = []
    client = recorder([(200, {"total": 3, "offset": 0, "next": 2, "data": [S2_PAPER, S2_PAPER]},
                        {}),
                       (200, {"total": 3, "offset": 2, "data": [S2_PAPER]}, {})], seen)
    conn = SemanticScholarConnector(api_key=FAKE_KEY, client=client, page_size=2)
    assert len(list(conn.search(PROFILE))) == 3
    params = seen[0].url.params
    assert (params["query"], params["year"], params["limit"], params["offset"]) == (
        "CANDU", "2010-2020", "2", "0")
    assert "citationCount" in params["fields"] and "openAccessPdf" in params["fields"]
    assert seen[0].headers["x-api-key"] == FAKE_KEY
    assert seen[1].url.params["offset"] == "2"


def test_semanticscholar_stops_at_1000(monkeypatch):
    monkeypatch.setattr(semanticscholar, "MAX_RECORDS", 2)
    seen = []
    client = recorder([(200, {"next": 2, "data": [S2_PAPER, S2_PAPER]}, {})], seen)
    conn = SemanticScholarConnector(client=client, page_size=100)
    assert len(list(conn.search(PROFILE))) == 2
    assert seen[0].url.params["limit"] == "2" and conn.truncated
    assert "x-api-key" not in seen[0].headers


def test_semanticscholar_rate_limit_without_key_names_the_setting():
    seen = []
    conn = SemanticScholarConnector(client=recorder([(429, {}, {})] * 2, seen),
                                    sleep=no_sleep, max_attempts=2)
    with pytest.raises(ConnectorError, match="SEMANTIC_SCHOLAR_API_KEY"):
        list(conn.search(PROFILE))


def test_semanticscholar_needs_query_text():
    seen = []
    conn = SemanticScholarConnector(client=recorder([], seen))
    assert list(conn.search(SearchProfile(query=""))) == [] and seen == []
    assert "query text" in conn.warnings[0]


# ---------------------------------------------------------------- CORE

CORE_WORK = {
    "id": 68820432, "title": "Analisis konsep CANDU", "abstract": "Abstrak.",
    "yearPublished": 2005, "authors": [{"name": "Alfa, Sudjatmi K"}],
    "doi": "10.17146/JSTNI.2001.2.1.1671", "documentType": "journal article",
    "downloadUrl": "", "sourceFulltextUrls": ["https://core.ac.uk/download/231140249.pdf"],
    "language": {"code": "en", "name": "English"}, "publisher": "HIMNI",
    "links": [{"type": "display", "url": "https://core.ac.uk/works/68820432"}],
}


def test_core_parse():
    p = core.parse_record(CORE_WORK)
    assert (p.source, p.id, p.year, p.doi) == (
        "core", "68820432", 2005, "10.17146/jstni.2001.2.1.1671")
    assert p.category == "peer_reviewed" and p.language == "en"
    assert p.is_oa and p.oa_url == "https://core.ac.uk/download/231140249.pdf"
    assert p.landing_url == "https://core.ac.uk/works/68820432"
    bare = core.parse_record({"id": 5, "documentType": None, "language": None,
                              "sourceFulltextUrls": []})
    assert not bare.is_oa and bare.category == "other" and bare.language == ""


@pytest.mark.parametrize("dtype,category", [
    ("journal article", "peer_reviewed"), ("conference paper", "proceedings"),
    ("thesis", "thesis"), ("report", "report"), ("research article", "other"),
    ("image", "other")])
def test_core_classification(dtype, category):
    assert core.classify_record({"documentType": dtype})[0] == category


def test_core_query_offsets_and_bearer_key():
    seen = []
    client = recorder([(200, {"totalHits": 3, "results": [CORE_WORK, CORE_WORK]}, {}),
                       (200, {"totalHits": 3, "results": [CORE_WORK]}, {})], seen)
    conn = CoreConnector(api_key=FAKE_KEY, client=client, page_size=2)
    assert len(list(conn.search(PROFILE))) == 3
    assert seen[0].url.path == "/v3/search/works/"
    assert seen[0].url.params["q"] == (
        "(CANDU) AND yearPublished>=2010 AND yearPublished<=2020")
    assert [r.url.params["offset"] for r in seen] == ["0", "2"]
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_core_without_key_sends_no_authorization_and_explains_limits():
    seen = []
    conn = CoreConnector(client=recorder([(429, {}, {})] * 2, seen), sleep=no_sleep,
                         max_attempts=2)
    with pytest.raises(ConnectorError, match="CORE_API_KEY"):
        list(conn.search(PROFILE))
    assert "authorization" not in seen[0].headers


# ---------------------------------------------------------------- Scopus

SCOPUS_ENTRY = {
    "dc:identifier": "SCOPUS_ID:85012345678", "eid": "2-s2.0-85012345678",
    "dc:title": "CANDU fuel", "dc:creator": "Lovelace A.",
    "prism:publicationName": "Nuclear Engineering and Design",
    "prism:coverDate": "2017-05-01", "prism:doi": "10.1016/J.NUCENGDES.2017.01",
    "citedby-count": "12", "subtype": "ar", "subtypeDescription": "Article",
    "openaccessFlag": False,
    "link": [{"@ref": "self", "@href": "https://api.elsevier.com/x"},
             {"@ref": "scopus", "@href": "https://www.scopus.com/inward/record.uri?eid=2"}],
}


def scopus_page(entries, total):
    return {"search-results": {"opensearch:totalResults": str(total), "entry": entries}}


def test_scopus_parse():
    p = scopus.parse_entry(SCOPUS_ENTRY)
    assert (p.source, p.id, p.year, p.citations) == ("scopus", "85012345678", 2017, 12)
    assert p.doi == "10.1016/j.nucengdes.2017.01" and p.category == "peer_reviewed"
    assert p.authors == ["Lovelace A."] and p.venue == "Nuclear Engineering and Design"
    assert p.landing_url == "https://www.scopus.com/inward/record.uri?eid=2" and not p.is_oa
    assert scopus.parse_entry({**SCOPUS_ENTRY, "openaccessFlag": "true"}).is_oa


@pytest.mark.parametrize("code,category", [
    ("ar", "peer_reviewed"), ("re", "peer_reviewed"), ("cp", "proceedings"), ("ch", "other"),
    ("", "other")])
def test_scopus_classification(code, category):
    assert scopus.classify_entry({"subtype": code})[0] == category


def test_scopus_query_headers_and_paging():
    seen = []
    client = recorder([(200, scopus_page([SCOPUS_ENTRY] * 25, 26), {}),
                       (200, scopus_page([SCOPUS_ENTRY], 26), {})], seen)
    conn = ScopusConnector(FAKE_KEY, FAKE_TOKEN, client=client)
    assert len(list(conn.search(PROFILE))) == 26
    params = seen[0].url.params
    assert params["query"] == "TITLE-ABS-KEY(CANDU) AND PUBYEAR > 2009 AND PUBYEAR < 2021"
    assert (params["start"], params["count"], params["view"]) == ("0", "25", "STANDARD")
    assert seen[1].url.params["start"] == "25"
    headers = seen[0].headers
    assert headers["x-els-apikey"] == FAKE_KEY and headers["x-els-insttoken"] == FAKE_TOKEN
    assert headers["accept"] == "application/json"
    assert FAKE_KEY not in str(seen[0].url) and FAKE_KEY not in repr(conn)


def test_scopus_empty_result_entry_is_not_a_paper():
    seen = []
    empty = scopus_page([{"@_fa": "true", "error": "Result set was empty"}], 0)
    conn = ScopusConnector(FAKE_KEY, client=recorder([(200, empty, {})], seen))
    assert list(conn.search(PROFILE)) == []
    assert "x-els-insttoken" not in seen[0].headers


@pytest.mark.parametrize("status", [401, 403])
def test_scopus_refusal_is_explained_without_the_key(status):
    seen = []
    conn = ScopusConnector(FAKE_KEY, client=recorder([(status, {}, {})], seen))
    with pytest.raises(ConnectorError) as exc:
        list(conn.search(PROFILE))
    assert str(exc.value) == scopus.REFUSED and FAKE_KEY not in str(exc.value)


# ---------------------------------------------------------------- registry and wiring

NEW = ("osti", "inis", "doaj", "semanticscholar", "core", "scopus")


def test_registry_capabilities_and_citation_sources():
    libs = {lib["id"]: lib for lib in LIBRARIES}
    for lib_id in NEW:
        assert libs[lib_id]["status"] == "available" and lib_id in CAPABILITIES
        assert not libs[lib_id].get("preselected")  # chosen by hand
    assert libs["scopus"]["tier"] == "membership"
    assert libs["nrc_adams"]["status"] == "unconfirmed"
    assert libs["elsevier"]["status"] == "planned"
    assert {"semanticscholar", "scopus"} <= CITATION_LIBRARIES
    assert not {"osti", "inis", "doaj", "core"} & CITATION_LIBRARIES


def test_default_factory_builds_the_open_connectors(tmp_path):
    settings = Settings(database_path=tmp_path / "x.sqlite", semantic_scholar_api_key=FAKE_KEY,
                        core_api_key=FAKE_TOKEN)
    make = default_connector_factory(settings)
    assert [make(i).name for i in NEW[:5]] == list(NEW[:5])
    assert make("semanticscholar")._api_key == FAKE_KEY and make("core")._api_key == FAKE_TOKEN
    assert FAKE_KEY not in repr(settings) and FAKE_TOKEN not in repr(settings)


def test_build_connector_passes_the_user_only_to_factories_that_take_one():
    calls = []
    assert build_connector(lambda lib: calls.append(lib) or lib, "osti", 7) == "osti"

    def with_user(lib, user_id=None):
        calls.append((lib, user_id))
        return lib

    build_connector(with_user, "scopus", 7)
    assert calls == ["osti", ("scopus", 7)]


def scopus_app(tmp_path, seen):
    settings = Settings(database_path=tmp_path / "t.sqlite", app_secret_key=SECRET,
                        search_max_results=50)
    real = default_connector_factory(settings)

    def factory(lib, user_id=None):
        conn = real(lib, user_id=user_id)
        if lib == "scopus":
            conn._client = recorder([(200, scopus_page([SCOPUS_ENTRY], 1), {})], seen)
        return conn

    return TestClient(create_app(settings, connector_factory=factory))


def test_scopus_search_needs_the_users_saved_key(tmp_path):
    seen = []
    with scopus_app(tmp_path, seen) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        body = {"libraries": ["scopus"], "profile": {"query": "CANDU", "year_from": 2010,
                                                     "document_types": ["peer_reviewed"]}}
        res = client.post("/api/search", json=body)
        assert res.status_code == 502
        assert res.json()["detail"] == "Add your Scopus API key under Membership access first"
        assert client.put("/api/membership/scopus/key", json={"key": FAKE_KEY}).status_code == 200
        r = client.put("/api/membership/scopus_insttoken/key", json={"key": FAKE_TOKEN})
        assert r.status_code == 200 and FAKE_TOKEN not in r.text
        token = next(p for p in r.json()["publishers"] if p["id"] == "scopus_insttoken")
        assert token["saved"] and token["parent"] == "scopus"
        res = client.post("/api/search", json=body)
        assert res.status_code == 200, res.text
        assert res.json()["summary"]["by_library"] == {"scopus": 1}
        assert seen[0].headers["x-els-apikey"] == FAKE_KEY
        assert seen[0].headers["x-els-insttoken"] == FAKE_TOKEN
        assert FAKE_KEY not in res.text


def test_another_users_scopus_key_is_not_used(tmp_path):
    seen = []
    with scopus_app(tmp_path, seen) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        client.put("/api/membership/scopus/key", json={"key": FAKE_KEY})
        client.post("/api/auth/logout")
        client.post("/api/auth/register", json={"email": "b@example.org", "password": PASSWORD})
        res = client.post("/api/search", json={"libraries": ["scopus"],
                                               "profile": {"query": "CANDU"}})
        assert res.status_code == 502 and seen == []


def test_refresh_passes_the_owner_and_records_unavailable_libraries(env):  # noqa: F811
    sign_in(env, "a@example.org")
    cid = make_collection(env)
    with db(env) as conn:
        job = rf.queue_refresh(conn, cid, 1, 5, False)
        conn.commit()
    users = []

    def factory(lib, user_id=None):
        users.append(user_id)
        raise ConnectorError("Add your Scopus API key under Membership access first")

    rf.run_refresh_job(env.db, job, factory, 50, 30)
    done = history(env, cid)[0]
    assert users == [1]
    assert done["skipped"] == {"libraries_unavailable": {
        "openalex": "Add your Scopus API key under Membership access first"}}
    assert done["shortfalls"][0]["reason"] == "no_library_with_date_filter"
