"""M21: manuals added by public URL or PDF upload (no web search), private to their owner."""

from fastapi.testclient import TestClient
from test_accounts_search import register
from test_downloads import (  # noqa: F401  (make_env is a fixture)
    GOOD,
    html_resp,
    make_env,
    make_pdf,
    paper,
    part_files,
    pdf_resp,
)

MANUAL_URL = "https://docs.example/Pump-Manual_v2.pdf"
PDF_HEADERS = {"content-type": "application/pdf"}


def add_url(client, url=MANUAL_URL, **extra):
    return client.post("/api/manuals/url", json={"url": url, **extra})


def add_upload(client, data=GOOD, ctype="application/pdf", **params):
    return client.post("/api/manuals/upload", params=params, content=data,
                       headers={"content-type": ctype})


def type_row(env, key):
    report = env.client.get(f"/api/runs/{env.run_id}").json()["type_report"]
    return next(r for r in report["rows"] if r["id"] == key)


def env_with_manual_route(make_env):  # noqa: F811
    env = make_env([paper(1)])
    env.net.routes[MANUAL_URL] = lambda r: pdf_resp()
    return env


# ---------------------------------------------------------------- happy paths

def test_manual_by_url_is_added_to_run_and_labelled_user_supplied(make_env):  # noqa: F811
    env = env_with_manual_route(make_env)
    r = add_url(env.client, title="  Pump   maintenance manual ", run_id=env.run_id)
    assert r.status_code == 201, r.text
    body = r.json()
    p = body["paper"]
    assert p["source"] == "user" and p["id"].startswith("m-")
    assert p["title"] == "Pump maintenance manual"
    assert p["category"] == "other" and p["document_kind"] == "manual"
    assert p["user_supplied"] is True and p["type_key"] == "manual"
    assert p["type_label"] == "Manual (user-supplied)"
    assert body["origin"] == "url" and body["run_id"] == env.run_id
    d = body["download"]
    assert d["has_file"] and d["status"] == "downloaded_unverified_identity"
    assert d["identity_basis"] == "user_supplied" and "user-supplied" in d["detail"]
    assert d["source_url"] == MANUAL_URL
    assert not part_files(env.root)

    detail = env.client.get(f"/api/runs/{env.run_id}").json()
    assert [x["id"] for x in detail["papers"]][-1] == p["id"]
    assert type_row(env, "manual")["count"] == 1
    assert type_row(env, "peer_reviewed")["count"] == 1
    got = env.client.get("/api/downloads/file", params={"source": "user", "paper_id": p["id"]})
    assert got.status_code == 200 and got.content == GOOD


def test_title_defaults_from_url_and_never_uses_the_query(make_env):  # noqa: F811
    env = env_with_manual_route(make_env)
    r = add_url(env.client, MANUAL_URL + "?token=secret")
    assert r.status_code == 201
    assert r.json()["paper"]["title"] == "Pump Manual v2"
    assert r.json()["download"]["source_url"] == MANUAL_URL
    assert "secret" not in r.text


def test_manual_by_upload_collection_only_then_added_to_run(make_env):  # noqa: F811
    env = make_env([paper(1)])
    r = add_upload(env.client, title="Boiler manual")
    assert r.status_code == 201, r.text
    p = r.json()["paper"]
    assert r.json()["origin"] == "upload" and r.json()["run_id"] is None
    assert r.json()["download"]["origin"] == "upload"
    assert type_row(env, "manual")["count"] == 0
    listed = env.client.get("/api/manuals").json()
    assert [m["paper"]["id"] for m in listed] == [p["id"]]

    r2 = env.client.post(f"/api/runs/{env.run_id}/manuals", json={"paper_id": p["id"]})
    assert r2.status_code == 201
    assert type_row(env, "manual")["count"] == 1
    # adding twice keeps one row
    env.client.post(f"/api/runs/{env.run_id}/manuals", json={"paper_id": p["id"]})
    assert type_row(env, "manual")["count"] == 1


def test_manual_upload_to_run_and_edit_title(make_env):  # noqa: F811
    env = make_env([paper(1)])
    p = add_upload(env.client, title="Old", run_id=env.run_id).json()["paper"]
    r = env.client.patch(f"/api/manuals/{p['id']}", json={"title": "  New\ttitle "})
    assert r.status_code == 200 and r.json()["paper"]["title"] == "New title"
    titles = [x["title"] for x in env.client.get(f"/api/runs/{env.run_id}").json()["papers"]]
    assert "New title" in titles and "Old" not in titles


def test_same_file_twice_is_refused(make_env):  # noqa: F811
    env = make_env([paper(1)])
    assert add_upload(env.client, title="One").status_code == 201
    again = add_upload(env.client, title="Two")
    assert again.status_code == 409 and "One" in again.json()["detail"]
    assert len(env.client.get("/api/manuals").json()) == 1
    assert len(list(env.root.rglob("*.pdf"))) == 1


def test_manual_is_exported_marked_as_user_supplied(make_env):  # noqa: F811
    env = make_env([paper(1)])
    add_upload(env.client, title="Valve manual", run_id=env.run_id)
    out = env.client.post(f"/api/runs/{env.run_id}/export", json={"format": "csv"})
    text = out.content.decode("utf-8-sig")
    assert "Valve manual" in text and "user-supplied" in text and "manual" in text


# ---------------------------------------------------------------- rejections

