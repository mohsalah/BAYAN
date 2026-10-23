"""M21: counts per reference type (empties stated) and the client-side type filter."""

import shutil
import subprocess
from pathlib import Path

import pytest
from test_accounts_search import FakeConnector, make_paper, register

from bayan.api import STATIC, create_app
from bayan.config import Settings
from bayan.doctypes import CAPABILITIES, type_report
from bayan.libraries import LIBRARIES
from bayan.models import Paper


def row(report, key):
    return next(r for r in report["rows"] if r["id"] == key)


def paper(source="openalex", category="peer_reviewed", kind="", i=1):
    return {"source": source, "id": f"W{i}", "category": category, "document_kind": kind}


def test_empty_type_is_listed_and_says_no_library_lists_it():
    report = type_report(["peer_reviewed", "thesis"], [paper()], ["arxiv"])
    thesis = row(report, "thesis")
    assert thesis["count"] == 0 and thesis["selected"]
    assert thesis["message"] == ("Theses: 0 found in the chosen libraries "
                                 "(no selected library lists theses).")
    assert thesis["can_supply"] == []


def test_zero_with_capable_library_names_it_and_does_not_claim_it_is_missing():
    report = type_report(["thesis"], [], ["openalex", "arxiv"])
    thesis = row(report, "thesis")
    assert thesis["can_supply"] == ["openalex"]
    assert "OpenAlex can list theses, but none matched this search" in thesis["message"]
    assert "arXiv" not in thesis["message"]


def test_capability_text_follows_each_connector():
    assert CAPABILITIES["arxiv"] == {"other"}
    assert CAPABILITIES["europepmc"] == {"peer_reviewed", "other"}
    report = type_report(["peer_reviewed", "other", "report"], [], ["arxiv", "europepmc"])
    assert row(report, "peer_reviewed")["can_supply"] == ["europepmc"]
    assert row(report, "other")["can_supply"] == ["arxiv", "europepmc"]
    assert "Europe PMC / PubMed" in row(report, "peer_reviewed")["message"]
    assert "arXiv and Europe PMC / PubMed can list other" in row(report, "other")["message"]
    assert "no selected library lists reports" in row(report, "report")["message"]


def test_every_available_search_library_has_recorded_capabilities():
    for lib in LIBRARIES:
        if lib["status"] == "available" and lib["role"] == "search":
            assert lib["id"] in CAPABILITIES, lib["id"]


def test_unrecorded_capability_is_not_guessed():
    report = type_report(["thesis"], [], ["mystery"])
    message = row(report, "thesis")["message"]
    assert "0 found" in message and "not recorded" in message and "mystery" in message
    assert row(report, "thesis")["capability_unknown"] == ["mystery"]


def test_counts_split_by_library_and_manuals_are_their_own_row():
    papers = [paper("openalex", i=1), paper("crossref", i=2), paper("openalex", i=3),
              paper("user", "other", "manual", i=4)]
    report = type_report(["peer_reviewed", "other"], papers, ["openalex", "crossref"])
    pr = row(report, "peer_reviewed")
    assert pr["count"] == 3 and pr["by_library"] == {"openalex": 2, "crossref": 1}
    assert pr["message"] == "Peer-reviewed papers: 3 found (OpenAlex 2, Crossref 1)."
    assert row(report, "other")["count"] == 0          # the manual is not counted as Other
    manual = row(report, "manual")
    assert manual["count"] == 1 and "user-supplied" in manual["label"]
    assert report["total"] == 4


def test_manual_row_is_present_even_when_empty():
    manual = row(type_report(["thesis"], [], ["openalex"]), "manual")
    assert manual["count"] == 0 and "No library is searched for manuals" in manual["message"]


def test_unselected_type_with_papers_is_still_counted():
    report = type_report(["peer_reviewed"], [paper(category="report")], ["openalex"])
    assert row(report, "report")["count"] == 1 and not row(report, "report")["selected"]


def test_legacy_selected_types_are_normalised():
    report = type_report(["article", "review", "manual"], [], ["openalex"])
    ids = [r["id"] for r in report["rows"]]
    assert ids == ["peer_reviewed", "other", "manual"]


def test_run_detail_lists_selected_types_with_zero(tmp_path):
    fake = FakeConnector([make_paper(1), make_paper(2)])
    settings = Settings(database_path=tmp_path / "t.sqlite", search_max_results=50,
                        searches_per_hour=5)
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings, connector_factory=lambda lib: fake)) as client:
        register(client)
        body = {"profile": {"query": "x", "year_from": 2020, "year_to": 2021,
                            "document_types": ["peer_reviewed", "thesis", "report"]}}
        run_id = client.post("/api/search", json=body).json()["id"]
        detail = client.get(f"/api/runs/{run_id}").json()
        report = detail["type_report"]
        ids = [r["id"] for r in report["rows"]]
        assert ids == ["peer_reviewed", "thesis", "report", "manual"]
        assert row(report, "peer_reviewed")["count"] == 2
        assert row(report, "thesis")["count"] == 0
        assert "OpenAlex can list theses" in row(report, "thesis")["message"]
        assert report["total"] == len(detail["papers"]) == 2
        assert {p["type_key"] for p in detail["papers"]} == {"peer_reviewed"}
        assert detail["papers"][0]["user_supplied"] is False


# ---------------------------------------------------------------- client logic (node)

NODE = shutil.which("node")


def pure_block():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index("/* pure:start")
    end = html.index("/* pure:end */")
    return html[start:end]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_client_filter_logic_runs_under_node(tmp_path):
    script = pure_block() + """
const assert = require('assert');
const P = (id, type_key) => ({ id, type_key, source: 's' });
const papers = [P(1, 'peer_reviewed'), P(2, 'report'), P(3, 'peer_reviewed'), P(4, 'manual')];
assert.deepStrictEqual(countByType(papers), { peer_reviewed: 2, report: 1, manual: 1 });
assert.strictEqual(filterByTypes(papers, []).length, 4);
assert.deepStrictEqual(filterByTypes(papers, ['report', 'manual']).map(p => p.id), [2, 4]);
assert.strictEqual(filterByTypes(papers, ['thesis']).length, 0);
assert.deepStrictEqual(toggleType([], 'report'), ['report']);
assert.deepStrictEqual(toggleType(['report', 'manual'], 'report'), ['manual']);
assert.deepStrictEqual(setStats(papers, ['peer_reviewed'], 50), { total: 4, inView: 2, shown: 2 });
assert.deepStrictEqual(setStats(papers, [], 3), { total: 4, inView: 4, shown: 3 });
const view = filterByTypes(papers, ['peer_reviewed']);
assert.deepStrictEqual(exportPapers('view', papers, view, [papers[1]]).map(p => p.id), [1, 3]);
assert.deepStrictEqual(exportPapers('all', papers, view, []).map(p => p.id), [1, 2, 3, 4]);
assert.deepStrictEqual(exportPapers('selected', papers, view, [papers[1]]).map(p => p.id), [2]);
assert.strictEqual(typeKeyOf({ category: 'thesis' }), 'thesis');
"""
    path = tmp_path / "pure.js"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_script_parses(tmp_path):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = html[html.rindex("<script>") + 8:html.rindex("</script>")]
    path = tmp_path / "page.js"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_page_has_manual_and_filter_controls():
    html = Path(STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ('id="typeFilter"', 'id="typeReport"', 'id="manualBox"', 'id="exportScope"',
                   "Add a manual", "user-supplied"):
        assert needle in html
    assert Paper(source="user", id="m-1").document_kind == ""
