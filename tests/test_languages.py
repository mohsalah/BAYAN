"""M15: six languages. No live services; HTTP is faked with httpx.MockTransport."""

import csv
import html
import io
import json
import re
import shutil
import sqlite3
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from bayan.api import LOCALES, STATIC, create_app
from bayan.config import Settings
from bayan.connectors import EuropePMCConnector, OpenAlexConnector
from bayan.connectors.europepmc import build_query
from bayan.connectors.openalex import build_filter
from bayan.export import COLUMNS, to_csv
from bayan.models import (
    LANGUAGES,
    QUERY_MAX_LENGTH,
    Paper,
    SearchProfile,
    language_key,
    language_name,
)
from bayan.retrieval import collect
from bayan.textnorm import (
    cjk_bigrams,
    fts_query,
    index_text,
    normalize_arabic,
    normalize_text,
    tokenize,
)

SIX = ["en", "ar", "fr", "zh", "de", "es"]
PASSWORD = "correct horse battery"


# ------------------------------------------------------------------ locales
def load(code):
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def placeholders(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


def test_exactly_six_locale_files():
    assert sorted(p.stem for p in LOCALES.glob("*.json")) == sorted(SIX)
    assert sorted(LANGUAGES) == sorted(SIX)


@pytest.mark.parametrize("code", [c for c in SIX if c != "en"])
def test_locale_has_same_keys_as_english(code):
    en, other = load("en"), load(code)
    assert sorted(set(en) - set(other)) == [], f"{code} is missing keys"
    assert sorted(set(other) - set(en)) == [], f"{code} has extra keys"


@pytest.mark.parametrize("code", [c for c in SIX if c != "en"])
def test_locale_placeholders_match_english(code):
    en, other = load("en"), load(code)
    for key, text in en.items():
        assert placeholders(other[key]) == placeholders(text), f"{code}:{key}"


@pytest.mark.parametrize("code", SIX)
def test_locale_values_are_nonempty_strings(code):
    for key, text in load(code).items():
        assert isinstance(text, str) and text.strip(), f"{code}:{key}"


def test_translations_differ_from_english_for_most_keys():
    en = load("en")
    for code in ("ar", "fr", "zh", "de", "es"):
        same = [k for k, v in load(code).items() if v == en[k]]
        # Names such as BAYAN, DOI and PDF legitimately repeat; real text must not.
        assert len(same) < 12, (code, same)


def test_page_keys_all_exist_in_english():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    en = load("en")
    used = set(re.findall(r'data-i18n(?:-aria|-ph)?="([^"]+)"', page))
    used |= set(re.findall(r"\bt\('([\w.]+)'", page))
    used |= set(re.findall(r"'((?:help|stop|access|status)\.\w+)'", page))
    used |= {f"palette.{p}" for p in ("navy", "teal", "purple", "green", "amber", "rose")}
    used |= {f"lang.{c}" for c in SIX} | {"geo.include", "geo.exclude"}
    used |= {f"export.name.{s}" for s in ("selected", "view", "all")}
    used |= {f"type.{c}.{p}" for c in ("peer_reviewed", "proceedings", "thesis", "report", "other")
             for p in ("label", "desc")}
    used = {k for k in used if not k.endswith(".")}  # prefixes built at run time
    assert sorted(k for k in used if k not in en) == []


def test_help_map_keys_exist():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    block = page[page.index("const HELP = {"):page.index("};", page.index("const HELP = {"))]
    keys = re.findall(r"'((?:help|theme|ui)\.[\w.]+)'", block)
    assert len(keys) >= 25
    assert [k for k in keys if k not in load("en")] == []


def test_page_default_text_matches_english_locale():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    en = load("en")
    for key, text in re.findall(r'data-i18n="([^"]+)"[^>]*>([^<]*)<', page):
        assert html.unescape(text) == en[key], key


def test_locale_endpoint_serves_only_the_six(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"))
    with TestClient(app) as client:
        for code in SIX:
            assert client.get(f"/api/locales/{code}").json()["search.go"]
        assert client.get("/api/locales/ja").status_code == 404
        assert client.get("/api/locales/..%2Fapi").status_code in (404, 422)
        assert sorted(client.get("/api/languages").json()) == sorted(SIX)


# ------------------------------------------------------------------ language names
def test_language_names():
    assert [language_name(c) for c in SIX] == [
        "English", "Arabic", "French", "Chinese", "German", "Spanish"]
    assert language_name("") == language_name(None) == "Unknown language"
    assert language_name("ja") == "Japanese"
    assert language_name("xx") == "Other language (xx)"
    assert language_key("") == language_key(None) == "unknown"
    assert language_key(" AR ") == "ar"


# ------------------------------------------------------------------ profile validation
def test_queries_by_language_validation():
    p = SearchProfile(languages=["en", "ar"], queries_by_language={"en": " heat ", "ar": "حرارة"})
    assert p.queries_by_language == {"en": "heat", "ar": "حرارة"}
    assert SearchProfile(languages=["en", "fr"],
                         queries_by_language={"fr": "  ", "en": "x"}).queries_by_language == {
        "en": "x"}
    with pytest.raises(ValueError, match="unsupported language"):
        SearchProfile(languages=["en"], queries_by_language={"ja": "x"})
    with pytest.raises(ValueError, match="not chosen"):
        SearchProfile(languages=["en"], queries_by_language={"fr": "x"})
    with pytest.raises(ValueError, match="longer than"):
        SearchProfile(languages=["en", "fr"],
                      queries_by_language={"fr": "x" * (QUERY_MAX_LENGTH + 1)})
    with pytest.raises(ValueError):
        SearchProfile(query="x" * (QUERY_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="unsupported language"):
        SearchProfile(languages=["en", "ja"])


def test_language_queries_fall_back_to_the_main_query():
    p = SearchProfile(query="main", languages=["en", "zh", "ar"],
                      queries_by_language={"zh": "中子输运"})
    assert p.language_queries() == [("en", "main"), ("zh", "中子输运"), ("ar", "main")]
    assert SearchProfile(query="main", languages=["en", "fr"]).language_queries() == []
    one = p.for_language("zh", "中子输运")
    assert (one.languages, one.query, one.queries_by_language) == (["zh"], "中子输运", {})


def test_api_rejects_other_languages_with_a_clear_message(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"),
                     connector_factory=lambda lib: Fake(lib, [], ALL))
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        for profile, text in (
            ({"languages": ["ja"]}, "unsupported language"),
            ({"languages": ["en"], "queries_by_language": {"ja": "x"}}, "unsupported language"),
            ({"languages": ["en"], "queries_by_language": {"fr": "x"}}, "not chosen"),
        ):
            res = client.post("/api/search", json={"profile": profile, "libraries": ["openalex"]})
            assert res.status_code == 422 and text in json.dumps(res.json())


# ------------------------------------------------------------------ connector query building
def test_openalex_per_language_requests_use_that_languages_text_and_filter():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    conn = OpenAlexConnector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    profile = SearchProfile(query="heat transfer", year_from=2020, year_to=2021,
                            languages=["en", "ar", "zh"],
                            queries_by_language={"ar": "انتقال الحرارة", "zh": "传热"})
    for code, text in profile.language_queries():
        assert list(conn.search(profile.for_language(code, text))) == []
    got = [(r.url.params["search"], r.url.params["filter"]) for r in seen]
    base = "publication_year:2020-2021,is_retracted:false,type:article|review,language:"
    assert got == [("heat transfer", base + "en"), ("انتقال الحرارة", base + "ar"),
                   ("传热", base + "zh")]


def test_europepmc_per_language_queries():
    base = SearchProfile(year_from=2020, year_to=2021, languages=["fr", "de", "es"],
                         queries_by_language={"fr": "transfert thermique",
                                              "de": "Wärmeübertragung"}, query="heat")
    got = [build_query(base.for_language(c, t)) for c, t in base.language_queries()]
    assert got == [
        "(transfert thermique) AND (PUB_YEAR:[2020 TO 2021]) AND (LANG:fre) AND (NOT SRC:PPR)",
        "(Wärmeübertragung) AND (PUB_YEAR:[2020 TO 2021]) AND (LANG:ger) AND (NOT SRC:PPR)",
        "(heat) AND (PUB_YEAR:[2020 TO 2021]) AND (LANG:spa) AND (NOT SRC:PPR)",
    ]


def test_single_language_query_building_is_unchanged():
    p = SearchProfile(query="x", year_from=2020, year_to=2020, languages=["en", "fr"])
    assert build_filter(p).endswith("language:en|fr")
    assert "LANG:eng OR LANG:fre" in build_query(p)


def test_europepmc_requests_through_mock_transport():
    seen = []

    def handler(request):
        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": []}})

    conn = EuropePMCConnector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    p = SearchProfile(query="a", year_from=2020, year_to=2020, languages=["zh", "ar"],
                      queries_by_language={"zh": "癌症"})
    for code, text in p.language_queries():
        list(conn.search(p.for_language(code, text)))
    assert seen == ["(癌症) AND (PUB_YEAR:[2020 TO 2020]) AND (LANG:chi) AND (NOT SRC:PPR)",
                    "(a) AND (PUB_YEAR:[2020 TO 2020]) AND (LANG:ara) AND (NOT SRC:PPR)"]


# ------------------------------------------------------------------ runs through the API
ALL = {"years", "document_types", "languages", "countries"}


class Fake:
    """Answers per language: papers[language] is what a query filtered to it returns."""

    def __init__(self, name, papers, filters=ALL):
        self.name, self.tier, self.papers = name, "open", papers
        self.supported_filters = frozenset(filters)
        self.calls = []

    def search(self, profile):
        self.calls.append((tuple(profile.languages), profile.query))
        if isinstance(self.papers, dict):
            for code in profile.languages:
                yield from self.papers.get(code, [])
        else:
            yield from self.papers


def paper(i, language="en", doi=None, source="openalex", **kw):
    return Paper(source=source, id=f"W{i}", doi=f"10.1/{i}" if doi is None else doi,
                 title=f"Paper {i}", year=2020, native_type="article",
                 category="peer_reviewed", language=language, **kw)


def run_api(tmp_path, connectors, profile, libraries=None, max_results=50, extra=None):
    by_name = {c.name: c for c in connectors}
    app = create_app(Settings(database_path=tmp_path / "t.sqlite", search_max_results=max_results),
                     connector_factory=lambda lib: by_name.get(lib, Fake(lib, [])))
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "a@example.org", "password": PASSWORD})
        body = {"profile": {"query": "main", "year_from": 2020, "year_to": 2020, **profile},
                "libraries": libraries or ["openalex"], **(extra or {})}
        res = client.post("/api/search", json=body)
        if res.status_code != 200:
            return res, None, client
        return res, client.get(f"/api/runs/{res.json()['id']}").json(), client


def test_run_searches_once_per_language_and_records_the_queries(tmp_path):
    lib = Fake("openalex", {"en": [paper(1, "en")], "ar": [paper(2, "ar")],
                            "zh": [paper(3, "zh")]})
    res, detail, _ = run_api(tmp_path, [lib], {
        "languages": ["en", "ar", "zh"],
        "queries_by_language": {"ar": "انتقال الحرارة", "zh": "传热"}})
    assert lib.calls == [(("en",), "main"), (("ar",), "انتقال الحرارة"), (("zh",), "传热")]
    plan = res.json()["summary"]["language_queries"]
    assert plan["per_language"] is True
    assert plan["queries"] == {"en": "main", "ar": "انتقال الحرارة", "zh": "传热"}
    assert plan["run_per_language"] == ["openalex"] and plan["notes"] == []
    # Reproducible: the stored request keeps the user's per-language text.
    assert detail["request"]["profile"]["queries_by_language"] == {
        "ar": "انتقال الحرارة", "zh": "传热"}
    assert {p["language_key"]: p["language_name"] for p in detail["papers"]} == {
        "en": "English", "ar": "Arabic", "zh": "Chinese"}


def test_without_per_language_text_one_combined_query_is_unchanged(tmp_path):
    lib = Fake("openalex", [paper(1, "en"), paper(2, "fr")])
    res, _, _ = run_api(tmp_path, [lib], {"languages": ["en", "fr"]})
    assert lib.calls == [(("en", "fr"), "main")]
    assert res.json()["summary"]["language_queries"]["per_language"] is False


def test_library_without_language_filter_runs_the_main_query_once_and_says_so(tmp_path):
    arx = Fake("arxiv", [paper(1, "en", source="arxiv")], {"years"})
    oa = Fake("openalex", {"en": [paper(2, "en")], "fr": [paper(3, "fr")]})
    res, _, _ = run_api(tmp_path, [oa, arx], {
        "languages": ["en", "fr"], "queries_by_language": {"fr": "chaleur"}},
        libraries=["openalex", "arxiv"])
    summary = res.json()["summary"]
    assert arx.calls == [(("en", "fr"), "main")]
    assert oa.calls == [(("en",), "main"), (("fr",), "chaleur")]
    assert "languages" in summary["unapplied_filters"]["arxiv"]
    assert summary["language_queries"]["run_per_language"] == ["openalex"]
    assert any("arxiv cannot filter by language" in n
               for n in summary["language_queries"]["notes"])


def test_duplicates_across_language_queries_are_removed_once(tmp_path):
    same = paper(1, "en")
    again = paper(1, "en")  # same DOI returned by the second language's query
    lib = Fake("openalex", {"en": [same, paper(2, "en")], "fr": [again, paper(3, "fr")]})
    res, detail, _ = run_api(tmp_path, [lib], {
        "languages": ["en", "fr"], "queries_by_language": {"fr": "chaleur"}})
    summary = res.json()["summary"]
    assert summary["duplicates"] == 1 and summary["found"] == 3
    assert sorted(p["doi"] for p in detail["papers"]) == ["10.1/1", "10.1/2", "10.1/3"]


def test_limit_counts_the_combined_result_and_languages_share_it(tmp_path):
    lib = Fake("openalex", {"en": [paper(i, "en") for i in range(1, 9)],
                            "fr": [paper(i, "fr") for i in range(11, 19)]})
    res, detail, _ = run_api(tmp_path, [lib], {
        "languages": ["en", "fr"], "queries_by_language": {"fr": "chaleur"}}, max_results=4)
    summary = res.json()["summary"]
    assert summary["found"] == 4 and summary["stopped"] == "limit"
    # Round-robin: a full first language cannot starve the second one.
    assert summary["by_language"] == {"en": 2, "fr": 2}


def test_year_quotas_count_the_combined_result(tmp_path):
    lib = Fake("openalex", {"en": [paper(i, "en") for i in range(1, 6)],
                            "fr": [paper(i, "fr") for i in range(11, 16)]})
    res, _, _ = run_api(tmp_path, [lib], {
        "languages": ["en", "fr"], "queries_by_language": {"fr": "chaleur"}},
        extra={"quota_total": 3})
    summary = res.json()["summary"]
    assert summary["found"] == 3 and summary["shortfalls"] == {}


def test_by_language_lists_unknown_and_chosen_languages(tmp_path):
    lib = Fake("openalex", [paper(1, "en"), paper(2, ""), paper(3, "ja")])
    res, detail, _ = run_api(tmp_path, [lib], {"languages": ["en", "fr"]})
    assert res.json()["summary"]["by_language"] == {"en": 1, "unknown": 1, "ja": 1}
    by_key = {p["language_key"]: p["language_name"] for p in detail["papers"]}
    assert by_key == {"en": "English", "unknown": "Unknown language", "ja": "Japanese"}


def test_dedupe_merges_language_from_the_duplicate():
    a, b = paper(1, ""), paper(1, "ar")
    assert collect([a, b]).papers[0].language == "ar"


# ------------------------------------------------------------------ export
def test_export_has_a_language_name_column():
    keys = [k for k, _, _ in COLUMNS]
    assert keys.index("language_name") == keys.index("language") + 1
    rows = [{"title": "t", "language": "ar", "language_name": "Arabic"},
            {"title": "u", "language": ""}]
    out = list(csv.DictReader(io.StringIO(to_csv(rows, {}).decode("utf-8-sig"))))
    assert (out[0]["Language code"], out[0]["Language"]) == ("ar", "Arabic")
    assert (out[1]["Language code"], out[1]["Language"]) == ("", "Unknown language")


def test_export_through_the_api_includes_names_and_queries(tmp_path):
    lib = Fake("openalex", {"en": [paper(1, "en")], "ar": [paper(2, "ar")]})
    res, _, client = run_api(tmp_path, [lib], {
        "languages": ["en", "ar"], "queries_by_language": {"ar": "حرارة"}})
    run_id = res.json()["id"]
    out = client.post(f"/api/runs/{run_id}/export", json={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(out.content.decode("utf-8-sig"))))
    assert sorted(r["Language"] for r in rows) == ["Arabic", "English"]


# ------------------------------------------------------------------ normalisation and tokens
def test_arabic_normalisation_strips_marks_and_unifies_alef():
    assert normalize_arabic("أَحْمَد") == normalize_arabic("احمد") == "احمد"
    assert normalize_arabic("إبراهيم آمن ٱلله") == "ابراهيم امن الله"
    assert normalize_arabic("العـــربية") == "العربية"
    # Letters that can change a word are left alone.
    assert normalize_arabic("مدرسة") == "مدرسة" and normalize_arabic("على") == "على"


def test_latin_accents_fold_for_french_german_spanish():
    assert normalize_text("Élève à l'école") == "eleve a l'ecole"
    assert normalize_text("Über Wärmeübertragung STRASSE straße") == (
        "uber warmeubertragung strasse strasse")
    assert normalize_text("Corazón, niño") == "corazon, nino"
    assert tokenize("Élève, ÉLÈVE") == ["eleve", "eleve"]


def test_chinese_tokenization_uses_character_pairs():
    assert cjk_bigrams("机器学习") == ["机器", "器学", "学习"]
    assert cjk_bigrams("热") == ["热"]
    assert tokenize("机器学习 and AI 2024") == ["机器", "器学", "学习", "and", "ai", "2024"]
    assert tokenize("中子 transport 输运") == ["中子", "transport", "输运"]
    assert tokenize("") == [] and tokenize(None) == []


def test_fts_query_quotes_tokens_and_never_injects_operators():
    assert fts_query("机器学习") == '"机器" AND "器学" AND "学习"'
    assert fts_query('Élève OR "x"') == '"eleve" AND "or" AND "x"'
    assert fts_query("   ") == ""


def test_normalised_index_matches_in_the_real_fts5_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE s USING fts5(id UNINDEXED, text)")
    docs = {
        "ar": "دراسةٌ عن أَحْمَد، إبراهيم",
        "zh": "基于机器学习的中子输运方法",
        "fr": "Étude de l'élève à l'école",
        "de": "Wärmeübertragung in Kühlsystemen",
        "es": "Educación del corazón en niños",
    }
    for key, text in docs.items():
        conn.execute("INSERT INTO s VALUES(?,?)", (key, index_text(text)))

    def hit(query):
        return [r[0] for r in conn.execute(
            "SELECT id FROM s WHERE s MATCH ?", (fts_query(query),))]

    assert hit("احمد") == ["ar"] and hit("ابراهيم") == ["ar"]
    assert hit("机器学习") == ["zh"] and hit("中子") == ["zh"]
    assert hit("学习机器") == []          # pairs must occur together, in order
    assert hit("eleve ecole") == ["fr"] and hit("ÉLÈVE") == ["fr"]
    assert hit("warmeubertragung") == ["de"] and hit("kuhlsystemen") == ["de"]
    assert hit("educacion niños") == ["es"]


def test_stored_paper_text_is_unchanged_but_the_search_table_is_normalised(tmp_path):
    lib = Fake("openalex", [Paper(source="openalex", id="W1", doi="10.1/a", title="Étude 机器学习",
                                  abstract="أَحْمَد", year=2020, language="fr",
                                  category="peer_reviewed")])
    _, detail, client = run_api(tmp_path, [lib], {"languages": ["fr"]})
    assert detail["papers"][0]["title"] == "Étude 机器学习"
    assert detail["papers"][0]["abstract"] == "أَحْمَد"
    conn = sqlite3.connect(tmp_path / "t.sqlite")
    row = conn.execute("SELECT title, abstract FROM paper_search WHERE id='W1'").fetchone()
    assert row == ("etude 机器 器学 学习", "احمد")
    hit = conn.execute("SELECT id FROM paper_search WHERE paper_search MATCH ?",
                       (fts_query("étude 学习"),)).fetchall()
    assert hit == [("W1",)]


# ------------------------------------------------------------------ client logic (node)
NODE = shutil.which("node")


def pure_block():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return page[page.index("/* pure:start"):page.index("/* pure:end */")]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_client_language_logic_runs_under_node(tmp_path):
    script = pure_block() + """
const assert = require('assert');
assert.deepStrictEqual(UI_LANGS, ['en', 'ar', 'fr', 'zh', 'de', 'es']);
assert.strictEqual(pickUiLanguage('fr', 'de-DE'), 'fr');          // saved choice wins
assert.strictEqual(pickUiLanguage(null, 'ar-EG'), 'ar');          // browser language, one of the six
assert.strictEqual(pickUiLanguage(null, 'zh_CN'), 'zh');
assert.strictEqual(pickUiLanguage(null, 'ja-JP'), 'en');          // not one of the six -> en
assert.strictEqual(pickUiLanguage('ja', 'ja'), 'en');             // a saved unsupported code is ignored
assert.strictEqual(pickUiLanguage(undefined, undefined), 'en');
assert.strictEqual(dirOfLanguage('ar'), 'rtl');
for (const c of ['en', 'fr', 'zh', 'de', 'es']) assert.strictEqual(dirOfLanguage(c), 'ltr');
assert.strictEqual(fmt('{n} papers (run {id})', { n: 3, id: 7 }), '3 papers (run 7)');
assert.strictEqual(fmt('{missing} stays', {}), '{missing} stays');
assert.strictEqual(fmt('{n}', { n: 0 }), '0');
const P = (language, language_key) => ({ language, language_key });
const papers = [P('en'), P('ar'), P('ar'), P(''), P('ja'), P(undefined, 'unknown')];
assert.deepStrictEqual(countByLanguage(papers, ['en', 'ar', 'zh']),
  { en: 1, ar: 2, zh: 0, unknown: 2, ja: 1 });                   // chosen zh listed at zero
assert.deepStrictEqual(languageChipOrder(countByLanguage(papers, ['ar', 'en', 'zh']), ['ar', 'en', 'zh']),
  ['ar', 'en', 'zh', 'ja', 'unknown']);
assert.deepStrictEqual(languageChipOrder(countByLanguage([], []), []), ['unknown']);
assert.strictEqual(filterByLanguages(papers, []).length, 6);
assert.strictEqual(filterByLanguages(papers, ['ar']).length, 2);
assert.strictEqual(filterByLanguages(papers, ['unknown']).length, 2);
assert.strictEqual(filterByLanguages(papers, ['zh']).length, 0);
assert.deepStrictEqual(syncLangQueries(['en', 'ar'], 'heat', {}, {}), { en: 'heat', ar: 'heat' });
assert.deepStrictEqual(syncLangQueries(['en', 'ar'], 'heat 2', { en: 'heat', ar: 'حرارة' }, { ar: true }),
  { en: 'heat 2', ar: 'حرارة' });                                 // edited box keeps its text
assert.deepStrictEqual(syncLangQueries(['fr'], 'x', { en: 'a' }, { en: true }), { fr: 'x' });
assert.deepStrictEqual(queriesForRequest(['en'], { en: 'x' }), {}); // one language: no per-language text
assert.deepStrictEqual(queriesForRequest(['en', 'ar'], { en: ' heat ', ar: '  ' }), { en: 'heat' });
"""
    path = tmp_path / "lang.js"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_page_sets_direction_and_uses_logical_css():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    css = page[page.index("<style>"):page.index("</style>")]
    assert "setAttribute('dir', dirOfLanguage(code))" in page
    assert 'id="uiLang"' in page and "bayan.lang" in page
    for physical in ("margin-left", "margin-right", "padding-left", "padding-right",
                     "text-align:left", "text-align:right", "float:left", "float:right"):
        assert physical not in css, physical
    assert "unicode-bidi:isolate" in css
    assert "@import" not in css and "url(http" not in css      # system fonts only
    assert "dir: 'auto'" in page                                # per-element paper text direction
    assert not re.search(r"<link[^>]+fonts", page)
