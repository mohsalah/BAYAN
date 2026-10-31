"""BibTeX and RIS export: valid entries, nothing invented, special characters survive."""

import bibtexparser
import pytest
from fastapi.testclient import TestClient
from test_accounts_search import FakeConnector, make_paper, register, search

from bayan.api import create_app
from bayan.citations import bib_escape, cite_key, entry_type, to_bibtex, to_ris, unique_keys
from bayan.config import Settings
from bayan.connectors.arxiv import parse_entry
from bayan.connectors.crossref import parse_item
from bayan.connectors.europepmc import parse_result
from bayan.models import Paper


def p(**kw):
    base = {"source": "openalex", "id": "W1", "title": "Neutron transport in molten salts",
            "authors": ["Ana María López", "Bo Chen"], "year": 2021, "venue": "Ann. Nucl. Energy",
            "doi": "10.1016/j.anucene.2021.1", "native_type": "article",
            "category": "peer_reviewed", "language": "en", "keywords": ["MSR", "transport"],
            "abstract": "We model 50% of the core & the loop_2 at $5 cost."}
    return {**base, **kw}


def parse(text):
    db = bibtexparser.loads(text)
    return {e["ID"]: e for e in db.entries}


def test_bibtex_round_trip_keeps_every_field():
    entries = parse(to_bibtex([p()]))
    e = entries["lopez2021neutron"]
    assert e["ENTRYTYPE"] == "article" and e["journal"] == "Ann. Nucl. Energy"
    assert e["author"] == "Ana María López and Bo Chen" and e["year"] == "2021"
    assert e["doi"] == "10.1016/j.anucene.2021.1"
    assert e["url"] == "https://doi.org/10.1016/j.anucene.2021.1"
    assert e["abstract"] == r"We model 50\% of the core \& the loop\_2 at \$5 cost."
    assert e["note"] == "BAYAN record openalex:W1"


def test_missing_fields_are_left_out_not_invented():
    entry = parse(to_bibtex([p(authors=[], first_author="", year=None, venue="", doi="",
                                abstract="", keywords=[], language="", landing_url="")]))
    (e,) = entry.values()
    assert set(e) == {"ENTRYTYPE", "ID", "title", "note"}


def test_braces_backslashes_and_scripts_do_not_break_entries():
    nasty = p(title="A {broken} title \\ with } and ~ ^", authors=["محمد صلاح", "李 明"],
              venue="Journal {x")
    text = to_bibtex([nasty, p(id="W2")])
    entries = parse(text)
    assert len(entries) == 2  # the second entry is still read after the nasty one
    first = next(e for e in entries.values() if "broken" in e["title"])
    assert first["author"] == "محمد صلاح and 李 明"
    assert first["title"].count("{") == first["title"].count("}")


@pytest.mark.parametrize("native,category,expected", [
    ("book-chapter", "other", "incollection"), ("proceedings-article", "proceedings",
                                                "inproceedings"),
    ("dissertation", "thesis", "phdthesis"), ("report", "report", "techreport"),
    ("preprint", "other", "misc"), ("", "peer_reviewed", "article"), ("weird", "other", "misc"),
])
def test_entry_types(native, category, expected):
    assert entry_type({"native_type": native, "category": category}) == expected


def test_venue_goes_to_the_right_field():
    chapter = parse(to_bibtex([p(native_type="book-chapter", venue="Handbook of MSRs")]))
    assert next(iter(chapter.values()))["booktitle"] == "Handbook of MSRs"
    thesis = parse(to_bibtex([p(native_type="dissertation", venue="MIT")]))
    assert next(iter(thesis.values()))["school"] == "MIT"


def test_cite_keys_are_ascii_and_unique():
    assert cite_key(p(authors=["Ana María López"])) == "lopez2021neutron"
    assert cite_key(p(authors=["López, Ana"])) == "lopez2021neutron"
    assert cite_key(p(authors=["محمد صلاح"], title="دراسة")) == "paper2021"
    papers = [p(), p(id="W2"), p(id="W3")]
    assert unique_keys(papers) == ["lopez2021neutron", "lopez2021neutrona", "lopez2021neutronb"]
    assert len(parse(to_bibtex(papers))) == 3


