"""Reading only the records a search keeps (found by the M27 bench: every extra record at a
page boundary is another library page, 10 OpenAlex credits)."""

from itertools import islice

from bayan.models import Paper
from bayan.retrieval import collect
from bayan.search import _interleave, _scan


def paper(i, source="openalex", doi=None):
    return Paper(source=source, id=f"W{i}", doi=f"10.1/{i}" if doi is None else doi,
                 title=f"Paper {i}", year=2020, native_type="article", category="peer_reviewed")


def counted(items, pulled):
    for item in items:
        pulled.append(item)
        yield item


def test_collect_stops_at_the_limit_without_reading_one_more():
    pulled = []
    result = collect(counted([paper(i) for i in range(5)], pulled), limit=3)
    assert len(result.papers) == 3 and len(pulled) == 3


def test_the_limit_counts_unique_papers_not_duplicates():
    stream = [paper(1), paper(1, source="crossref"), paper(2), paper(3)]
    result = collect(stream, limit=2)
    assert [p.doi for p in result.papers] == ["10.1/1", "10.1/2"] and result.duplicates == 1


def test_a_limit_of_zero_reads_nothing():
    pulled = []
    assert collect(counted([paper(1)], pulled), limit=0).papers == [] and pulled == []


def test_interleave_reads_one_library_record_at_a_time():
    a, b = [], []
    stream = _interleave([counted([paper(1), paper(2), paper(3)], a),
                          counted([paper(11, "crossref"), paper(12, "crossref")], b)])
    assert [p.id for p in islice(stream, 3)] == ["W1", "W11", "W2"]
    assert len(a) == 2 and len(b) == 1  # crossref's second record was not read
    assert [p.id for p in stream] == ["W12", "W3"]  # a short library drops out cleanly


def test_scan_stops_at_its_limit_without_reading_one_more():
    pulled, state = [], {"scanned": 0, "stopped": "exhausted"}
    assert len(list(_scan(counted([paper(i) for i in range(5)], pulled), 3, state))) == 3
    assert len(pulled) == 3 and state["stopped"] == "scan_limit"
