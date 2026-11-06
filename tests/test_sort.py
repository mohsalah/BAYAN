"""Most cited first (found by the M27 bench: text order missed each topic's landmark papers)."""

import httpx
from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.connectors.openalex import OpenAlexConnector
from bayan.models import Paper, SearchProfile

PASSWORD = "correct horse battery"
WORK = {"id": "https://openalex.org/W1", "title": "T", "publication_year": 2020, "type": "article"}


def paper(i, year, cites, source="openalex"):
    return Paper(source=source, id=f"{source}{i}", doi=f"10.1/{source}{i}", title=f"P{i}",
                 year=year, native_type="article", category="peer_reviewed", language="en",
                 citations=cites)


class Fake:
    """Records each query's period and how many records were read from it."""

    def __init__(self, name, papers, sorts=("relevance", "citations")):
        self.name, self.tier, self.papers = name, "open", papers
        self.supported_filters = frozenset({"years", "document_types", "languages"})
        self.supported_sorts = frozenset(sorts)
        self.queries, self.read = [], {}

    def search(self, profile):
        key = (profile.year_from, profile.year_to)
        self.queries.append((key, profile.sort))
        self.read[key] = 0
        rows = [p for p in self.papers if profile.year_from <= p.year <= profile.year_to]
        if profile.sort == "citations" and "citations" in getattr(self, "supported_sorts", ()):
            rows.sort(key=lambda p: -p.citations)
        for p in rows:
            self.read[key] += 1
            yield p


class NoSort(Fake):
    """A library without `supported_sorts` (read as relevance only)."""

    def __init__(self, name, papers):
        super().__init__(name, papers)
        del self.supported_sorts


def run(tmp_path, connectors, sort, quota=None, years=(2020, 2021)):
    by_name = {c.name: c for c in connectors}
    app = create_app(Settings(database_path=tmp_path / "t.sqlite", search_max_results=50),
                     connector_factory=lambda lib: by_name.get(lib) or Fake(lib, []))
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        body = {"profile": {"query": "r", "year_from": years[0], "year_to": years[1],
                            "languages": ["en"], "sort": sort},
                "libraries": list(by_name)}
        if quota:
            body["quota_total"] = quota
        res = client.post("/api/search", json=body)
        assert res.status_code == 200, res.text
        detail = client.get(f"/api/runs/{res.json()['id']}").json()
        return res.json()["summary"], detail


def ids(detail):
    return [p["id"] for p in detail["papers"]]


def test_openalex_asks_for_most_cited_first_only_when_chosen():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [WORK], "meta": {}})

    conn = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)),
                             sleep=lambda s: None)
    list(conn.search(SearchProfile(query="x", sort="citations")))
    list(conn.search(SearchProfile(query="x")))
    assert seen[0].url.params["sort"] == "cited_by_count:desc"
    assert "sort" not in seen[1].url.params


def test_relevance_is_the_default_order():
    assert SearchProfile().sort == "relevance"


def test_year_quotas_run_one_most_cited_query_per_year(tmp_path):
    # 2020 has old highly cited papers; 2021's are newer and less cited. One sorted query over
    # both years would fill the list with 2020 papers; per-year queries fill each year.
    lib = Fake("openalex", [paper(i, 2020, 1000 + i) for i in range(10)]
               + [paper(20 + i, 2021, i) for i in range(10)])
    summary, detail = run(tmp_path, [lib], "citations", quota=4)
    assert sorted(lib.queries) == [((2020, 2020), "citations"), ((2021, 2021), "citations")]
    assert ids(detail) == ["openalex9", "openalex8", "openalex29", "openalex28"]
    assert summary["shortfalls"] == {} and summary["sorted_by_year"] == ["openalex"]
    assert summary["sort"] == "citations" and summary["sort_unapplied"] == []


def test_a_full_year_is_not_read_further(tmp_path):
    lib = Fake("openalex", [paper(i, 2020, i) for i in range(10)]
               + [paper(20 + i, 2021, i) for i in range(10)])
    run(tmp_path, [lib], "citations", quota=4)
    # Two papers per year: each year's query is read twice and never a third time.
    assert lib.read == {(2020, 2020): 2, (2021, 2021): 2}


def test_years_without_a_quota_are_not_searched(tmp_path):
    lib = Fake("openalex", [paper(i, 2020 + i % 3, i) for i in range(9)])
    run(tmp_path, [lib], "citations", quota=2, years=(2020, 2022))
    assert len(lib.queries) == 2  # 2 papers over 3 years: one year gets no quota


def test_without_quotas_one_sorted_query_and_the_list_is_most_cited_first(tmp_path):
    lib = Fake("openalex", [paper(1, 2020, 5), paper(2, 2021, 50), paper(3, 2020, 20)])
    summary, detail = run(tmp_path, [lib], "citations")
    assert lib.queries == [((2020, 2021), "citations")]
    assert ids(detail) == ["openalex2", "openalex3", "openalex1"]
    assert summary["sorted_by_year"] == []


def test_a_library_without_a_citation_sort_is_reported_and_searched_once(tmp_path):
    oa = Fake("openalex", [paper(1, 2020, 30), paper(2, 2021, 10)])
    ax = NoSort("arxiv", [paper(1, 2020, 0, "arxiv"), paper(2, 2021, 0, "arxiv")])
    summary, detail = run(tmp_path, [oa, ax], "citations", quota=4)
    assert summary["sort_unapplied"] == ["arxiv"] and summary["sorted_by_year"] == ["openalex"]
    assert ax.queries == [((2020, 2021), "citations")]
    # arXiv reports no citations: its records follow, in arXiv's own order, not as "0".
    assert ids(detail) == ["openalex1", "openalex2", "arxiv1", "arxiv2"]


def test_relevance_keeps_the_library_order_and_one_query(tmp_path):
    lib = Fake("openalex", [paper(1, 2020, 5), paper(2, 2021, 50)])
    summary, detail = run(tmp_path, [lib], "relevance", quota=2)
    assert lib.queries == [((2020, 2021), "relevance")]
    assert ids(detail) == ["openalex1", "openalex2"]
    assert summary["sort"] == "relevance" and summary["sort_unapplied"] == []


class Warns(Fake):
    def __init__(self, name, papers):
        super().__init__(name, papers)
        self.warnings = []

    def search(self, profile):
        self.warnings.append("OpenAlex searched for 'react'")
        yield from super().search(profile)


def test_a_note_repeated_by_per_year_searches_is_shown_once(tmp_path):
    lib = Warns("openalex", [paper(1, 2020, 3), paper(2, 2021, 4)])
    summary, _ = run(tmp_path, [lib], "citations", quota=2)
    assert len(lib.queries) == 2 and summary["warnings"] == ["OpenAlex searched for 'react'"]