def test_non_pdf_urls_are_rejected_and_leave_nothing(make_env):  # noqa: F811
    env = make_env([paper(1)])
    env.net.routes["https://docs.example/page"] = lambda r: html_resp()
    env.net.routes["https://docs.example/fake.pdf"] = lambda r: pdf_resp(b"PK\x03\x04 zip")
    env.net.routes["https://docs.example/broken.pdf"] = lambda r: pdf_resp(b"%PDF-1.4 broken")
    env.net.routes["https://docs.example/missing"] = lambda r: __import__("httpx").Response(404)
    for url, code in (("page", 422), ("fake.pdf", 422), ("broken.pdf", 422), ("missing", 502)):
        r = add_url(env.client, f"https://docs.example/{url}", run_id=env.run_id)
        assert r.status_code == code, (url, r.text)
    assert env.client.get("/api/manuals").json() == []
    assert type_row(env, "manual")["count"] == 0
    assert not part_files(env.root) and not list(env.root.rglob("*.pdf"))


def test_non_pdf_uploads_are_rejected(make_env):  # noqa: F811
    env = make_env([paper(1)])
    assert add_upload(env.client, ctype="text/html").status_code == 415
    assert add_upload(env.client, b"<html>nope</html>").status_code == 422
    assert add_upload(env.client, b"%PDF-1.4 broken").status_code == 422
    assert add_upload(env.client, b"").status_code == 422
    assert env.client.get("/api/manuals").json() == []
    assert not part_files(env.root) and not list(env.root.rglob("*.pdf"))


def test_oversize_upload_is_rejected(make_env):  # noqa: F811
    env = make_env([paper(1)], pdf_max_bytes=len(GOOD) + 100)
    assert add_upload(env.client, GOOD + b"0" * 500).status_code == 413
    assert not part_files(env.root)


def test_ssrf_urls_are_refused_without_a_request(make_env):  # noqa: F811
    env = make_env([paper(1)])
    for url in ("http://localhost/manual.pdf", "http://127.0.0.1/m.pdf",
                "http://169.254.169.254/latest/meta-data", "https://db.internal/m.pdf",
                "file:///etc/passwd", "ftp://docs.example/m.pdf",
                "https://user:pw@docs.example/m.pdf"):
        r = add_url(env.client, url)
        assert r.status_code == 422, (url, r.text)
    assert env.net.requests == []
    assert env.client.get("/api/manuals").json() == []


def test_redirect_to_a_private_address_is_refused(make_env):  # noqa: F811
    import httpx
    env = make_env([paper(1)])
    env.net.routes["https://docs.example/go"] = lambda r: httpx.Response(
        302, headers={"location": "http://10.0.0.5/secret.pdf"})
    r = add_url(env.client, "https://docs.example/go")
    assert r.status_code == 422 and "private" in r.json()["detail"]
    assert env.net.urls() == ["https://docs.example/go"]


def test_run_must_exist_and_belong_to_the_user(make_env):  # noqa: F811
    env = env_with_manual_route(make_env)
    assert add_url(env.client, run_id=999).status_code == 404
    assert add_upload(env.client, run_id=999).status_code == 404
    assert env.net.requests == []


def test_url_downloads_count_towards_the_hourly_limit(make_env):  # noqa: F811
    env = make_env([paper(1)], downloads_per_hour=1)
    env.net.routes[MANUAL_URL] = lambda r: pdf_resp()
    assert add_url(env.client).status_code == 201
    env.net.routes["https://docs.example/b.pdf"] = lambda r: pdf_resp(make_pdf(["Other"]))
    assert add_url(env.client, "https://docs.example/b.pdf").status_code == 429


# ---------------------------------------------------------------- ownership

def test_manuals_are_owner_only(make_env):  # noqa: F811
    env = env_with_manual_route(make_env)
    mine = add_url(env.client, title="Mine", run_id=env.run_id).json()["paper"]
    key = {"source": "user", "paper_id": mine["id"]}

    anon = TestClient(env.app)
    for call in (lambda: anon.get("/api/manuals"), lambda: add_url(anon),
                 lambda: add_upload(anon), lambda: anon.patch(f"/api/manuals/{mine['id']}",
                                                              json={"title": "x"})):
        assert call().status_code == 401

    other = TestClient(env.app)
    register(other, "bob@example.org")
    assert other.get("/api/manuals").json() == []
    assert other.get("/api/downloads/file", params=key).status_code == 404
    assert other.patch(f"/api/manuals/{mine['id']}", json={"title": "Hijacked"}).status_code == 404
    assert other.get(f"/api/runs/{env.run_id}").status_code == 404
    assert add_url(other, run_id=env.run_id).status_code == 404
    assert add_upload(other, run_id=env.run_id).status_code == 404
    assert other.post(f"/api/runs/{env.run_id}/manuals",
                      json={"paper_id": mine["id"]}).status_code == 404

    # Bob has his own run; my manual id cannot be attached to it either.
    bob_run = other.post("/api/search", json={"profile": {
        "query": "x", "year_from": 2020, "year_to": 2021}}).json()["id"]
    assert other.post(f"/api/runs/{bob_run}/manuals",
                      json={"paper_id": mine["id"]}).status_code == 404

    # The same file is not reported as a duplicate of someone else's manual.
    assert add_upload(other, title="Bob's copy").status_code == 201
    assert env.client.get("/api/manuals").json()[0]["paper"]["title"] == "Mine"
    assert env.client.patch(f"/api/manuals/{mine['id']}",
                            json={"title": "Mine v2"}).status_code == 200
    assert len(list(env.root.rglob("*.pdf"))) == 2
