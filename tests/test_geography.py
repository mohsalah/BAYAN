import csv
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors import OpenAlexConnector
from bayan.connectors.openalex import build_filter
from bayan.export import to_csv
from bayan.models import Paper, SearchProfile
from bayan.regions import (
    COUNTRY_NAMES,
    GEO_BASES,
    REGION_NAMES,
    REGIONS,
    REGIONS_VERSION,
    expand,
    regions_of,
)

PASSWORD = "correct horse battery"
SEVEN = {"north_america", "latin_america_caribbean", "europe", "africa",
         "middle_east_north_africa", "asia", "oceania"}
# Deliberately excluded from every region (see the module docstring).
UNASSIGNED = {"AQ", "BV", "HM", "TF", "GS", "UM"}


# ------------------------------------------------------------------ region lists
def test_seven_regions_with_names_and_version():
    assert set(REGIONS) == SEVEN and set(REGION_NAMES) == SEVEN
    assert REGION_NAMES["middle_east_north_africa"] == "Middle East and North Africa"
    assert REGIONS_VERSION


def test_every_code_is_valid_iso_shape_and_named():
    for codes in REGIONS.values():
        for code in codes:
            assert len(code) == 2 and code.isalpha() and code.isupper()
            assert COUNTRY_NAMES[code]
    assert len(COUNTRY_NAMES) == sum(len(c) for c in REGIONS.values())
    assert 230 <= len(COUNTRY_NAMES) <= 249
    assert not UNASSIGNED & set(COUNTRY_NAMES)


def test_no_country_in_two_regions_and_lists_are_sorted():
    seen: dict[str, str] = {}
    for rid, codes in REGIONS.items():
        assert list(codes) == sorted(codes) and len(codes) == len(set(codes))
        for code in codes:
            assert code not in seen, f"{code} is in {seen[code]} and {rid}"
            seen[code] = rid


def test_ambiguous_countries_follow_the_documented_rule():
    where = {c: r for r, codes in REGIONS.items() for c in codes}
    assert where["RU"] == "europe" and where["TR"] == "middle_east_north_africa"
    assert where["EG"] == "middle_east_north_africa" and where["MX"] == "latin_america_caribbean"
    assert where["US"] == where["CA"] == "north_america"
    assert where["KZ"] == where["GE"] == "asia" and where["ZA"] == "africa"


def test_lists_are_stable_snapshots():
    # A change to any list must come with a new REGIONS_VERSION and an updated snapshot.
    assert {r: len(c) for r, c in REGIONS.items()} == {
        "north_america": 5, "latin_america_caribbean": 50, "europe": 52, "africa": 51,
        "middle_east_north_africa": 23, "asia": 35, "oceania": 27}
    assert REGIONS_VERSION == "2026-10-v1"
    assert REGIONS["north_america"] == ("BM", "CA", "GL", "PM", "US")


def test_expand_dedupes_and_sorts():
    assert expand([], []) == []
    assert expand(["north_america"], ["US", "EG"]) == ["BM", "CA", "EG", "GL", "PM", "US"]
    assert expand(["north_america", "north_america"], []) == list(REGIONS["north_america"])
    assert regions_of(["EG", "US", "XX"]) == ["north_america", "middle_east_north_africa"]
    assert regions_of([]) == []


# ------------------------------------------------------------------ profile validation
def test_profile_defaults_and_expansion():
    p = SearchProfile()
    assert (p.regions, p.geo_mode, p.geo_basis, p.geo_countries) == (
        [], "include", "author_affiliation", [])
    p = SearchProfile(regions=["oceania", "oceania"], countries=["eg", "EG", "us"])
    assert p.regions == ["oceania"] and p.countries == ["EG", "US"]
    assert p.geo_countries == sorted({*REGIONS["oceania"], "EG", "US"})


def test_profile_rejects_bad_codes_regions_mode_and_basis():
    with pytest.raises(ValueError, match="unknown country code"):
        SearchProfile(countries=["ZZ"])
    with pytest.raises(ValueError, match="ISO 3166-1"):
        SearchProfile(countries=["USA"])
    with pytest.raises(ValueError, match="unknown region"):
        SearchProfile(regions=["atlantis"])
    with pytest.raises(ValueError):
        SearchProfile(geo_mode="maybe")
    with pytest.raises(ValueError, match="author_affiliation"):
        SearchProfile(geo_basis="study_location")
    assert GEO_BASES == ("author_affiliation",)