def test_escape():
    assert bib_escape("a\\b {c}") == r"a\textbackslash{}b \textbraceleft{}c\textbraceright{}"
    assert bib_escape("line\nbreak") == "line break"


def test_ris_records():
    text = to_ris([p(), p(id="W2", native_type="preprint", authors=["Z"], doi="",
                          landing_url="https://arxiv.org/abs/1")])
    records = text.split("ER  - ")
    assert len([r for r in records if r.strip()]) == 2
    first = records[0].splitlines()
    assert first[0] == "TY  - JOUR"
    assert "AU  - Ana María López" in first and "AU  - Bo Chen" in first
    assert "DO  - 10.1016/j.anucene.2021.1" in first and "JO  - Ann. Nucl. Energy" in first
    assert "TY  - INPR" in records[1] and "UR  - https://arxiv.org/abs/1" in records[1]
    assert "\r\n" in text


# ---------------------------------------------------------------- every author from each library

def test_connectors_keep_all_authors():
    item = {"DOI": "10.1/x", "title": ["T"], "author": [
        {"given": "Ana", "family": "López"}, {"given": "Bo", "family": "Chen"},
        {"name": "CERN Collaboration"}]}
    assert parse_item(item).authors == ["Ana López", "Bo Chen", "CERN Collaboration"]
    record = {"id": "1", "source": "MED", "title": "T", "authorString": "Smith J, Doe A, Li X."}
    assert parse_result(record).authors == ["Smith J", "Doe A", "Li X"]


def test_arxiv_keeps_all_authors():
    import xml.etree.ElementTree as ET

    from bayan.connectors.arxiv import NS
    xml = (f'<entry xmlns="{NS["a"]}"><id>http://arxiv.org/abs/2101.00001v1</id>'
           "<title>T</title><published>2021-01-01T00:00:00Z</published>"
           "<author><name>Ana  López</name></author><author><name>Bo Chen</name></author></entry>")
    assert parse_entry(ET.fromstring(xml)).authors == ["Ana López", "Bo Chen"]


# ---------------------------------------------------------------- through the API

def test_export_endpoint_bibtex_and_ris(tmp_path):
    papers = [make_paper(i, authors=["Ana López", "Bo Chen"], first_author="Ana López")
              for i in range(3)]
    settings = Settings(database_path=tmp_path / "x.sqlite", job_resume_interval=0)
    with TestClient(create_app(settings,
                               connector_factory=lambda lib: FakeConnector(papers))) as c:
        register(c)
        run_id = search(c).json()["id"]
        r = c.post(f"/api/runs/{run_id}/export", json={"format": "bibtex"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-bibtex")
        assert 'filename="bayan-run-' in r.headers["content-disposition"]
        assert r.headers["content-disposition"].endswith('.bib"')
        entries = parse(r.text)
        assert len(entries) == 3
        assert {e["author"] for e in entries.values()} == {"Ana López and Bo Chen"}
        one = c.post(f"/api/runs/{run_id}/export",
                     json={"format": "ris", "papers": [{"source": "openalex", "id": "W1"}]})
        assert one.text.count("ER  - ") == 1 and "DO  - 10.1/1" in one.text
        bad = c.post(f"/api/runs/{run_id}/export", json={"format": "docx"})
        assert bad.status_code == 422


def test_duplicate_keeps_the_fuller_author_list():
    from bayan.retrieval import merge_into

    base = Paper(source="openalex", id="W1", authors=["A"])
    merge_into(base, Paper(source="crossref", id="10.1/x", authors=["A", "B", "C"]))
    assert base.authors == ["A", "B", "C"]
    merge_into(base, Paper(source="arxiv", id="1", authors=["A"]))
    assert base.authors == ["A", "B", "C"]


def test_rows_saved_before_author_lists_fall_back_to_first_author(tmp_path):
    from bayan.db import connect, init_db
    from bayan.search import paper_row_to_dict

    init_db(tmp_path / "o.sqlite")
    with connect(tmp_path / "o.sqlite") as conn:
        conn.execute("INSERT INTO papers(source, id, title, first_author, countries, keywords,"
                     " is_oa, oa_url) VALUES('openalex','W1','T','Ana López','[]','[]',0,'')")
        row = conn.execute("SELECT * FROM papers").fetchone()
        assert paper_row_to_dict(row)["authors"] == ["Ana López"]
