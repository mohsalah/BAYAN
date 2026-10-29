"""M8: reader agent, evidence table and the quote verifier. No live model calls."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_accounts_search import FakeConnector, register, search
from test_downloads import Connector, Net, make_fetcher, paper, pdf_resp
from test_pages_resume import THREE, make_pages_pdf

from bayan.api import create_app, reader_key_ok
from bayan.config import Settings
from bayan.db import connect, init_db
from bayan.reader import (
    OUTPUT_SCHEMA,
    AnthropicReader,
    ReaderRefused,
    ReaderReply,
    Source,
    build_prompt,
    check_items,
    chunks,
    verify_quote,
)

METHOD_Q = "Monte Carlo simulation with continuous energy cross sections."
RESULT_Q = "The multiplication factor agrees within 200 pcm of the benchmark."


class FakeReader:
    """Returns scripted answers and records every prompt it was sent."""

    model = "fake-model"

    def __init__(self, answer=None, refuse=False):
        self.answer = answer or {"items": [], "not_reported": []}
        self.refuse, self.prompts = refuse, []

    def extract(self, system, prompt):
        self.prompts.append(prompt)
        if self.refuse:
            raise ReaderRefused("The model declined to read this text.")
        data = self.answer(prompt) if callable(self.answer) else self.answer
        return ReaderReply(json.loads(json.dumps(data)), self.model, 1000, 200)


def item(dimension, quote, page, statement="A statement."):
    return {"dimension": dimension, "statement": statement, "quote": quote, "page": page}


ANSWER = {
    "items": [
        item("methods", METHOD_Q, 2, "Continuous-energy Monte Carlo."),
        item("validation", "The code was validated against forty critical experiments.", 3),
        item("validation", RESULT_Q, 1),  # real text, wrong page
        item("limitations", RESULT_Q, 9),  # page does not exist
    ],
    "not_reported": ["data"],
}


@pytest.fixture
def env(tmp_path):
    made = []

    def build(papers, reader="default", **extra):
        net = Net()
        settings = Settings(database_path=tmp_path / f"t{len(made)}.sqlite",
                            pdf_root=tmp_path / f"pdfs{len(made)}", search_max_results=100,
                            searches_per_hour=50, job_resume_interval=0, **extra)
        fake = FakeReader(ANSWER) if reader == "default" else reader
        app = create_app(settings, connector_factory=lambda lib: Connector(papers),
                         fetcher=make_fetcher(net), reader=fake)
        client = TestClient(app)
        client.__enter__()
        made.append(client)
        register(client)
        run_id = search(client).json()["id"]
        return type("Env", (), {"client": client, "net": net, "reader": fake, "app": app,
                                "run_id": run_id, "settings": settings})

    yield build
    for c in made:
        c.__exit__(None, None, None)


def download(e, pid="W1", data=THREE):
    e.net.routes[f"https://oa.example/{pid}.pdf"] = lambda r: pdf_resp(data)
    body = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": pid}]}
    assert e.client.post("/api/downloads", json=body).status_code == 202


def read(e, *ids):
    body = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": i} for i in ids]}
    r = e.client.post("/api/reader", json=body)
    assert r.status_code == 202, r.text
    job = e.client.get(f"/api/reader/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    return {p["id"]: p for p in job["papers"]}


def evidence(e, **params):
    return e.client.get("/api/evidence", params={"run_id": e.run_id, **params}).json()


# ---------------------------------------------------------------- verifier

PAGE = ("Results\nThe multiplication factor agrees with-\nin 200 pcm of the bench-\nmark, "
        "and the “reference” value is 1.000 ± 0.001 for the ﬁrst core.")


@pytest.mark.parametrize("quote", [
    "The multiplication factor agrees within 200 pcm of the benchmark",  # hyphenated line ends
    'the "reference" value is 1.000 ± 0.001 for the first core.',  # quotes and ligature
    "agrees with- in 200 pcm",  # as printed, with the line break as a space
])
def test_verifier_accepts_layout_differences(quote):
    assert verify_quote(quote, PAGE) == ""


@pytest.mark.parametrize("quote, reason", [
    ("The multiplication factor agrees within 300 pcm of the benchmark", "quote_not_on_page"),
    ("THE MULTIPLICATION FACTOR AGREES", "quote_not_on_page"),  # case is not ignored
    ("The multiplication factor ... benchmark", "quote_has_ellipsis"),
    ("200 pcm", "quote_too_short"),
    ("x" * 601, "quote_too_long"),
])
def test_verifier_rejects_changed_or_shortened_quotes(quote, reason):
    assert verify_quote(quote, PAGE) == reason


def test_check_items_needs_the_cited_page():
    src = Source("full_text", pages={2: ("2", METHOD_Q)})
    got = check_items(src, {"items": [item("methods", METHOD_Q, 2), item("methods", METHOD_Q, 3),
                                      item("bogus", METHOD_Q, 2), item("data", METHOD_Q, "2"),
                                      item("data", METHOD_Q, 2, statement=" ")]})
    assert [g["reject_reason"] for g in got] == [None, "page_not_found", "unknown_dimension",
                                                 "page_not_found", "empty_statement"]


def test_chunks_keep_page_markers_and_split_long_papers():
    src = Source("full_text", pages={1: ("i", "a" * 50), 2: ("ii", "b" * 50), 3: ("1", "c" * 50)})
    assert chunks(src) == ["[[page 1]]\n" + "a" * 50 + "\n[[page 2]]\n" + "b" * 50
                           + "\n[[page 3]]\n" + "c" * 50 + "\n"]
    parts = chunks(src, limit=130)
    assert len(parts) == 2 and parts[0].startswith("[[page 1]]") and "[[page 3]]" in parts[1]
    prompt = build_prompt("T", parts[1], 2, 2)
    assert "part 2 of 2" in prompt and prompt.rstrip().endswith("</paper>")


# ---------------------------------------------------------------- end to end

def test_read_stores_only_verified_quotes_with_pages(env):
    e = env([paper(1, oa_url="https://oa.example/W1.pdf")])
    download(e)
    got = read(e, "W1")["W1"]
    assert got["status"] == "done" and got["level"] == "full_text"
    assert (got["verified"], got["rejected"]) == (1, 3)
    assert got["dimensions"] == {"methods": "reported", "data": "not_reported",
                                 "validation": "no_verified_evidence",
                                 "limitations": "no_verified_evidence"}
    prompt = e.reader.prompts[0]
    assert "[[page 2]]" in prompt and METHOD_Q in prompt and "<paper>" in prompt
    shown = evidence(e)["papers"][0]
    assert [(x["dimension"], x["page"], x["page_label"], x["level"]) for x in shown["evidence"]] \
        == [("methods", 2, "2", "full_text")]
    assert shown["evidence"][0]["quote"] == METHOD_Q and shown["evidence"][0]["verified"]
    every = evidence(e, include_rejected="true")["papers"][0]["evidence"]
    reasons = sorted(x["reject_reason"] or "" for x in every)
    assert reasons == ["", "page_not_found", "quote_not_on_page", "quote_not_on_page"]
    assert all(x["reject_text"] for x in every if not x["verified"])


def test_second_reading_uses_the_cache_and_meters_usage_once(env):
    e = env([paper(1, oa_url="https://oa.example/W1.pdf")])
    download(e)
    read(e, "W1")
    again = read(e, "W1")["W1"]
    assert len(e.reader.prompts) == 1 and again["cached_parts"] == 1
    assert len(evidence(e, include_rejected="true")["papers"][0]["evidence"]) == 4  # replaced
    with connect(e.settings.database_path) as conn:
        rows = conn.execute("SELECT model, input_tokens, output_tokens FROM usage").fetchall()
    assert [tuple(r) for r in rows] == [("fake-model", 1000, 200)]


def test_abstract_only_paper_is_labelled_abstract(env):
    p = paper(1)
    p.abstract = "We measured the void reactivity coefficient of the lattice at three densities."
    e = env([p], reader=FakeReader({"items": [item("data", p.abstract[:60], 0)],
                                    "not_reported": []}))
    got = read(e, "W1")["W1"]
    assert got["status"] == "done" and got["level"] == "abstract"
    assert "(abstract only)" in e.reader.prompts[0]
    x = evidence(e)["papers"][0]["evidence"][0]
    assert x["level"] == "abstract" and x["page"] is None and x["verified"]


def test_nothing_to_read_is_not_reviewed_and_no_call_is_made(env):
    e = env([paper(1)])
    got = read(e, "W1")["W1"]
    assert got["status"] == "not_reviewed" and "abstract" in got["detail"]
    assert e.reader.prompts == []


def test_too_long_document_is_not_reviewed_not_partly_read(env, monkeypatch):
    from bayan import reader

    monkeypatch.setattr(reader, "MAX_CHARS_PER_CALL", 80)
    monkeypatch.setattr(reader, "MAX_PARTS", 2)
    e = env([paper(1, oa_url="https://oa.example/W1.pdf")])
    download(e)
    got = read(e, "W1")["W1"]
    assert got["status"] == "not_reviewed" and "too long" in got["detail"]
    assert e.reader.prompts == []


def test_boolean_page_is_not_page_one():
    src = Source("full_text", pages={1: ("1", METHOD_Q)})
    assert check_items(src, {"items": [item("methods", METHOD_Q, True)]})[0]["reject_reason"] \
        == "page_not_found"


def test_scan_without_abstract_says_ocr_is_needed(env):
    e = env([paper(1, oa_url="https://oa.example/W1.pdf")])
    download(e, data=make_pages_pdf([[]]))
    got = read(e, "W1")["W1"]
    assert got["status"] == "not_reviewed" and "OCR" in got["detail"]


def test_refusal_is_recorded_and_leaves_no_evidence(env):
    e = env([paper(1, oa_url="https://oa.example/W1.pdf")])
    download(e)
    read(e, "W1")
    e.reader.refuse = True
    with connect(e.settings.database_path) as conn:
        conn.execute("DELETE FROM reader_cache")
        conn.commit()
    got = read(e, "W1")["W1"]
    assert got["status"] == "refused" and got["dimensions"] is None
    assert evidence(e, include_rejected="true")["papers"][0]["evidence"] == []


def test_reader_not_configured_answers_503(env):
    e = env([paper(1)], reader=None)
    r = e.client.post("/api/reader", json={"run_id": e.run_id,
                                           "papers": [{"source": "openalex", "id": "W1"}]})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert not reader_key_ok("your-key-here") and not reader_key_ok("") and not reader_key_ok(None)
    assert reader_key_ok("sk-ant-api03-abc")


def test_readings_are_private_and_rate_limited(env):
    e = env([paper(1), paper(2)], readings_per_hour=1)
    read(e, "W1")
    body = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": "W2"}]}
    assert e.client.post("/api/reader", json=body).status_code == 429
    job_id = 1
    with TestClient(e.app) as other:
        register(other, "other@example.org")
        assert other.get(f"/api/reader/jobs/{job_id}").status_code == 404
        assert other.get("/api/evidence", params={"run_id": e.run_id}).status_code == 404
        assert other.post("/api/reader", json=body).status_code == 404
    bad = {"run_id": e.run_id, "papers": [{"source": "openalex", "id": "W99"}]}
    assert e.client.post("/api/reader", json=bad).status_code == 404


# ---------------------------------------------------------------- the real SDK path, mocked HTTP

def sse(answer, stop_reason="end_turn"):
    text = json.dumps(answer)
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1234, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 56}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return httpx2.Response(200, content=body.encode(),
                          headers={"content-type": "text/event-stream"})


def test_anthropic_reader_request_and_response(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return sse(ANSWER)

    client = AnthropicReader("sk-ant-test", "claude-sonnet-5-5", "high",
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    reply = client.extract("SYSTEM", "PROMPT")
    assert reply.data == ANSWER and (reply.input_tokens, reply.output_tokens) == (1234, 56)
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-sonnet-5-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"} and body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema",
                                                                 "schema": OUTPUT_SCHEMA}}
    assert body["system"] == "SYSTEM" and body["messages"][0]["content"] == "PROMPT"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert seen[0].headers["authorization"] == "Bearer sk-ant-test"


@pytest.mark.parametrize("stop, error", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_anthropic_reader_stop_reasons(stop, error):
    client = AnthropicReader("sk-ant-test", "m", http_client=httpx2.Client(
        transport=httpx2.MockTransport(lambda r: sse(ANSWER, stop))))
    with pytest.raises(Exception, match=error):
        client.extract("S", "P")


def test_anthropic_reader_http_error_never_shows_the_key():
    client = AnthropicReader("sk-ant-secret-123", "m", http_client=httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(401, json={
            "type": "error", "error": {"type": "authentication_error", "message": "bad"}}))))
    with pytest.raises(Exception) as info:
        client.extract("S", "P")
    assert "HTTP 401" in str(info.value) and "secret" not in str(info.value)


def test_schema_version_tables_exist(tmp_path):
    init_db(tmp_path / "x.sqlite")
    with connect(tmp_path / "x.sqlite") as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"evidence", "reader_results", "reader_cache", "reader_attempts"} <= names


_ = FakeConnector  # imported for parity with the other API tests