# ------------------------------------------------------------------ OpenAlex query
def test_openalex_filter_include_and_exclude_strings():
    inc = build_filter(SearchProfile(regions=["north_america"], countries=["EG"]))
    assert "authorships.institutions.country_code:BM|CA|EG|GL|PM|US" in inc
    exc = build_filter(SearchProfile(countries=["US", "CA"], geo_mode="exclude"))
    assert "authorships.institutions.country_code:!CA|US" in exc
    assert "country_code" not in build_filter(SearchProfile())
    assert "country_code" not in build_filter(SearchProfile(geo_mode="exclude"))


def test_openalex_sends_the_exact_filter_over_a_mock_transport():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    conn = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    profile = SearchProfile(year_from=2020, year_to=2021, document_types=["other"],
                            languages=[], countries=["EG", "CA"], geo_mode="exclude")
    assert list(conn.search(profile)) == []
    assert seen[0].url.params["filter"] == (
        "publication_year:2020-2021,is_retracted:false,"
        "authorships.institutions.country_code:!CA|EG")


# ------------------------------------------------------------------ runs through the API
class Fake:
    def __init__(self, name, papers, filters):
        self.name, self.tier, self.papers = name, "open", papers
        self.supported_filters = frozenset(filters)

    def search(self, profile):
        yield from self.papers


ALL = {"years", "document_types", "languages", "countries"}


def paper(i, countries, source="openalex", **kw):
    return Paper(source=source, id=f"W{i}", doi=f"10.1/{i}", title=f"Paper {i}", year=2020,
                 native_type="article", category="peer_reviewed", language="en",
                 countries=countries, **kw)


def run_api(tmp_path, connectors, profile, libraries=None):
    by_name = {c.name: c for c in connectors}
    app = create_app(Settings(database_path=tmp_path / "t.sqlite", search_max_results=50),
                     connector_factory=lambda lib: by_name.get(lib, Fake(lib, [], ALL)))
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        body = {"profile": {"query": "r", "year_from": 2020, "year_to": 2020, **profile},
                "libraries": libraries or ["openalex"]}
        res = client.post("/api/search", json=body)
        if res.status_code != 200:
            return res, None, client
        detail = client.get(f"/api/runs/{res.json()['id']}").json()
        return res, detail, client


def mixed():
    return [paper(1, ["EG"]), paper(2, ["US", "FR"]), paper(3, []), paper(4, ["FR"]),
            paper(5, ["CN"])]


def test_include_counts_matched_unknown_outside(tmp_path):
    res, detail, _ = run_api(tmp_path, [Fake("openalex", mixed(), ALL)],
                             {"regions": ["middle_east_north_africa"], "countries": ["US"]})
    g = res.json()["summary"]["geography"]
    assert (g["matched"], g["unknown"], g["outside"]) == (2, 1, 2)
    assert g["active"] and g["mode"] == "include" and g["basis"] == "author_affiliation"
    status = {p["id"]: p["geo_status"] for p in detail["papers"]}
    assert status == {"W1": "matched", "W2": "matched", "W3": "unknown"}
    assert res.json()["summary"]["found"] == 3  # outside papers are not stored in the run


def test_exclude_counts_and_unknown_is_not_excluded(tmp_path):
    res, detail, _ = run_api(tmp_path, [Fake("openalex", mixed(), ALL)],
                             {"regions": ["europe"], "geo_mode": "exclude"})
    g = res.json()["summary"]["geography"]
    assert (g["matched"], g["unknown"], g["outside"]) == (2, 1, 2)
    status = {p["id"]: p["geo_status"] for p in detail["papers"]}
    assert status == {"W1": "matched", "W3": "unknown", "W5": "matched"}


def test_no_filter_means_no_geography_counts_or_status(tmp_path):
    res, detail, _ = run_api(tmp_path, [Fake("openalex", mixed(), ALL)], {})
    g = res.json()["summary"]["geography"]
    assert g["active"] is False and (g["matched"], g["unknown"], g["outside"]) == (0, 0, 0)
    assert {p["geo_status"] for p in detail["papers"]} == {""}
    assert res.json()["summary"]["found"] == 5


def test_connector_without_geography_support_says_so_and_does_not_guess(tmp_path):
    # arXiv cannot apply a country filter. Its paper has no country: it is kept and listed as
    # unknown (not dropped, not counted as a match). Even a country it happened to report is
    # structured data, so that one is checked, but nothing is guessed from text.
    arxiv = Fake("arxiv", [paper(7, [], source="arxiv")], {"years", "document_types"})
    res, detail, _ = run_api(
        tmp_path, [Fake("openalex", [paper(1, ["EG"])], ALL), arxiv],
        {"regions": ["middle_east_north_africa"]}, libraries=["openalex", "arxiv"])
    assert res.status_code == 200, res.text
    s = res.json()["summary"]
    assert s["unapplied_filters"] == {"openalex": [], "arxiv": ["languages", "countries"]}
    assert s["geography"]["unknown"] == 1 and s["geography"]["matched"] == 1
    assert {p["id"]: p["geo_status"] for p in detail["papers"]} == {
        "W1": "matched", "W7": "unknown"}


