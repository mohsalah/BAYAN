"""The reliability bench: cache, gold verification, agents, scoring, models, report. No live calls."""

import csv
import json
import random

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accounts_search import FakeConnector, make_paper, register  # noqa: F401

from bayan.api import create_app
from bayan.config import Settings
from reliability import gold, metrics, ml, report, runner
from reliability.agents import AgentConfig, AreaAgent, special_query
from reliability.client import Client, FetchError

AREA = {"id": "1210", "name": "Music", "field": {"id": "12", "name": "Arts and Humanities"},
        "domain": {"id": "2", "name": "Social Sciences"}, "frascati": "Humanities and the arts",
        "all_topics": ["Musicology and Musical Analysis", "Music History and Culture"],
        "queries": [{"topic_id": "T1", "query": "Musicology and Musical Analysis",
                     "keywords": ["sonata", "harmony"]}]}


def no_sleep(_):
    pass


# ---------------------------------------------------------------- client

def test_client_caches_retries_and_keeps_key_out_of_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429)
        if "missing" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"ok": True})

    c = Client(tmp_path, openalex_key="secret-key", transport=httpx.MockTransport(handler),
               sleep=no_sleep)
    assert c.get_json("https://api.openalex.org/works", {"filter": "x"}) == {"ok": True}
    assert len(calls) == 2 and "api_key=secret-key" in calls[1]  # retried after the 429
    assert c.get_json("https://api.openalex.org/works", {"filter": "x"}) == {"ok": True}
    assert len(calls) == 2 and c.cache_hits == 1
    assert c.get_json("https://api.crossref.org/works/missing") is None
    cached = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert "secret-key" not in cached
    with pytest.raises(FetchError):
        Client(tmp_path, offline=True).get_json("https://api.openalex.org/never")


def test_client_gives_up_on_hard_errors(tmp_path):
    c = Client(tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(400)),
               sleep=no_sleep)
    with pytest.raises(FetchError, match="HTTP 400"):
        c.get_json("https://api.openalex.org/works", {"filter": "bad"})


# ---------------------------------------------------------------- gold

def crossref_like(titles: dict[str, str], handles: set[str] = frozenset()):
    def handler(request):
        url = str(request.url)
        if "api.crossref.org/works/" in url:
            doi = url.split("/works/", 1)[1].lower()
            if doi in titles:
                return httpx.Response(200, json={"message": {"title": [titles[doi]]}})
            return httpx.Response(404)
        if "doi.org/api/handles/" in url:
            doi = url.split("/handles/", 1)[1].lower()
            return httpx.Response(200, json={"responseCode": 1 if doi in handles else 100})
        return httpx.Response(404)
    return handler


def work(i, doi="", title=None, year=2010, cites=10):
    return {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/{doi}" if doi else None,
            "display_name": title or f"Paper number {i} on music", "publication_year": year,
            "type": "article", "language": "en", "cited_by_count": cites,
            "primary_location": {"source": {"display_name": "J. Music"}},
            "authorships": [{"author": {"display_name": "Ana López"}}],
            "primary_topic": {"id": "https://openalex.org/T1",
                              "display_name": "Musicology and Musical Analysis"},
            "open_access": {"oa_url": ""}}


def test_verify_rules(tmp_path):
    titles = {"10.1/ok": "Paper number 1 on music", "10.1/bad": "Something else entirely"}
    c = Client(tmp_path, transport=httpx.MockTransport(crossref_like(titles, {"10.5281/z"})),
               sleep=no_sleep)
    assert gold.verify(c, work(1, "10.1/ok"))[0] == "ok_crossref"
    assert gold.verify(c, work(2, "10.1/bad"))[0] == "drop_crossref_title_mismatch"
    assert gold.verify(c, work(3, "10.5281/z"))[0] == "ok_doi_registered"
    assert gold.verify(c, work(4, "10.1/nowhere"))[0] == "drop_doi_not_found"
    assert gold.verify(c, work(5, "10.5860/choice.46-1"))[0] == "drop_review_record"
    assert gold.verify(c, work(6))[0] == "ok_openalex_only"
    assert gold.verify(c, {**work(7, "10.1/ok"), "display_name": ""})[0] == "drop_incomplete"


