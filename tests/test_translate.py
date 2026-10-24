"""M24: translator. No live services: a fake engine stands in for the real one."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accounts_search import FakeConnector, register, search
from test_downloads import GOOD, TITLE, paper

from bayan import translate
from bayan.analysis import build_packet
from bayan.api import create_app
from bayan.config import Settings
from bayan.translate import (
    LibreTranslateEngine,
    NotConfigured,
    TranslateError,
    parse_pages,
    protect,
    restore,
    split_segments,
    translate_text,
)

WORDS = {"le": "the", "flux": "flux", "est": "is", "de": "of", "à": "at", "mesuré": "measured",
         "énergie": "energy", "la": "the", "avec": "with", "une": "a", "section": "section",
         "efficace": "cross-section", "uranium": "uranium"}


class FakeEngine:
    """Translates a few words and *damages* anything that is not a placeholder.

    Digits become '#', letters of unknown words are upper-cased: any number, unit or equation
    that was not protected would come back corrupted.
    """

    name = "fake"

    def __init__(self):
        self.calls = []

    def translate(self, text, source, target):
        self.calls.append((text, source, target))
        out = []
        for part in re.split(r"(XQ\d+X)", text):
            if re.fullmatch(r"XQ\d+X", part):
                out.append(part)
                continue
            words = [WORDS.get(w, w.upper()) for w in part.split(" ")]
            out.append(re.sub(r"\d", "#", " ".join(words)))
        return "".join(out)


class DroppingEngine:
    """Loses every placeholder, like a poor engine would."""

    name = "dropper"

    def translate(self, text, source, target):
        return re.sub(r"XQ\d+X", "", text)


SENTENCE = ("le flux est mesuré à 3,5 MeV avec U-235 et UO2 ($k_{eff} = 1.002$) à 25 °C "
            "[4] (Dupont et al., 2019) doi:10.1016/j.anucene.2020.107654")


# ------------------------------------------------------------------ placeholders

def test_numbers_units_equations_nuclides_dois_and_citations_are_preserved():
    result = translate_text(FakeEngine(), SENTENCE, "fr", "en")
    out = result.text
    for kept in ("3,5 MeV", "U-235", "UO2", "$k_{eff} = 1.002$", "25 °C", "[4]",
                 "(Dupont et al., 2019)", "10.1016/j.anucene.2020.107654"):
        assert kept in out, kept
    assert "#" not in out  # the fake engine's damage never reached protected text
    assert "THE" not in out and out.startswith("the flux is measured at")
    assert result.warnings == []


def test_protect_then_restore_is_exact_without_an_engine():
    text = ("Spectrum of 235U and Pu-239m at 10^-3 cm^-2 s^-1, E = mc^2, 5 mm/s, 0.5%, "
            "H2O, NaCl, ±0.003, 1e-5, https://example.org/a?b=1, 2020-2021 α β ∑")
    protected = protect(text)
    assert "235" not in protected.text and "mc^2" not in protected.text
    restored, lost = restore(protected.text, protected.originals)
    assert restored == text and lost == 0


def test_ordinary_words_are_not_protected():
    protected = protect("The method is simple and works in this case.")
    assert protected.originals == []


def test_literal_placeholder_like_text_in_source_survives():
    protected = protect("keep XQ7X as written")
    assert restore(protected.text, protected.originals)[0] == "keep XQ7X as written"


def test_lost_placeholders_are_flagged_not_hidden():
    result = translate_text(DroppingEngine(), "mesure 3,5 MeV", "fr", "en")
    assert result.warnings and "Low confidence" in result.warnings[0]


def test_long_text_is_split_and_paragraphs_kept():
    text = "Premier paragraphe.\n\n" + ("Une phrase assez longue. " * 200)
    parts = split_segments(text, 500)
    assert all(len(p) <= 500 for p in parts) and parts[1] == ""
    result = translate_text(FakeEngine(), text, "fr", "en")
    assert len(result.segments) == len(split_segments(text))


# ------------------------------------------------------------------ target restriction

@pytest.mark.parametrize("target", ["fr", "zh", "de", "es", "ja", "", "EN"])
def test_only_english_or_arabic_targets(target):
    with pytest.raises(TranslateError):
        translate_text(FakeEngine(), "texte", "fr", target)


@pytest.mark.parametrize("source", ["en", "ar", "fr", "zh", "de", "es"])
def test_all_six_sources_accepted(source):
    target = "ar" if source == "en" else "en"
    assert translate_text(FakeEngine(), "texte", source, target).target_language == target


def test_unsupported_source_and_same_language_rejected():
    for source in ("ja", "pt", "xx"):
        with pytest.raises(TranslateError):
            translate_text(FakeEngine(), "x", source, "en")
    with pytest.raises(TranslateError):
        translate_text(FakeEngine(), "x", "en", "en")


def test_no_engine_is_a_clear_not_configured_error():
    with pytest.raises(NotConfigured, match="not configured"):
        translate_text(None, "texte", "fr", "en")


def test_libretranslate_engine_parses_response_and_hides_key_on_failure():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"translatedText": "hello"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = LibreTranslateEngine("http://mt.local/", "SECRET-KEY", client)
    assert engine.translate("bonjour", "fr", "en") == "hello"
    assert b"SECRET-KEY" in seen["body"]  # sent to the engine, but nowhere else
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(translate.EngineError) as err:
        LibreTranslateEngine("http://mt.local", "SECRET-KEY", bad).translate("x", "fr", "en")
    assert "SECRET-KEY" not in str(err.value)


def test_detect_language():
    assert translate.detect_language("مرحبا بالعالم كيف حالك") == "ar"
    assert translate.detect_language("中子输运在快堆中的应用") == "zh"
    assert translate.detect_language("Das ist nicht der Fall und die Ergebnisse sind klar") == "de"
    assert translate.detect_language("12345") is None


def test_parse_pages():
    assert parse_pages("1-3,5", 5) == [1, 2, 3, 5]
    assert parse_pages(None, 3) == [1, 2, 3]
    for bad in ("0", "4", "a", "3-1"):
        with pytest.raises(TranslateError):
            parse_pages(bad, 3)


# ------------------------------------------------------------------ API

@pytest.fixture
def env(tmp_path):
    fake = FakeConnector([paper(0)])
    settings = Settings(database_path=tmp_path / "t.sqlite", pdf_root=tmp_path / "pdfs",
                        search_max_results=50, searches_per_hour=50, translate_max_chars=400,
                        translations_per_hour=6)
    engine = FakeEngine()
    app = create_app(settings, connector_factory=lambda lib: fake, translator=engine)
    with TestClient(app) as client:
        register(client)
        client.engine, client.run_id, client.app_settings = engine, search(client).json()["id"], \
            settings
        yield client


def tr(client, text="le flux est mesuré à 3,5 MeV", **extra):
    return client.post("/api/translate", json={"text": text, "source_language": "fr",
                                               "target_language": "en", **extra})


def test_endpoints_need_login(env):
    anon = TestClient(env.app)
    for method, url in (("get", "/api/translate/status"), ("post", "/api/translate"),
                        ("post", "/api/translate/paper"), ("post", "/api/translate/pdf"),
                        ("get", "/api/translate/glossary"), ("put", "/api/translate/glossary"),
                        ("delete", "/api/translate/glossary")):
        assert getattr(anon, method)(url).status_code == 401, url


def test_translation_is_labelled_machine_translated_and_never_evidence(env):
    r = tr(env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "machine translated" and body["machine_translated"] is True
    assert body["evidence_quote"] is False
    assert (body["source_language"], body["target_language"], body["engine"]) == \
        ("fr", "en", "fake")
    assert body["segments"][0]["original"] == "le flux est mesuré à 3,5 MeV"
    assert body["text"] == "the flux is measured at 3,5 MeV"


def test_api_rejects_other_targets_and_sources(env):
    assert tr(env, target_language="de").status_code == 422
    r = env.post("/api/translate", json={"text": "x", "source_language": "ja",
                                         "target_language": "en"})
    assert r.status_code == 422


def test_auto_detect_and_undetectable(env):
    ok = env.post("/api/translate", json={
        "text": "Das ist nicht der Fall und die Ergebnisse sind klar", "target_language": "en"})
    assert ok.json()["source_language"] == "de"
    assert env.post("/api/translate", json={"text": "12345", "target_language": "en"}
                    ).status_code == 422


def test_size_and_rate_limits_per_user(env):
    assert tr(env, text="x" * 401).status_code == 422
    for _ in range(6):
        assert tr(env).status_code == 200
    assert tr(env).status_code == 429
    other = TestClient(env.app)
    register(other, "bob@example.org")
    assert tr(other).status_code == 200  # limits are per user


def test_not_configured_status_and_error(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"))
    with TestClient(app) as client:
        register(client)
        status = client.get("/api/translate/status").json()
        assert status["status"] == "not_configured" and "not configured" in status["message"]
        assert status["targets"] == ["en", "ar"]
        r = tr(client)
        assert r.status_code == 503 and "not configured" in r.json()["detail"].lower()


def test_status_reports_engine_and_ocr(env):
    s = env.get("/api/translate/status").json()
    assert s["status"] == "ready" and s["engine"] == "fake"
    assert set(s["ocr"]) == {"available", "message"}


def test_settings_repr_hides_translate_key():
    s = Settings(database_path="x", translate_api_key="SECRET-KEY")
    assert "SECRET-KEY" not in repr(s)


def test_paper_title_and_abstract_translated_in_place_never_stored(env):
    r = env.post("/api/translate/paper", json={"source": "openalex", "id": "W0",
                                               "source_language": "fr", "target_language": "ar"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"]["machine_translated"] is True
    assert body["title"]["segments"][0]["original"] == TITLE
    assert body["title"]["target_language"] == "ar"
    assert body["abstract"] is None  # this fixture paper has no abstract
    # The stored paper is untouched and the analysis packet carries only original text.
    runs = env.get(f"/api/runs/{env.run_id}").json()
    assert runs["papers"][0]["title"] == TITLE
    packet = env.post("/api/analysis/gaps", json={
        "papers": [{"source": "openalex", "id": "W0"}], "run_id": env.run_id}).json()
    assert TITLE in str(packet) and TITLE.upper() not in str(packet)
    assert "machine translated" not in str(packet).lower()


def test_paper_not_in_users_runs_is_404(env):
    other = TestClient(env.app)
    register(other, "bob@example.org")
    r = other.post("/api/translate/paper", json={"source": "openalex", "id": "W0",
                                                 "target_language": "en"})
    assert r.status_code == 404


def test_build_packet_has_no_translation_fields():
    import inspect
    assert "translat" not in inspect.getsource(build_packet).lower()


# ------------------------------------------------------------------ glossary

def put_term(client, source="uranium", target="اليورانيوم", s="fr", t="ar"):
    return client.put("/api/translate/glossary", json={
        "source_language": s, "target_language": t, "source_term": source, "target_term": target})


def test_glossary_is_applied_consistently(env):
    assert put_term(env, "section efficace", "neutron cross section", "fr", "en").status_code == 200
    r = tr(env, text="la section efficace de la Section Efficace")
    body = r.json()
    assert body["text"].count("neutron cross section") == 2
    assert body["glossary_applied"] == {"section efficace": "neutron cross section"}


def test_glossary_change_invalidates_cache(env):
    first = tr(env, text="le flux d'uranium")
    assert first.json()["cached"] is False and tr(env, text="le flux d'uranium").json()["cached"]
    put_term(env, "uranium", "U metal", "fr", "en")
    again = tr(env, text="le flux d'uranium").json()
    assert again["cached"] is False and "U metal" in again["text"]


def test_glossary_update_list_delete_and_validation(env):
    put_term(env, "flux", "flow", "fr", "en")
    put_term(env, "FLUX", "fluence", "fr", "en")  # same term (case-insensitive): updated
    items = env.get("/api/translate/glossary").json()
    assert items == [{"source_language": "fr", "target_language": "en",
                      "source_term": "FLUX", "target_term": "fluence"}]
    assert put_term(env, "x", "y", "fr", "de").status_code == 422  # target restriction
    assert put_term(env, "", "y", "fr", "en").status_code == 422
    assert put_term(env, "x" * 101, "y", "fr", "en").status_code == 422
    gone = env.delete("/api/translate/glossary", params={
        "source_language": "fr", "target_language": "en", "source_term": "flux"})
    assert gone.status_code == 200 and gone.json() == []
    assert env.delete("/api/translate/glossary", params={
        "source_language": "fr", "target_language": "en", "source_term": "flux"}
    ).status_code == 404


def test_glossary_is_private_between_users(env):
    put_term(env, "flux", "SECRET-FLOW", "fr", "en")
    other = TestClient(env.app)
    register(other, "bob@example.org")
    assert other.get("/api/translate/glossary").json() == []
    assert "SECRET-FLOW" not in tr(other).json()["text"]
    assert other.delete("/api/translate/glossary", params={
        "source_language": "fr", "target_language": "en", "source_term": "flux"}
    ).status_code == 404
    assert env.get("/api/translate/glossary").json()[0]["target_term"] == "SECRET-FLOW"


def test_translation_cache_is_private_between_users(env):
    tr(env)
    other = TestClient(env.app)
    register(other, "bob@example.org")
    assert tr(other).json()["cached"] is False


# ------------------------------------------------------------------ PDF and OCR

def upload(client, data, pid="W1"):
    return client.post("/api/downloads/upload", params={"source": "openalex", "paper_id": pid},
                       content=data, headers={"content-type": "application/pdf"})


@pytest.fixture
def pdf_env(tmp_path):
    fake = FakeConnector([paper(1)])
    settings = Settings(database_path=tmp_path / "t.sqlite", pdf_root=tmp_path / "pdfs",
                        search_max_results=50, searches_per_hour=50, translations_per_hour=20)
    app = create_app(settings, connector_factory=lambda lib: fake, translator=FakeEngine())
    with TestClient(app) as client:
        register(client)
        search(client)
        yield client


def test_uploaded_pdf_pages_are_translated_side_by_side(pdf_env):
    assert upload(pdf_env, GOOD).status_code == 200
    r = pdf_env.post("/api/translate/pdf", json={
        "source": "openalex", "id": "W1", "source_language": "fr", "target_language": "en"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["machine_translated"] and body["evidence_quote"] is False
    page = body["pages"][0]
    assert page["page"] == 1 and page["origin"] == "pdf_text"
    assert TITLE in "\n".join(s["original"] for s in page["segments"])
    assert page["text"] and page["text"] != TITLE


def test_pdf_requires_own_validated_file_and_valid_pages(pdf_env, tmp_path):
    body = {"source": "openalex", "id": "W1", "source_language": "fr", "target_language": "en"}
    assert pdf_env.post("/api/translate/pdf", json=body).status_code == 404  # nothing uploaded
    upload(pdf_env, GOOD)
    assert pdf_env.post("/api/translate/pdf", json={**body, "pages": "9"}).status_code == 422
    assert pdf_env.post("/api/translate/pdf", json={**body, "target_language": "fr"}
                        ).status_code == 422
    other = TestClient(pdf_env.app)
    register(other, "bob@example.org")
    assert other.post("/api/translate/pdf", json=body).status_code == 404


def test_scanned_pdf_without_ocr_gives_clear_message(pdf_env, monkeypatch):
    monkeypatch.setattr("bayan.api.ocr_available", lambda: False)
    upload(pdf_env, GOOD)
    # Treat every page as image-only by raising the "scanned" threshold above the page text.
    monkeypatch.setattr("bayan.api.SCANNED_MIN_CHARS", 10**6)
    r = pdf_env.post("/api/translate/pdf", json={
        "source": "openalex", "id": "W1", "source_language": "fr", "target_language": "en"})
    assert r.status_code == 422 and "OCR is not installed" in r.json()["detail"]


def test_scanned_pdf_uses_ocr_when_available_and_marks_it(pdf_env, monkeypatch):
    monkeypatch.setattr("bayan.api.ocr_available", lambda: True)
    monkeypatch.setattr("bayan.api.ocr_page", lambda path, n, lang: "le flux est mesuré 42 MeV")
    monkeypatch.setattr("bayan.api.SCANNED_MIN_CHARS", 10**6)
    upload(pdf_env, GOOD)
    r = pdf_env.post("/api/translate/pdf", json={
        "source": "openalex", "id": "W1", "source_language": "fr", "target_language": "en"})
    assert r.status_code == 200, r.text
    page = r.json()["pages"][0]
    assert page["origin"] == "ocr" and "42 MeV" in page["text"]


def test_ocr_is_optional_not_a_dependency():
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    deps = " ".join(data["project"]["dependencies"]).lower()
    assert "tesseract" not in deps and "pdf2image" not in deps
    assert "ocr" in data["project"]["optional-dependencies"]
    assert translate.ocr_available() in (True, False)
