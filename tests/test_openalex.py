import httpx
import pytest

from bayan.connectors import ConnectorError, OpenAlexConnector
from bayan.connectors.openalex import build_filter, parse_work, rebuild_abstract
from bayan.models import SearchProfile

WORK = {
    "id": "https://openalex.org/W123",
    "doi": "https://doi.org/10.1234/ABC",
    "title": "Neutron transport",
    "publication_year": 2021,
    "type": "article",
    "language": "en",
    "primary_location": {"source": {"display_name": "Ann. Nucl. Energy"}, "landing_page_url": "u"},
    "open_access": {"is_oa": True, "oa_url": "https://x/p.pdf"},
    "cited_by_count": 7,
    "authorships": [
        {"author": {"display_name": "A. Author"}, "countries": ["EG", "US"]},
        {"author": {"display_name": "B. Author"}, "countries": ["US"]},
    ],
    "abstract_inverted_index": {"hello": [0], "world": [1]},
    "keywords": [{"display_name": "transport"}],
    "primary_topic": {"display_name": "Reactor physics"},
}


def client_for(pages, seen=None):
    queue = list(pages)

    def handler(request):
        if seen is not None:
            seen.append(request)
        status, body, headers = queue.pop(0)
        return httpx.Response(status, json=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_work():
    p = parse_work(WORK)
    assert (p.id, p.doi, p.year, p.language) == ("W123", "10.1234/abc", 2021, "en")
    assert p.abstract == "hello world" and p.countries == ["EG", "US"]
    assert p.first_author == "A. Author" and p.is_oa and p.keywords == ["transport"]


def test_rebuild_abstract_handles_empty():
    assert rebuild_abstract(None) == "" and rebuild_abstract({}) == ""


def test_filter_includes_period_types_languages_countries():
    profile = SearchProfile(
        year_from=2000, year_to=2010,
        document_types=["peer_reviewed", "thesis"],
        languages=["ar", "fr"], countries=["eg", "ca"],
    )
    f = build_filter(profile)
    assert "publication_year:2000-2010" in f and "is_retracted:false" in f
    assert "type:article|review|dissertation" in f
    # `other` means "not one of the four", so it cannot be pre-filtered: no type filter
    assert "type:" not in build_filter(SearchProfile(document_types=["thesis", "other"]))
    assert "language:ar|fr" in f
    assert "authorships.institutions.country_code:EG|CA" in f


def test_profile_rejects_unsupported_language_and_bad_dates():
    with pytest.raises(ValueError):
        SearchProfile(languages=["ru"])
    with pytest.raises(ValueError):
        SearchProfile(year_from=2020, year_to=2010)
    with pytest.raises(ValueError):
        SearchProfile(countries=["USA"])


def test_search_follows_cursor_pages():
    pages = [
        (200, {"results": [WORK], "meta": {"next_cursor": "c2"}}, {}),
        (200, {"results": [{**WORK, "id": "https://openalex.org/W456"}], "meta": {}}, {}),
    ]
    seen = []
    conn = OpenAlexConnector(client=client_for(pages, seen), sleep=lambda s: None)
    ids = [p.id for p in conn.search(SearchProfile(query="reactor"))]
    assert ids == ["W123", "W456"]
    assert seen[0].url.params["search"] == "reactor" and seen[1].url.params["cursor"] == "c2"


def test_retries_then_succeeds_and_honours_retry_after():
    pages = [(429, {}, {"Retry-After": "3"}), (200, {"results": [WORK], "meta": {}}, {})]
    sleeps = []
    conn = OpenAlexConnector(client=client_for(pages), sleep=sleeps.append)
    assert len(list(conn.search(SearchProfile()))) == 1 and sleeps == [3.0]


def test_failure_message_never_contains_key():
    pages = [(403, {}, {})]
    conn = OpenAlexConnector(api_key="SECRET-KEY", client=client_for(pages), sleep=lambda s: None)
    with pytest.raises(ConnectorError) as exc:
        list(conn.search(SearchProfile()))
    assert "SECRET-KEY" not in str(exc.value) and "403" in str(exc.value)


def test_gives_up_after_max_attempts():
    pages = [(503, {}, {})] * 3
    conn = OpenAlexConnector(client=client_for(pages), sleep=lambda s: None, max_attempts=3)
    with pytest.raises(ConnectorError):
        list(conn.search(SearchProfile()))


def test_paging_past_first_page_without_key_keeps_page_one_and_warns():
    pages = [
        (200, {"results": [WORK], "meta": {"next_cursor": "c2"}}, {}),
        (401, {"error": "API key required for cursor paging"}, {}),
    ]
    conn = OpenAlexConnector(client=client_for(pages), sleep=lambda s: None)
    assert [p.id for p in conn.search(SearchProfile())] == ["W123"]
    assert len(conn.warnings) == 1 and "OPENALEX_API_KEY" in conn.warnings[0]


def test_401_with_a_key_or_on_first_page_is_still_an_error():
    first = OpenAlexConnector(client=client_for([(401, {}, {})]), sleep=lambda s: None)
    with pytest.raises(ConnectorError):
        list(first.search(SearchProfile()))
    pages = [(200, {"results": [WORK], "meta": {"next_cursor": "c2"}}, {}), (401, {}, {})]
    keyed = OpenAlexConnector(api_key="K", client=client_for(pages), sleep=lambda s: None)
    with pytest.raises(ConnectorError):
        list(keyed.search(SearchProfile()))