def test_no_geography_requested_reports_nothing_unapplied(tmp_path):
    arxiv = Fake("arxiv", [], {"years", "document_types"})
    res, _, _ = run_api(tmp_path, [Fake("openalex", [], ALL), arxiv], {},
                        libraries=["openalex", "arxiv"])
    # only the language default is unapplied; "countries" is not listed when none was asked
    assert res.json()["summary"]["unapplied_filters"] == {"openalex": [], "arxiv": ["languages"]}


def test_run_records_choices_expansion_and_version_reproducibly(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, _, _ = run_api(tmp_path / "a", [Fake("openalex", mixed(), ALL)],
                          {"regions": ["oceania", "north_america"], "countries": ["EG", "US"]})
    second, _, _ = run_api(tmp_path / "b", [Fake("openalex", mixed(), ALL)],
                           {"regions": ["north_america", "oceania"], "countries": ["US", "EG"]})
    g1, g2 = first.json()["summary"]["geography"], second.json()["summary"]["geography"]
    assert g1["expanded_countries"] == g2["expanded_countries"] == expand(
        ["oceania", "north_america"], ["EG", "US"])
    assert g1["regions"] == ["oceania", "north_america"]  # the choice is kept as made
    assert g1["countries"] == ["EG", "US"] and g1["regions_version"] == REGIONS_VERSION
    assert g1["matched"] == g2["matched"] and g1["outside"] == g2["outside"]


def test_invalid_geography_is_rejected_with_a_clear_message(tmp_path):
    for bad, text in (({"geo_basis": "study_location"}, "author_affiliation"),
                      ({"regions": ["narnia"]}, "unknown region"),
                      ({"countries": ["ZZ"]}, "unknown country"),
                      ({"geo_mode": "sometimes"}, "include")):
        (tmp_path / "x").mkdir(exist_ok=True)
        res, _, _ = run_api(tmp_path / "x", [Fake("openalex", [], ALL)], bad)
        assert res.status_code == 422
        assert text in res.text
        (tmp_path / "x" / "t.sqlite").unlink(missing_ok=True)


def test_regions_endpoint(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"),
                     connector_factory=lambda lib: Fake(lib, [], ALL))
    with TestClient(app) as client:
        data = client.get("/api/regions").json()
    assert data["version"] == REGIONS_VERSION and data["bases"] == ["author_affiliation"]
    assert [r["id"] for r in data["regions"]] == list(REGIONS)
    assert data["regions"][0]["countries"] == list(REGIONS["north_america"])
    by_code = {c["code"]: c for c in data["countries"]}
    assert by_code["EG"] == {"code": "EG", "name": "Egypt", "region": "middle_east_north_africa"}
    assert len(by_code) == len(COUNTRY_NAMES)


# ------------------------------------------------------------------ export
def test_csv_export_has_region_and_countries_with_unknown_spelled_out(tmp_path):
    res, detail, client = run_api(
        tmp_path, [Fake("openalex", mixed(), ALL)], {"regions": ["middle_east_north_africa"]})
    out = client.post(f"/api/runs/{res.json()['id']}/export", json={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(out.content.decode("utf-8-sig"))))
    by_title = {r["Title"]: r for r in rows}
    assert by_title["Paper 1"]["Affiliation countries"] == "EG"
    assert by_title["Paper 1"]["Region"] == "Middle East and North Africa"
    assert by_title["Paper 1"]["Geography match"] == "matched"
    assert by_title["Paper 3"]["Affiliation countries"] == "unknown"
    assert by_title["Paper 3"]["Region"] == "unknown"
    assert by_title["Paper 3"]["Geography match"] == "country unknown"
    xlsx = client.post(f"/api/runs/{res.json()['id']}/export", json={"format": "xlsx"})
    assert xlsx.status_code == 200


def test_to_csv_without_geography_data_still_says_unknown():
    row = list(csv.DictReader(io.StringIO(
        to_csv([{"title": "T", "countries": []}], {}).decode("utf-8-sig"))))[0]
    assert row["Affiliation countries"] == row["Region"] == "unknown"
    assert row["Geography match"] == "not filtered"
