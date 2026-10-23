import hashlib
import threading
import time
from collections import defaultdict
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accounts_search import FakeConnector, register, search

from bayan import downloads
from bayan.api import create_app
from bayan.config import Settings
from bayan.db import connect
from bayan.downloads import (
    Fetcher,
    UnsafeURL,
    check_identity,
    check_public_url,
    finalize_pdf,
    pdf_path,
    pdf_sink,
    safe_name,
    validate_pdf,
)
from bayan.models import Paper

TITLE = "Neutron transport in fast reactors"
OTHER_BODY = [
    "Consumer attitudes towards sustainable packaging in retail supply chains",
    "We surveyed shoppers across several regions and compared stated preferences",
    "with observed purchasing behaviour over twelve months of loyalty card data.",
    "Results show a persistent gap between intention and action among respondents.",
]


def make_pdf(lines=()):
    """A minimal one-page PDF whose text pypdf can extract."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
        "/Resources << /Font << /F1 4 0 R >> >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


GOOD = make_pdf([TITLE, "A. Author", "Abstract. We study transport of neutrons."])


def pdf_resp(data=GOOD, **headers):
    return httpx.Response(200, content=data, headers={"content-type": "application/pdf", **headers})


def html_resp():
    return httpx.Response(200, content=b"<!DOCTYPE html><html><body>Verify you are human</body>",
                          headers={"content-type": "text/html"})


class Net:
    """Fake network: routes maps URL (no query) -> callable(request) -> Response."""

    def __init__(self):
        self.routes, self.requests = {}, []

    def __call__(self, request):
        self.requests.append(request)
        route = self.routes.get(str(request.url.copy_with(query=None)))
        return route(request) if route else httpx.Response(404)

    def urls(self):
        return [str(r.url) for r in self.requests]


def fake_resolver(host, port):
    if host == "localhost":
        return ["127.0.0.1"]
    if host.endswith(".internal"):
        return ["10.0.0.5"]
    return ["93.184.216.34"]


def make_fetcher(net, **kw):
    kw.setdefault("host_delay", 0)
    kw.setdefault("sleep", lambda s: None)
    return Fetcher(transport=httpx.MockTransport(net), resolver=fake_resolver, **kw)


def paper(i, oa_url="", doi=None, title=None):
    return Paper(source="openalex", id=f"W{i}", doi=f"10.1/{i}" if doi is None else doi,
                 title=title or f"{TITLE} {i}" if i else TITLE, year=2020,
                 native_type="article", category="peer_reviewed", language="en", oa_url=oa_url)


class Connector(FakeConnector):
    def lookup_doi(self, title, first_author, year):
        return None


class Env:
    def __init__(self, tmp_path, papers, **settings):
        self.net = Net()
        self.fetcher = make_fetcher(self.net)
        settings.setdefault("contact_email", "me@example.org")
        self.settings = Settings(database_path=tmp_path / "t.sqlite", pdf_root=tmp_path / "pdfs",
                                 search_max_results=100, searches_per_hour=50, **settings)
        fake = Connector(papers)
        self.app = create_app(self.settings, connector_factory=lambda lib: fake,
                              fetcher=self.fetcher)
        self.root = tmp_path / "pdfs"


@pytest.fixture
def make_env(tmp_path):
    clients = []

    def build(papers, **settings):
        env = Env(tmp_path / f"env{len(clients)}", papers, **settings)
        env.client = TestClient(env.app)
        env.client.__enter__()
        clients.append(env.client)
        register(env.client)
        env.run_id = search(env.client).json()["id"]
        return env

    yield build
    for c in clients:
        c.__exit__(None, None, None)


def start(env, *ids, client=None):
    client = client or env.client
    body = {"run_id": env.run_id, "papers": [{"source": "openalex", "id": i} for i in ids]}
    return client.post("/api/downloads", json=body)


def run_job(env, *ids):
    r = start(env, *ids)
    assert r.status_code == 202, r.text
    job = env.client.get(f"/api/downloads/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    return {p["id"]: p for p in job["papers"]}


def part_files(root):
    return list(Path(root).rglob("*.part"))


# ---------------------------------------------------------------- status paths

def test_open_url_verified_and_served_to_owner(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "downloaded_verified" and got["has_file"]
    assert got["origin"] == "oa_url" and got["http_status"] == 200
    assert got["size"] == len(GOOD) and got["sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert got["source_url"] == got["final_url"] == "https://oa.example/p1.pdf"
    assert got["retrieved_at"].endswith("Z")
    assert "path" not in got
    files = list(env.root.rglob("*.pdf"))
    assert len(files) == 1 and files[0].parts[-3] == "1" and files[0].name == "W1.pdf"
    assert not part_files(env.root)
    r = env.client.get("/api/downloads/file", params={"source": "openalex", "paper_id": "W1"})
    assert r.status_code == 200 and r.content == GOOD
    assert r.headers["content-type"] == "application/pdf"


def test_verified_by_doi_when_title_differs(make_env):
    data = make_pdf(["Some unrelated heading", "doi: 10.1/2"])
    env = make_env([paper(2, oa_url="https://oa.example/p2.pdf", title="A completely other name")])
    env.net.routes["https://oa.example/p2.pdf"] = lambda r: pdf_resp(data)
    got = run_job(env, "W2")["W2"]
    assert got["status"] == "downloaded_verified" and got["identity_basis"] == "doi"


def test_no_extractable_text_is_unverified_not_verified(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(make_pdf([]))
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "downloaded_unverified_identity" and got["has_file"]
    r = env.client.get("/api/downloads/file", params={"source": "openalex", "paper_id": "W1"})
    assert r.status_code == 200


def test_identity_mismatch_is_kept_separate_and_not_offered(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(make_pdf(OTHER_BODY))
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "identity_mismatch" and not got["has_file"]
    assert not pdf_path(env.root, 1, "openalex", "W1").exists()
    kept = list(env.root.rglob("*.pdf"))
    assert len(kept) == 1 and "_identity_mismatch" in kept[0].parts
    r = env.client.get("/api/downloads/file", params={"source": "openalex", "paper_id": "W1"})
    assert r.status_code == 404


def test_html_instead_of_pdf_is_rejected(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf", doi="")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: html_resp()
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "needs_access" and got["reason"] == "html_returned"
    assert not got["has_file"] and not list(env.root.rglob("*.pdf")) and not part_files(env.root)


def test_html_with_pdf_content_type_is_rejected(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf", doi="")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(b"<html>captcha</html>")
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "needs_access" and got["reason"] == "html_returned"


def test_oversize_rejected_by_streaming_and_by_content_length(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/a.pdf", doi=""),
                    paper(2, oa_url="https://oa.example/b.pdf", doi="")], pdf_max_bytes=1000)
    big = GOOD + b"0" * 5000
    env.net.routes["https://oa.example/a.pdf"] = lambda r: pdf_resp(big)
    env.net.routes["https://oa.example/b.pdf"] = lambda r: pdf_resp(GOOD, **{"content-length": "99999"})
    got = run_job(env, "W1", "W2")
    assert got["W1"]["reason"] == "too_large" and got["W2"]["reason"] == "too_large"
    assert got["W1"]["status"] == "needs_access"
    assert not list(env.root.rglob("*.pdf")) and not part_files(env.root)


def test_not_a_pdf_and_unparseable_pdf(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/a.pdf", doi=""),
                    paper(2, oa_url="https://oa.example/b.pdf", doi=""),
                    paper(3, oa_url="https://oa.example/c.pdf", doi="")])
    env.net.routes["https://oa.example/a.pdf"] = lambda r: pdf_resp(b"just some text")
    env.net.routes["https://oa.example/b.pdf"] = lambda r: pdf_resp(b"%PDF-1.4 garbage, no body")
    env.net.routes["https://oa.example/c.pdf"] = lambda r: pdf_resp(b"")
    got = run_job(env, "W1", "W2", "W3")
    assert [got[k]["reason"] for k in ("W1", "W2", "W3")] == ["not_pdf"] * 3
    assert not list(env.root.rglob("*.pdf")) and not part_files(env.root)


def test_http_error_and_no_open_link(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/gone.pdf", doi=""),
                    paper(2, oa_url="", doi="")])
    got = run_job(env, "W1", "W2")
    assert got["W1"]["status"] == "needs_access" and got["W1"]["reason"] == "http_error"
    assert "404" in got["W1"]["detail"]
    assert got["W2"]["status"] == "needs_access" and got["W2"]["reason"] == "no_open_link"
    assert env.net.requests == [] or all("gone.pdf" in u for u in env.net.urls())


def test_unpaywall_lookup_by_doi_with_mailto(make_env):
    env = make_env([paper(1, oa_url="", doi="10.1/abc")])
    env.net.routes["https://api.unpaywall.org/v2/10.1/abc"] = lambda r: httpx.Response(200, json={
        "best_oa_location": {"url_for_pdf": "https://repo.example/x.pdf",
                             "license": "cc-by", "version": "publishedVersion"}})
    env.net.routes["https://repo.example/x.pdf"] = lambda r: pdf_resp()
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "downloaded_verified" and got["origin"] == "unpaywall"
    assert got["license"] == "cc-by" and got["version"] == "publishedVersion"
    first = env.net.requests[0]
    assert first.url.params["email"] == "me@example.org"
    assert "me@example.org" not in (got["source_url"] + got["final_url"])


def test_unpaywall_used_after_oa_url_fails(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p.pdf", doi="10.1/abc")])
    env.net.routes["https://oa.example/p.pdf"] = lambda r: html_resp()
    env.net.routes["https://api.unpaywall.org/v2/10.1/abc"] = lambda r: httpx.Response(200, json={
        "oa_locations": [{"url_for_pdf": "https://repo.example/x.pdf"}]})
    env.net.routes["https://repo.example/x.pdf"] = lambda r: pdf_resp()
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "downloaded_verified" and got["origin"] == "unpaywall"


def test_unpaywall_without_pdf_or_without_email_gives_needs_access(make_env):
    env = make_env([paper(1, oa_url="", doi="10.1/abc")])
    env.net.routes["https://api.unpaywall.org/v2/10.1/abc"] = lambda r: httpx.Response(
        200, json={"is_oa": False, "best_oa_location": None})
    got = run_job(env, "W1")["W1"]
    assert got["reason"] == "no_open_link" and "Unpaywall" in got["detail"]

    env2 = make_env([paper(1, oa_url="", doi="10.1/abc")], contact_email=None)
    got = run_job(env2, "W1")["W1"]
    assert got["reason"] == "no_open_link" and "CONTACT_EMAIL" in got["detail"]
    assert env2.net.requests == []


def test_nothing_else_is_tried(make_env):
    # landing_url must never be fetched: only oa_url and Unpaywall are legal sources.
    p = paper(1, oa_url="", doi="")
    p.landing_url = "https://publisher.example/paper"
    env = make_env([p])
    got = run_job(env, "W1")["W1"]
    assert got["reason"] == "no_open_link" and env.net.requests == []


def test_pending_then_done_and_status_listing(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    r = start(env, "W1")
    assert r.json()["queued"] == [{"source": "openalex", "id": "W1"}]
    job = env.client.get(f"/api/downloads/jobs/{r.json()['job_id']}").json()
    assert job["total"] == job["finished"] == 1
    listing = env.client.get("/api/downloads", params={"run_id": env.run_id}).json()
    assert [x["status"] for x in listing] == ["downloaded_verified"]
    with connect(env.settings.database_path) as conn:
        row = conn.execute("SELECT kind, status FROM jobs").fetchone()
    assert (row["kind"], row["status"]) == ("pdf_download", "done")
    # A validated paper is not fetched again, and is reported rather than skipped silently.
    n = len(env.net.requests)
    again = start(env, "W1").json()
    assert again["job_id"] is None and again["already_downloaded"] == [
        {"source": "openalex", "id": "W1"}]
    assert len(env.net.requests) == n


def test_pending_status_is_recorded_before_the_job_runs(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    with connect(env.settings.database_path) as conn:
        uid = conn.execute("SELECT id FROM users").fetchone()["id"]
        downloads.save_record(conn, uid, "openalex", "W1", status="pending")
        row = conn.execute("SELECT * FROM downloads").fetchone()
    assert downloads.public_record(row)["has_file"] is False


# ---------------------------------------------------------------- request limits and selection

def test_max_50_papers_and_papers_must_belong_to_the_run(make_env):
    env = make_env([paper(1)])
    many = [{"source": "openalex", "id": f"W{i}"} for i in range(51)]
    assert env.client.post("/api/downloads", json={"run_id": env.run_id, "papers": many}
                           ).status_code == 422
    assert env.client.post("/api/downloads", json={"run_id": env.run_id, "papers": []}
                           ).status_code == 422
    assert start(env, "W999").status_code == 404
    assert env.client.post("/api/downloads", json={"run_id": 999, "papers": many[:1]}
                           ).status_code == 404


def test_requires_sign_in(make_env):
    env = make_env([paper(1)])
    anon = TestClient(env.app)
    assert anon.post("/api/downloads", json={"run_id": 1, "papers": []}).status_code == 401
    assert anon.get("/api/downloads/file", params={"source": "a", "paper_id": "b"}
                    ).status_code == 401


def test_per_user_hourly_cap(make_env):
    env = make_env([paper(i) for i in range(1, 6)], downloads_per_hour=3)
    assert start(env, "W1", "W2").status_code == 202
    over = start(env, "W3", "W4")
    assert over.status_code == 429 and "1 left" in over.json()["detail"]
    assert start(env, "W3").status_code == 202
    assert start(env, "W5").status_code == 429


# ---------------------------------------------------------------- isolation

def test_user_b_cannot_fetch_or_see_user_a_pdf(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    job_id = start(env, "W1").json()["job_id"]
    key = {"source": "openalex", "paper_id": "W1"}
    assert env.client.get("/api/downloads/file", params=key).status_code == 200

    other = TestClient(env.app)
    register(other, "bob@example.org")
    assert other.get("/api/downloads/file", params=key).status_code == 404
    assert other.get(f"/api/downloads/jobs/{job_id}").status_code == 404
    assert other.get("/api/downloads", params={"run_id": env.run_id}).status_code == 404
    assert other.post("/api/downloads", json={
        "run_id": env.run_id, "papers": [{"source": "openalex", "id": "W1"}]}).status_code == 404
    assert other.post("/api/downloads/upload", params={"source": "openalex", "paper_id": "W1"},
                      content=GOOD, headers={"content-type": "application/pdf"}).status_code == 404
    assert pdf_path(env.root, 2, "openalex", "W1") != pdf_path(env.root, 1, "openalex", "W1")


def test_paths_cannot_escape_the_root(tmp_path):
    for evil in ("../../etc/passwd", "..", ".", "a/b", "x\x00y", "é/中"):
        path = pdf_path(tmp_path, 1, "openalex", evil)
        assert path.resolve().is_relative_to((tmp_path / "1" / "openalex").resolve()), evil
    assert safe_name("W1") == "W1"
    assert safe_name("a/b") != safe_name("a_b")
    assert len(safe_name("x" * 500)) < 100


# ---------------------------------------------------------------- upload

def upload(client, data, ctype="application/pdf", pid="W1"):
    return client.post("/api/downloads/upload", params={"source": "openalex", "paper_id": pid},
                       content=data, headers={"content-type": ctype})


def test_upload_wins_and_is_never_fetched(make_env):
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")])
    r = upload(env.client, GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "downloaded_verified" and body["origin"] == "upload"
    assert body["sha256"] == hashlib.sha256(GOOD).hexdigest()
    again = start(env, "W1").json()
    assert again["job_id"] is None and again["already_downloaded"]
    assert env.net.requests == []
    assert env.client.get("/api/downloads/file", params={"source": "openalex", "paper_id": "W1"}
                          ).content == GOOD


def test_upload_replaces_needs_access_entry(make_env):
    env = make_env([paper(1, oa_url="", doi="")])
    assert run_job(env, "W1")["W1"]["status"] == "needs_access"
    assert upload(env.client, GOOD).json()["status"] == "downloaded_verified"


def test_upload_validation(make_env):
    env = make_env([paper(1)], pdf_max_bytes=len(GOOD) + 50)
    assert upload(env.client, GOOD, ctype="text/html").status_code == 415
    assert upload(env.client, b"<html>nope</html>").status_code == 422
    assert upload(env.client, b"%PDF-1.4 broken").status_code == 422
    assert upload(env.client, b"").status_code == 422
    assert upload(env.client, GOOD + b"0" * 500).status_code == 413
    assert upload(env.client, GOOD, pid="W404").status_code == 404
    assert not list(env.root.rglob("*.pdf")) and not part_files(env.root)
    rows = env.client.get("/api/downloads", params={"run_id": env.run_id}).json()
    assert rows == []


def test_upload_streaming_limit_without_content_length(make_env):
    env = make_env([paper(1)], pdf_max_bytes=len(GOOD) + 50)
    # Generator content is sent chunked: no Content-Length for the server to check up front.
    big = (c for c in [GOOD, b"0" * 500])
    assert upload(env.client, big).status_code == 413
    assert not part_files(env.root)


def test_upload_mismatch_is_not_offered(make_env):
    env = make_env([paper(1)])
    r = upload(env.client, make_pdf(OTHER_BODY))
    assert r.json()["status"] == "identity_mismatch"
    assert env.client.get("/api/downloads/file", params={"source": "openalex", "paper_id": "W1"}
                          ).status_code == 404


# ---------------------------------------------------------------- identity and atomic write

def test_identity_rules():
    assert check_identity(TITLE, "", f"Header\n{TITLE.upper()}\nAuthors")[0] == "downloaded_verified"
    assert check_identity(TITLE, "10.5/x", "see doi:10.5/ x here")[2] == "doi"
    assert check_identity(TITLE, "10.5/x", "see 10.5/xy here")[2] != "doi"  # not a prefix match
    assert check_identity(TITLE, "", " ".join(OTHER_BODY))[0] == "identity_mismatch"
    assert check_identity(TITLE, "", "")[0] == "downloaded_unverified_identity"
    near = "Neutron transport in thermal systems and fast reactors: a long study " + " ".join(OTHER_BODY)
    status = check_identity(TITLE, "", near)[0]
    assert status in ("downloaded_unverified_identity", "downloaded_verified")
    assert check_identity("", "", " ".join(OTHER_BODY))[0] == "downloaded_unverified_identity"


def test_validate_pdf_rules(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(GOOD)
    ok = validate_pdf(f)
    assert ok.ok and ok.size == len(GOOD) and TITLE in ok.text
    assert validate_pdf(f, max_bytes=10).code == "too_large"
    f.write_bytes(b"")
    assert validate_pdf(f).code == "not_pdf"
    f.write_bytes(b"  %PDF-1.4" + GOOD[8:])
    assert not validate_pdf(f).ok  # must start with %PDF-


def test_atomic_write_final_appears_only_after_validation(tmp_path, monkeypatch):
    final = pdf_path(tmp_path, 1, "openalex", "W1")
    part = final.with_name("W1.pdf.abc.part")
    part.parent.mkdir(parents=True)
    part.write_bytes(GOOD)
    seen = {}
    real = downloads.validate_pdf

    def spy(path, max_bytes):
        seen["final_exists"], seen["part_exists"] = final.exists(), part.exists()
        return real(path, max_bytes)

    monkeypatch.setattr(downloads, "validate_pdf", spy)
    v = finalize_pdf(part, tmp_path, 1, "openalex", "W1", TITLE, "")
    assert seen == {"final_exists": False, "part_exists": True}
    assert v.status == "downloaded_verified" and final.read_bytes() == GOOD
    assert not part.exists() and not part_files(tmp_path)


def test_failed_validation_leaves_existing_file_untouched(tmp_path):
    final = pdf_path(tmp_path, 1, "openalex", "W1")
    final.parent.mkdir(parents=True)
    final.write_bytes(GOOD)
    part = final.with_name("W1.pdf.zzz.part")
    part.write_bytes(b"<html>oops</html>")
    v = finalize_pdf(part, tmp_path, 1, "openalex", "W1", TITLE, "")
    assert v.status == "needs_access" and v.reason == "html_returned"
    assert final.read_bytes() == GOOD and not part.exists()


# ---------------------------------------------------------------- SSRF

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.pdf", "http://localhost/a.pdf", "http://10.1.2.3/a.pdf",
    "http://192.168.0.10/a.pdf", "http://172.16.5.5/a.pdf", "http://169.254.169.254/latest",
    "http://[::1]/a.pdf", "http://[::ffff:127.0.0.1]/a.pdf", "http://[fe80::1]/a.pdf",
    "http://0.0.0.0/a.pdf", "http://100.64.0.1/a.pdf", "http://db.internal/a.pdf",
    "file:///etc/passwd", "ftp://example.org/a.pdf", "gopher://example.org/",
    "http://user:pass@example.org/a.pdf", "http://user@example.org/a.pdf", "http:///a.pdf",
    "https://example.org:notaport/a.pdf",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURL):
        check_public_url(url, fake_resolver)


def test_public_urls_accepted():
    assert check_public_url("https://Example.org/a.pdf", fake_resolver) == "example.org"
    assert check_public_url("http://93.184.216.34:8080/a.pdf", fake_resolver) == "93.184.216.34"


def test_unsafe_oa_url_is_never_requested(make_env):
    env = make_env([paper(1, oa_url="http://127.0.0.1:8000/secret.pdf", doi="")])
    got = run_job(env, "W1")["W1"]
    assert got["reason"] == "unsafe_url" and got["status"] == "needs_access"
    assert env.net.requests == []


def test_redirect_to_private_address_is_blocked():
    net = Net()
    net.routes["https://oa.example/a.pdf"] = lambda r: httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    net.routes["http://169.254.169.254/latest/meta-data"] = lambda r: pdf_resp()
    res = make_fetcher(net).fetch("https://oa.example/a.pdf", lambda resp: None)
    assert not res.ok and res.code == "unsafe_url"
    assert net.urls() == ["https://oa.example/a.pdf"]


def test_redirect_to_hostname_resolving_private_is_blocked():
    net = Net()
    net.routes["https://oa.example/a.pdf"] = lambda r: httpx.Response(
        301, headers={"location": "https://files.internal/a.pdf"})
    res = make_fetcher(net).fetch("https://oa.example/a.pdf", lambda resp: None)
    assert res.code == "unsafe_url" and len(net.requests) == 1


def test_redirects_are_followed_with_a_cap(tmp_path):
    net = Net()
    net.routes["https://a.example/start"] = lambda r: httpx.Response(
        302, headers={"location": "/final.pdf"})
    net.routes["https://a.example/final.pdf"] = lambda r: pdf_resp()
    part = tmp_path / "x.part"
    res = make_fetcher(net).fetch("https://a.example/start", pdf_sink(part))
    assert res.ok and res.final_url == "https://a.example/final.pdf" and part.read_bytes() == GOOD

    loop = Net()
    loop.routes["https://a.example/loop"] = lambda r: httpx.Response(
        302, headers={"location": "https://a.example/loop"})
    res = make_fetcher(loop, max_redirects=3).fetch("https://a.example/loop", lambda r: None)
    assert res.code == "http_error" and "redirect" in res.detail.lower()
    assert len(loop.requests) == 4


# ---------------------------------------------------------------- politeness

def test_requests_to_one_host_are_serialised():
    active, peak = defaultdict(int), defaultdict(int)
    guard = threading.Lock()

    def handler(request):
        host = request.url.host
        with guard:
            active[host] += 1
            peak[host] = max(peak[host], active[host])
        time.sleep(0.02)
        with guard:
            active[host] -= 1
        return httpx.Response(200, json={})

    fetcher = Fetcher(transport=httpx.MockTransport(handler), resolver=fake_resolver,
                      host_delay=0, sleep=lambda s: None)
    urls = [f"https://same.example/{i}" for i in range(6)] + \
           [f"https://other.example/{i}" for i in range(6)]
    threads = [threading.Thread(target=fetcher.fetch, args=(u, downloads.json_sink)) for u in urls]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak["same.example"] == 1 and peak["other.example"] == 1


def test_minimum_delay_between_requests_to_the_same_host():
    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    net = Net()
    net.routes["https://h.example/1"] = lambda r: httpx.Response(200, json={})
    net.routes["https://h.example/2"] = lambda r: httpx.Response(200, json={})
    net.routes["https://g.example/1"] = lambda r: httpx.Response(200, json={})
    f = make_fetcher(net, host_delay=2.0, sleep=sleep, clock=lambda: now[0])
    f.fetch("https://h.example/1", downloads.json_sink)
    assert slept == []
    f.fetch("https://g.example/1", downloads.json_sink)  # a different host is not delayed
    assert slept == []
    f.fetch("https://h.example/2", downloads.json_sink)
    assert len(slept) == 1 and slept[0] == pytest.approx(2.0)


def test_retry_after_is_honoured_and_retries_are_bounded():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json={"ok": True})

    net = Net()
    net.routes["https://h.example/x"] = flaky
    f = make_fetcher(net, host_delay=1.0, sleep=sleep, clock=lambda: now[0], max_retries=2)
    res = f.fetch("https://h.example/x", downloads.json_sink)
    assert res.ok and res.value == {"ok": True} and slept == [pytest.approx(7.0)]

    down = Net()
    down.routes["https://h.example/x"] = lambda r: httpx.Response(503)
    f = make_fetcher(down, host_delay=0, max_retries=2)
    res = f.fetch("https://h.example/x", downloads.json_sink)
    assert not res.ok and res.code == "http_error" and res.status == 503
    assert len(down.requests) == 3  # one try plus two retries, then stop

    ratelimited = Net()
    ratelimited.routes["https://h.example/x"] = lambda r: httpx.Response(
        429, headers={"retry-after": "100000"})
    slept.clear()
    f = make_fetcher(ratelimited, host_delay=0, sleep=sleep, clock=lambda: now[0], max_retries=1)
    f.fetch("https://h.example/x", downloads.json_sink)
    assert max(slept) <= 60  # a huge Retry-After is capped, not obeyed forever


def test_network_errors_are_reported_not_raised():
    def boom(request):
        raise httpx.ConnectError("refused")

    f = Fetcher(transport=httpx.MockTransport(boom), resolver=fake_resolver, host_delay=0,
                sleep=lambda s: None, max_retries=1)
    res = f.fetch("https://h.example/x", downloads.json_sink)
    assert res.code == "network_error"