def test_strata_and_spans():
    assert gold.strata(50) == {"landmark": 15, "mid": 25, "recent": 10}
    assert sum(gold.strata(120).values()) == 120
    s = gold.spans(2006, 2022)
    assert len(s) == 5 and s[0][0] == 2006 and s[-1][1] == 2022
    assert all(b[0] == a[1] + 1 for a, b in zip(s, s[1:], strict=False))
    assert gold.spans(2020, 2021) == [(2020, 2020), (2021, 2021)]


def test_build_area_fills_strata_drops_bad_records_and_prefers_dois(tmp_path):
    rng = random.Random(0)
    counter = iter(range(10_000))
    titles = {}

    def handler(request):
        url = str(request.url)
        if "api.openalex.org/works" in url:
            has_doi = "has_doi%3Atrue" in url or "has_doi:true" in url
            per = int(request.url.params["per_page"])
            out = []
            for _ in range(per):
                i = next(counter)
                doi = f"10.9/{i}" if has_doi else ""
                if doi and rng.random() > 0.2:  # 20% of DOIs fail the Crossref check
                    titles[doi] = f"Paper number {i} on music"
                out.append(work(i, doi))
            return httpx.Response(200, json={"results": out[: per // 2] if has_doi else out})
        return crossref_like(titles)(request)

    c = Client(tmp_path, transport=httpx.MockTransport(handler), sleep=no_sleep)
    rows, summary = gold.build_area(c, AREA, 2006, 2025, 20)
    assert summary["kept"] == 20 and summary["by_stratum"] == {"landmark": 6, "mid": 10,
                                                               "recent": 4}
    assert summary["checks"].get("drop_doi_not_found", 0) > 0  # failures are counted
    assert all(r["provenance"]["check"].startswith("ok_") for r in rows)
    assert summary["without_doi"] == sum(1 for r in rows if not r["doi"])
    with_doi = [r for r in rows if r["doi"]]
    assert with_doi and all(titles.get(r["doi"]) for r in with_doi)  # every DOI checked out


# ---------------------------------------------------------------- matching and scoring

GOLD = [{"openalex_id": "W1", "doi": "10.1/a", "title": "Elements of Sonata Theory",
         "year": 2006, "first_author": "James Hepokoski", "language": "en", "stratum": "landmark",
         "query": "Musicology and Musical Analysis"},
        {"openalex_id": "W2", "doi": "", "title": "Canções do povo", "year": 2015,
         "first_author": "X", "language": "pt", "stratum": "mid", "query": "Music History"}]


def test_matching_by_doi_id_or_title():
    g = GOLD[0]
    assert metrics.matches({"doi": "https://doi.org/10.1/A"}, g)
    assert metrics.matches({"source": "openalex", "id": "W1"}, g)
    assert metrics.matches({"title": "Elements of sonata theory.", "year": 2007}, g)
    assert not metrics.matches({"title": "Elements of sonata theory", "year": 2012}, g)
    assert metrics.rank_of([{"doi": "10.9/x"}, {"doi": "10.1/a"}], g) == 2
    checks = metrics.metadata_checks({"doi": "10.1/a", "year": 2006, "title": g["title"],
                                      "first_author": "Hepokoski, J."}, g)
    assert checks == {"year": True, "title": True, "doi": True, "first_author": True}


def episode(papers_topic, papers_known, status=200, error=""):
    return {"area": AREA, "error": error, "steps": [
        {"kind": "sign_up", "status": 201},
        {"kind": "topic", "query": "Musicology and Musical Analysis", "query_origin": "topic_name",
         "years": [2006, 2025], "languages": ["en"], "libraries": ["openalex"],
         "status": status, "seconds": 2.0, "found": len(papers_topic), "papers": papers_topic},
        {"kind": "known_item", "query": GOLD[0]["title"], "gold_id": "W1", "stratum": "landmark",
         "years": [2006, 2025], "languages": ["en"], "libraries": ["openalex"], "status": 200,
         "seconds": 1.0, "found": len(papers_known), "papers": papers_known},
        {"kind": "known_item", "query": GOLD[1]["title"], "gold_id": "W2", "stratum": "mid",
         "years": [2006, 2025], "languages": ["en"], "libraries": ["openalex"], "status": 200,
         "seconds": 1.0, "found": 0, "papers": []},
        {"kind": "special_query", "query": '"x" AND (y)', "years": [2006, 2025],
         "languages": ["en"], "libraries": ["openalex"], "status": 200, "seconds": 1, "found": 30,
         "papers": []},
        {"kind": "language", "query": "Music", "language": "fr", "years": [2006, 2025],
         "languages": ["fr"], "libraries": ["openalex"], "status": 200, "seconds": 1,
         "found": 2, "papers": [{"language": "fr"}, {"language": "en"}]},
    ]}


def topic_papers(n_in, n_out, year=2010):
    return ([{"source": "openalex", "id": f"W{100 + i}", "topic": "Music History and Culture",
              "year": year, "doi": f"10.2/{i}"} for i in range(n_in)]
            + [{"source": "openalex", "id": f"W{200 + i}", "topic": "Elsewhere", "year": 1990}
               for i in range(n_out)])


def test_search_rows_and_scorecard():
    topics = {"Music History and Culture": ("1210", "12"), "Elsewhere": ("2104", "21")}
    known = [{"source": "openalex", "id": "W1", "doi": "10.1/a", "year": 2006,
              "title": GOLD[0]["title"], "first_author": "J. Hepokoski"}]
    ep = episode(topic_papers(30, 10), known)
    rows = metrics.search_rows(ep, GOLD, topics)
    topic, k1, k2, special, lang = rows
    assert topic["precision_subfield"] == 0.75 and topic["out_of_years"] == 10
    assert topic["gold_recall_at50"] == 0.0 and topic["gold_for_query"] == 1
    assert (k1["known_found"], k1["known_rank"], k1["gold_in_scope"]) == (1, 1, 1)
    assert k2["gold_in_scope"] == 0  # Portuguese: outside BAYAN's six languages
    assert lang["language_correct"] == 0.5
    card = metrics.scorecard_row(ep, rows, {"10.2/0": (True, False), "10.2/1": (False, True),
                                            "10.2/2": (False, False)})
    assert card["known_items"] == 1 and card["known_item_found"] == 1.0
    # two broken DOIs, but only one was added by BAYAN's own lookup
    assert card["dois_unresolved"] == 2 and card["dois_unresolved_lookup"] == 1
    assert card["complete"] == 1 and 0 <= card["reliability_score"] <= 100
    assert sum(metrics.WEIGHTS.values()) == 100


def test_partial_test_gets_no_score():
    rows = metrics.search_rows(episode([], []), GOLD, {})
    card = metrics.scorecard_row(episode([], []), rows, {})
    assert card["complete"] == 0 and card["reliability_score"] is None
    assert "topical_precision" in card["missing_measures"]
    failed = episode(topic_papers(40, 0), [], error="TypeError: x")
    card = metrics.scorecard_row(failed, metrics.search_rows(failed, GOLD, {}), {})
    assert card["reliability_score"] is None


def test_bibtex_check():
    ep = {"steps": [{"kind": "export_bibtex", "status": 200, "papers_sent": 1,
                     "expected_dois": ["10.1/a"],
                     "text": "@article{a, title={T}, doi={10.1/a}}"}]}
    pytest.importorskip("bibtexparser")
    assert metrics.bibtex_check(ep)["bibtex"] == "ok"
    ep["steps"][0]["text"] = "@article{a, title={T}, doi={10.1/zzz}}"
    assert metrics.bibtex_check(ep)["bibtex"] == "wrong"
    assert metrics.bibtex_check({"steps": [{"kind": "export_bibtex", "status": 422}]}) == {
        "bibtex": "not_available"}


# ---------------------------------------------------------------- an agent against the real app

def test_agent_runs_a_whole_episode_against_bayan(tmp_path):
    papers = [make_paper(i, 2010).model_copy(update={"title": t}) for i, t in
              enumerate(["Elements of Sonata Theory", "Another paper", "Third"])]
    settings = Settings(database_path=tmp_path / "a.sqlite", job_resume_interval=0,
                        searches_per_hour=1000)
    app = create_app(settings, connector_factory=lambda lib: FakeConnector(papers))
    with TestClient(app) as http:
        cfg = AgentConfig(base_url="http://testserver", known_items=1, topic_queries=1)
        ep = AreaAgent(cfg, AREA, [GOLD[0]], http=http).run().to_dict()
        assert http.get("/api/libraries").status_code == 200  # the caller's client stays open
    assert ep["error"] == ""
    kinds = [s["kind"] for s in ep["steps"]]
    assert kinds[0] == "sign_up" and "known_item" in kinds and "special_query" in kinds
    assert all(s["status"] in (200, 201, 422) for s in ep["steps"]), ep["steps"]
    known = next(s for s in ep["steps"] if s["kind"] == "known_item")
    assert metrics.rank_of(known["papers"], GOLD[0]) is not None
    assert special_query(AREA) == '"Musicology and Musical Analysis" AND (sonata OR harmony)'


# ---------------------------------------------------------------- models and report

def synthetic_rows(n=400, seed=3):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        field = f"F{i % 5}"
        kind = rng.choice(["topic", "known_item"])
        words = rng.randint(1, 12)
        bad = field == "F0" or words > 9  # failures concentrate in one field and long queries
        row = {"subfield_id": str(i % 20), "subfield": f"S{i % 20}", "field_id": field,
               "field": field, "domain": "D", "kind": kind, "query_origin": "topic_name",
               "stratum": "", "libraries": "openalex", "query_words": str(words),
               "query_chars": str(words * 7), "boolean": "0", "languages": "en",
               "year_from": "2006", "year_to": "2025", "status": "200",
               "found": "10" if bad else "50", "precision_field": "0.3" if bad else "0.9",
               "gold_in_scope": "1", "known_found": "0" if bad else "1"}
        rows.append(row)
    return rows


def test_failure_predictor_learns_and_validates_on_unseen_fields():
    pytest.importorskip("sklearn")
    out = ml.failure_predictor(synthetic_rows())
    assert out["validation"].startswith("grouped by field")
    assert out["boosted_auc"] > 0.7
    top = {s["feature"] for s in out["strongest_signals"][:3]}
    assert "query_words" in top or "query_chars" in top


def test_query_forms_and_regressions():
    rows = [{"kind": "topic", "status": "200", "field": "Music", "query_origin": o,
             "precision_field": p, "found": "50"} for o, p in
            [("subfield_name", "0.2")] * 3 + [("topic_name", "0.8")] * 3]
    best = ml.query_forms(rows)["best_form_per_field"]
    assert best["Music"]["form"] == "topic_name"
    now = [{"subfield_id": "1", "subfield": "A", "reliability_score": "70",
            "known_item_found": "0.5"}]
    before = [{"subfield_id": "1", "subfield": "A", "reliability_score": "80",
               "known_item_found": "0.9"}]
    reg = ml.regressions(now, before)
    assert reg and "score 80.0 -> 70.0" in reg[0]["reasons"]
    assert ml.regressions(before, before) == []


def test_ranker_beats_bayan_order_when_citations_carry_relevance():
    pytest.importorskip("sklearn")
    rng = random.Random(1)
    episodes = []
    for f in range(4):
        area = {**AREA, "id": str(f), "field": {"id": str(f), "name": f"F{f}"}}
        steps = []
        for _ in range(8):
            papers = []
            for r in range(20):
                rel = rng.random() < 0.4
                papers.append({"source": "openalex", "id": f"W{r}", "year": 2010,
                               "citations": rng.randint(200, 900) if rel else rng.randint(0, 50),
                               "topic": "Music History and Culture" if rel else "Elsewhere",
                               "title": "t"})
            steps.append({"kind": "topic", "query": "music", "papers": papers})
        episodes.append({"area": area, "steps": steps})
    taxonomy = {"areas": [{"id": str(f), "all_topics": ["Music History and Culture"]}
                          for f in range(4)]}
    out = ml.ranker(episodes, taxonomy)
    assert out["ndcg10_reranked"] > out["ndcg10_bayan_order"]


def test_report_is_written_with_backlog(tmp_path):
    pytest.importorskip("sklearn")
    topics = {"Music History and Culture": ("1210", "12"), "Elsewhere": ("2104", "21")}
    ep = episode(topic_papers(10, 30), [])
    rows = metrics.search_rows(ep, GOLD, topics)
    card = metrics.scorecard_row(ep, rows, {})
    run = tmp_path / "r1"
    (run / "raw" / "episodes").mkdir(parents=True)
    for name, data in (("searches.csv", rows), ("scorecard.csv", [card])):
        keys = list(dict.fromkeys(k for r in data for k in r))
        with (run / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(data)
    (run / "run.json").write_text(json.dumps({
        "run_id": "r1", "bayan_commit": "abc", "scored": 1, "areas": 1, "searches": len(rows),
        "minutes": 1, "config": {"year_from": 2006, "year_to": 2025, "per_search": 50,
                                 "known_items": 2}}))
    text = report.write(run).read_text()
    assert "Prioritised improvements" in text and "Keep searches inside the field" in text
    assert "Find papers by title" in text and "Not tested because BAYAN does not have it" in text


# ---------------------------------------------------------------- identifiers when there is no DOI

def nodoi(i, **kw):
    w = work(i, "", title="The Social Life of Music in Cairo", **kw)
    return w


def registry(title="The Social Life of Music in Cairo"):
    def handler(request):
        url = str(request.url)
        if "europepmc" in url:
            return httpx.Response(200, json={"resultList": {"result": [{"title": title}]}})
        if "export.arxiv.org" in url:
            return httpx.Response(200, text=f"<feed><entry><title>{title}</title></entry></feed>")
        if "openlibrary.org/isbn/" in url:
            return httpx.Response(200, json={"title": title})
        if "openlibrary.org/search.json" in url:
            return httpx.Response(200, json={"docs": [
                {"title": "Unrelated", "author_name": ["Ana López"], "isbn": ["9780306406157"]},
                {"title": title, "author_name": ["Ana López"], "first_publish_year": 2010,
                 "isbn": ["0306406152", "9780306406157"]}]})
        if "hdl.handle.net" in url:
            return httpx.Response(200, json={"responseCode": 1})
        return httpx.Response(404)
    return handler


def test_identifiers_from_openalex_ids_and_links():
    w = {**nodoi(1), "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/32501203",
                             "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/7270000"},
         "locations": [{"landing_page_url": "https://arxiv.org/abs/2101.00001v2"},
                       {"landing_page_url": "https://ruj.uj.edu.pl/xmlui/handle/item/49766"},
                       {"landing_page_url": "https://hdl.handle.net/10419/123456"},
                       {"landing_page_url": "http://www.loc.gov/catdir/toc/ecip067/2006001767.html"},
                       {"landing_page_url": "http://ci.nii.ac.jp/ncid/BB26098147"},
                       {"pdf_url": "http://www.gbv.de/dms/bowker/toc/9780306406157.pdf"}]}
    assert gold.identifiers(w) == {"pmid": "32501203", "pmcid": "PMC7270000",
                                   "arxiv": "2101.00001", "handle": "10419/123456",
                                   "lccn": "2006001767", "ncid": "BB26098147",
                                   "isbn": "9780306406157"}
    assert gold.isbn13_ok("9780306406157") and not gold.isbn13_ok("9780306406158")


def test_no_doi_uses_the_fields_own_identifier_checked_at_its_registry(tmp_path):
    c = Client(tmp_path, transport=httpx.MockTransport(registry()), sleep=no_sleep)
    med = {**nodoi(1), "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"},
           "locations": [{"landing_page_url": "https://hdl.handle.net/1/2"}]}
    assert gold.verify(c, med)[0] == "ok_pmid"  # PMID ranks above the handle
    phys = {**nodoi(2), "locations": [{"landing_page_url": "https://arxiv.org/abs/1234.5678"}]}
    assert gold.verify(c, phys)[0] == "ok_arxiv"
    book = {**nodoi(3), "type": "book", "ids": {}, "locations": []}
    status, detail = gold.verify(c, book)
    assert status == "ok_isbn" and book["_id"] == ("isbn", "9780306406157")
    rec = gold.record(book, AREA, "mid", status, detail, "now")
    assert (rec["id_type"], rec["id_value"]) == ("isbn", "9780306406157")
    assert rec["identifiers"]["isbn"] == "9780306406157"
    catalogue = {**nodoi(4), "locations": [{"landing_page_url": "http://ci.nii.ac.jp/ncid/BB1234567"}]}
    assert gold.verify(c, catalogue)[0] == "ok_ncid"
    bare = {**nodoi(5), "ids": {}, "locations": []}
    assert gold.verify(c, bare)[0] == "ok_openalex_only"


def test_identifier_that_does_not_match_is_not_used(tmp_path):
    c = Client(tmp_path, transport=httpx.MockTransport(registry("A different paper")),
               sleep=no_sleep)
    med = {**nodoi(1), "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"}, "locations": []}
    assert gold.verify(c, med)[0] == "ok_openalex_only"
    book = {**nodoi(2), "type": "book", "ids": {}, "locations": []}
    assert gold.verify(c, book)[0] == "ok_openalex_only"  # no title match, so no ISBN


def test_results_match_gold_by_pmid_and_arxiv_id():
    g = {"openalex_id": "W9", "doi": "", "title": "x", "year": 2010,
         "identifiers": {"pmid": "123", "arxiv": "2101.00001"}}
    assert metrics.matches({"source": "europepmc", "id": "MED:123"}, g)
    assert metrics.matches({"source": "arxiv", "id": "2101.00001v3"}, g)
    assert not metrics.matches({"source": "europepmc", "id": "MED:999"}, g)


def test_reliability_endpoint_serves_the_published_summary(tmp_path):
    path = tmp_path / "rel.json"
    settings = Settings(database_path=tmp_path / "r.sqlite", job_resume_interval=0,
                        reliability_file=path)
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/reliability")  # public: no sign-in needed
        assert r.status_code == 404 and "No reliability run" in r.json()["detail"]
        path.write_text(json.dumps({"run_id": "r1", "by_field": []}))
        assert c.get("/api/reliability").json()["run_id"] == "r1"


def test_publish_writes_a_compact_summary(tmp_path):
    topics = {"Music History and Culture": ("1210", "12"), "Elsewhere": ("2104", "21")}
    ep = episode(topic_papers(30, 10), [])
    rows = metrics.search_rows(ep, GOLD, topics)
    run = tmp_path / "r1"
    run.mkdir()
    for name, data in (("searches.csv", rows), ("scorecard.csv",
                                                 [metrics.scorecard_row(ep, rows, {})])):
        keys = list(dict.fromkeys(k for r in data for k in r))
        with (run / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(data)
    (run / "run.json").write_text(json.dumps({
        "run_id": "r1", "started": "2026-10-31T01:00:00", "bayan_commit": "abc", "scored": 1,
        "areas": 1, "searches": len(rows), "minutes": 1,
        "config": {"year_from": 2006, "year_to": 2025}}))
    out = report.publish(run, tmp_path / "pub.json", {"kept": 50})
    assert out["period"] == [2006, 2025] and out["gold_papers"] == 50
    assert out["by_field"][0]["name"] == "Arts and Humanities" and out["improvements"]
    assert json.loads((tmp_path / "pub.json").read_text())["run_id"] == "r1"


def _part(results, name, sid, config):
    d = results / name
    (d / "raw" / "episodes").mkdir(parents=True)
    (d / "raw" / "episodes" / f"{sid}.json").write_text("{}")
    runner.write_csv(d / "searches.csv", [{"subfield_id": sid, "kind": "topic"}])
    runner.write_csv(d / "scorecard.csv", [{"subfield_id": sid, "reliability_score": "70"}])
    (d / "run.json").write_text(json.dumps({
        "run_id": name, "started": f"2026-10-30T0{sid[-1]}:00", "minutes": 5.0,
        "bayan_commit": "abc", "base_url": "own server", "areas": 1, "scored": 1,
        "searches": 1, "config": config | {"run_id": name}, "server_env": {}}))


def test_merge_joins_parts_and_refuses_overlap_or_mixed_settings(tmp_path):
    cfg = {"per_search": 50, "year_from": 2006}
    _part(tmp_path, "r-d1", "2102", cfg)
    _part(tmp_path, "r-d2", "1101", cfg)
    out = runner.merge(tmp_path, "r", ["r-d1", "r-d2"])
    meta = json.loads((out / "run.json").read_text())
    assert meta["areas"] == 2 and meta["searches"] == 2 and meta["config"]["run_id"] == "r"
    cards = ml.read_csv(out / "scorecard.csv")
    assert [c["subfield_id"] for c in cards] == ["1101", "2102"]
    assert sorted(p.name for p in (out / "raw" / "episodes").iterdir()) == [
        "1101.json", "2102.json"]
    _part(tmp_path, "x-d1", "2102", cfg)
    with pytest.raises(ValueError, match="more than one part"):
        runner.merge(tmp_path, "x", ["r-d1", "x-d1"])
    _part(tmp_path, "y-d3", "3101", cfg | {"per_search": 100})
    with pytest.raises(ValueError, match="different settings"):
        runner.merge(tmp_path, "y", ["r-d2", "y-d3"])


def test_pilot_takes_the_largest_subfields_of_every_field():
    from reliability.__main__ import pilot_areas
    tax = {"areas": [
        {"id": "1101", "field": {"id": "11"}, "works_count": 5},
        {"id": "1102", "field": {"id": "11"}, "works_count": 9},
        {"id": "1201", "field": {"id": "12"}, "works_count": 1}]}
    assert sorted(pilot_areas(tax, 1)) == ["1102", "1201"]
    assert sorted(pilot_areas(tax, 2)) == ["1101", "1102", "1201"]
    assert pilot_areas(tax, 1, ["1101"]) == ["1101"]
