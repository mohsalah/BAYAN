from bayan.models import Paper
from bayan.retrieval import collect


def paper(i, year=2020, doi="", source="openalex"):
    return Paper(source=source, id=str(i), doi=doi, year=year, title=f"t{i}")


def test_dedupes_by_doi_across_sources():
    result = collect([paper(1, doi="10.1/x"), paper(2, doi="10.1/X", source="crossref")])
    assert len(result.papers) == 1 and result.duplicates == 1


def test_dedupes_by_source_and_id_without_doi():
    result = collect([paper(1), paper(1), paper(2)])
    assert len(result.papers) == 2 and result.duplicates == 1


def test_quota_caps_each_year_and_reports_shortfall():
    quotas = {2019: 2, 2020: 2}
    result = collect([paper(1, 2020), paper(2, 2020), paper(3, 2020), paper(4, 2019)], quotas)
    assert [p.id for p in result.papers] == ["1", "2", "4"]
    assert result.skipped_over_quota == 1
    assert result.shortfalls == {2019: (2, 1)}


def test_no_padding_when_source_runs_out():
    result = collect([], {2020: 5})
    assert result.papers == [] and result.shortfalls == {2020: (5, 0)}


def test_stops_early_when_all_quotas_full():
    def stream():
        yield paper(1, 2020)
        raise AssertionError("consumed past a full quota")

    result = collect(stream(), {2020: 1})
    assert len(result.papers) == 1 and result.shortfalls == {}


def test_papers_without_year_are_counted_not_kept_under_quotas():
    result = collect([paper(1, year=None)], {2020: 1})
    assert result.papers == [] and result.skipped_no_year == 1
