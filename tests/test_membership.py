"""M7: encrypted keys, proxy links and private uploads. No live network."""

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_accounts_search import register
from test_downloads import GOOD, Env, make_env, paper  # noqa: F401  (make_env is a fixture)

from bayan.api import create_app
from bayan.config import Settings
from bayan.membership import (
    InvalidInput,
    SecretNotConfigured,
    build_link,
    fernet_for,
    get_user_key,
    validate_template,
)

SECRET = "test-secret-0123456789-abcdefghij"
OTHER_SECRET = "another-secret-9876543210-zyxwvu"
# Fake keys made up for the tests; they are not credentials for anything.
KEY_A = "fake-ieee-key-AAAA1111BBBB2222"
KEY_B = "fake-elsevier-key-CCCC3333DDDD"
TEMPLATE = "https://ezproxy.uni.example/login?url={url}"
PDF_HEADERS = {"content-type": "application/pdf"}


def make_client(tmp_path, secret=SECRET, name="app"):
    settings = Settings(database_path=tmp_path / f"{name}.sqlite", pdf_root=tmp_path / "pdfs",
                        app_secret_key=secret)
    return TestClient(create_app(settings)), settings


@pytest.fixture
def client(tmp_path):
    c, settings = make_client(tmp_path)
    with c:
        register(c)
        c.settings = settings
        yield c


def put_key(c, publisher="ieee", key=KEY_A):
    return c.put(f"/api/membership/{publisher}/key", json={"key": key})


def status(c, publisher="ieee"):
    return next(p for p in c.get("/api/membership").json()["publishers"]
                if p["id"] == publisher)


def second_user(c):
    other = TestClient(c.app)
    other.__enter__()
    register(other, "bob@example.org", "another good password")
    return other


# ---------------------------------------------------------------- keys

def test_publishers_are_data_driven(client):
    ids = [p["id"] for p in client.get("/api/membership").json()["publishers"]]
    assert ids == ["ieee", "elsevier", "springer", "wiley"]


def test_round_trip_and_status_hides_the_key(client):
    r = put_key(client)
    assert r.status_code == 200
    assert KEY_A not in r.text
    st = status(client)
    assert st["saved"] and not st["needs_reentry"] and st["last4"] == KEY_A[-4:]
    assert st["saved_at"].endswith("Z")
    assert KEY_A not in client.get("/api/membership").text
    assert get_user_key(1, "ieee", client.settings) == KEY_A
    assert get_user_key(1, "wiley", client.settings) is None


def test_ciphertext_in_database_file_differs_from_plaintext(client):
    put_key(client)
    row = sqlite3.connect(client.settings.database_path).execute(
        "SELECT ciphertext FROM membership_keys").fetchone()
    assert row[0] != KEY_A and KEY_A not in row[0]
    for suffix in ("", "-wal"):  # the WAL may still hold recent pages
        path = str(client.settings.database_path) + suffix
        try:
            raw = open(path, "rb").read()
        except FileNotFoundError:
            continue
        assert KEY_A.encode() not in raw


def test_short_keys_show_no_characters(client):
    put_key(client, key="shortkey12")
    assert status(client)["last4"] is None


def test_replace_and_delete(client):
    put_key(client)
    put_key(client, key=KEY_B)
    assert get_user_key(1, "ieee", client.settings) == KEY_B
    r = client.delete("/api/membership/ieee/key")
    assert r.status_code == 200 and not status(client)["saved"]
    assert get_user_key(1, "ieee", client.settings) is None


def test_unknown_publisher_and_signed_out(client, tmp_path):
    assert put_key(client, "cnki").status_code == 404
    assert client.delete("/api/membership/nope/key").status_code == 404
    anon, _ = make_client(tmp_path, name="anon")
    with anon:
        assert anon.get("/api/membership").status_code == 401
        assert anon.put("/api/membership/ieee/key", json={"key": KEY_A}).status_code == 401


@pytest.mark.parametrize("secret", [None, "", "   ", "change-me", "your-key-here",
                                    "Change-Me-Please-Now-123", "short"])
def test_missing_or_placeholder_secret_refuses_to_save(tmp_path, secret):
    c, settings = make_client(tmp_path, secret=secret)
    with c:
        register(c)
        r = put_key(c)
        assert r.status_code == 503
        assert "APP_SECRET_KEY" in r.json()["detail"] and KEY_A not in r.text
        view = c.get("/api/membership").json()
        assert view["keys_enabled"] is False and view["keys_disabled_reason"]
        assert not status(c)["saved"]
        count = sqlite3.connect(settings.database_path).execute(
            "SELECT COUNT(*) FROM membership_keys").fetchone()[0]
        assert count == 0
    with pytest.raises(SecretNotConfigured):
        fernet_for(secret)


def test_rotated_secret_needs_reentry_without_crashing(tmp_path):
    c, settings = make_client(tmp_path)
    with c:
        register(c)
        put_key(c)
    rotated = Settings(database_path=settings.database_path, pdf_root=settings.pdf_root,
                       app_secret_key=OTHER_SECRET)
    c2 = TestClient(create_app(rotated))
    with c2:
        assert c2.post("/api/auth/login", json={"email": "ada@example.org",
                                                "password": "correct horse battery"}).status_code == 200
        st = status(c2)
        assert st == {"id": "ieee", "name": "IEEE Xplore", "saved": True, "needs_reentry": True,
                      "last4": None, "saved_at": st["saved_at"]}
        assert get_user_key(1, "ieee", rotated) is None
        assert put_key(c2, key=KEY_B).status_code == 200  # re-entering fixes it
        assert not status(c2)["needs_reentry"]
        assert get_user_key(1, "ieee", rotated) == KEY_B


def test_unreadable_secret_after_removal_does_not_crash(tmp_path):
    c, settings = make_client(tmp_path)
    with c:
        register(c)
        put_key(c)
    gone = Settings(database_path=settings.database_path, app_secret_key=None)
    c2 = TestClient(create_app(gone))
    with c2:
        c2.post("/api/auth/login", json={"email": "ada@example.org",
                                         "password": "correct horse battery"})
        assert status(c2)["needs_reentry"] is True
        assert c2.delete("/api/membership/ieee/key").status_code == 200


def test_invalid_key_error_never_echoes_the_value(client):
    for bad in ("short", "has a space inside the key", "x" * 600, 12345, None):
        r = put_key(client, key=bad)
        assert r.status_code == 422, bad
        if isinstance(bad, str) and len(bad) > 8:
            assert bad not in r.text


def test_no_plaintext_key_in_any_response_or_log(client, caplog):
    caplog.set_level(logging.DEBUG)
    put_key(client)
    put_key(client, "elsevier", KEY_B)
    responses = [
        client.get("/api/membership"), client.get("/api/uploads"), client.get("/api/runs"),
        client.put("/api/membership/proxy", json={"template": TEMPLATE}),
        client.post("/api/membership/proxy-link", json={"doi": "10.1/abc"}),
        put_key(client, key="bad key"), put_key(client, "cnki"),
        client.delete("/api/membership/elsevier/key"),
    ]
    text = "".join(r.text for r in responses) + caplog.text
    assert KEY_A not in text and KEY_B not in text
    assert client.settings.app_secret_key not in text


def test_keys_are_not_in_run_summaries_or_exports(make_env):  # noqa: F811
    secret = SECRET
    env = make_env([paper(1)], app_secret_key=secret)
    env.client.put("/api/membership/ieee/key", json={"key": KEY_A})
    env.client.put("/api/membership/proxy", json={"template": TEMPLATE})
    run = env.client.get(f"/api/runs/{env.run_id}")
    csv = env.client.post(f"/api/runs/{env.run_id}/export", json={"format": "csv"})
    xlsx = env.client.post(f"/api/runs/{env.run_id}/export", json={"format": "xlsx"})
    runs = env.client.get("/api/runs")
    for r in (run, csv, xlsx, runs):
        assert KEY_A.encode() not in r.content


def test_keys_are_isolated_per_user(client):
    put_key(client)
    bob = second_user(client)
    assert not status(bob)["saved"]
    put_key(bob, key=KEY_B)
    assert get_user_key(1, "ieee", client.settings) == KEY_A
    assert get_user_key(2, "ieee", client.settings) == KEY_B
    bob.delete("/api/membership/ieee/key")
    assert status(client)["saved"] and status(client)["last4"] == KEY_A[-4:]
    bob.__exit__(None, None, None)


# ---------------------------------------------------------------- proxy

@pytest.mark.parametrize("template,fragment", [
    ("http://ezproxy.uni.example/login?url={url}", "https"),
    ("ftp://ezproxy.uni.example/login?url={url}", "https"),
    ("https://user:pw@ezproxy.uni.example/login?url={url}", "username or password"),
    ("https://user@ezproxy.uni.example/login?url={url}", "username or password"),
    ("https://localhost/login?url={url}", "public"),
    ("https://127.0.0.1/login?url={url}", "public"),
    ("https://10.0.0.5/login?url={url}", "public"),
    ("https://192.168.1.1/login?url={url}", "public"),
    ("https://[::1]/login?url={url}", "public"),
    ("https://169.254.169.254/latest?url={url}", "public"),
    ("https://printer.local/login?url={url}", "public"),
    ("https://intranet/login?url={url}", "public"),
    ("https://ezproxy.uni.example/login", "{url}"),
    ("https://ezproxy.uni.example/login?url={other}{url}", "placeholders"),
    ("https://{url}/login", "host"),
    ("https://ezproxy.uni.example/login?url={url} x", "spaces"),
    ("https://ezproxy.uni.example/" + "a" * 600 + "{url}", "longer"),
    ("", "Enter"),
])
def test_template_validation_rejects(client, template, fragment):
    with pytest.raises(InvalidInput):
        validate_template(template)
    r = client.put("/api/membership/proxy", json={"template": template})
    assert r.status_code == 422
    assert fragment in r.json()["detail"]
    assert client.get("/api/membership").json()["proxy"] is None


def test_template_accepts_ezproxy_and_openathens_styles():
    assert validate_template(TEMPLATE) == TEMPLATE
    athens = "https://go.openathens.net/redirector/uni.example?url={url}"
    assert validate_template(athens) == athens
    assert validate_template("https://proxy.uni.example/login?url=https://doi.org/{doi}")


def test_proxy_link_from_doi_and_url(client):
    assert client.post("/api/membership/proxy-link", json={"doi": "10.1/x"}).status_code == 404
    saved = client.put("/api/membership/proxy", json={"template": TEMPLATE}).json()
    assert saved["proxy"]["template"] == TEMPLATE
    assert saved["proxy"]["example"] == (
        "https://ezproxy.uni.example/login?url=https%3A%2F%2Fdoi.org%2F10.1000%2Fxyz123")
    r = client.post("/api/membership/proxy-link", json={"doi": "https://doi.org/10.1016/j.x(1)2"})
    assert r.json()["link"] == ("https://ezproxy.uni.example/login?url="
                                "https%3A%2F%2Fdoi.org%2F10.1016%2Fj.x%281%292")
    r = client.post("/api/membership/proxy-link",
                    json={"url": "https://www.nature.com/articles/abc?a=1&b=2"})
    assert r.json()["link"] == ("https://ezproxy.uni.example/login?url="
                                "https%3A%2F%2Fwww.nature.com%2Farticles%2Fabc%3Fa%3D1%26b%3D2")
    assert "does not log in" in r.json()["note"]


def test_proxy_link_rejects_bad_targets(client):
    client.put("/api/membership/proxy", json={"template": TEMPLATE})
    for body in ({}, {"doi": "not-a-doi"}, {"url": "javascript:alert(1)"},
                 {"url": "https://u:p@publisher.example/x"}, {"url": "https://a.example/ b"}):
        assert client.post("/api/membership/proxy-link", json=body).status_code == 422, body


def test_doi_template_needs_a_doi_and_substitutes_once():
    t = "https://proxy.uni.example/login?url=https://doi.org/{doi}"
    assert build_link(t, doi="10.1/abc") == "https://proxy.uni.example/login?url=https://doi.org/10.1/abc"
    with pytest.raises(InvalidInput):
        build_link(t, url="https://a.example/x")


def test_proxy_link_preview_with_unsaved_template(client):
    r = client.post("/api/membership/proxy-link", json={"doi": "10.1/x", "template": TEMPLATE})
    assert r.status_code == 200 and r.json()["link"].startswith("https://ezproxy.uni.example/")
    bad = client.post("/api/membership/proxy-link",
                      json={"doi": "10.1/x", "template": "http://x.example/{url}"})
    assert bad.status_code == 422
    assert client.get("/api/membership").json()["proxy"] is None


def test_proxy_is_per_user_and_removable(client):
    client.put("/api/membership/proxy", json={"template": TEMPLATE})
    bob = second_user(client)
    assert bob.get("/api/membership").json()["proxy"] is None
    assert bob.post("/api/membership/proxy-link", json={"doi": "10.1/x"}).status_code == 404
    other = "https://proxy.bob.example/go?url={url}"
    bob.put("/api/membership/proxy", json={"template": other})
    assert client.get("/api/membership").json()["proxy"]["template"] == TEMPLATE
    assert client.delete("/api/membership/proxy").json()["proxy"] is None
    assert bob.get("/api/membership").json()["proxy"]["template"] == other
    bob.__exit__(None, None, None)


def test_run_rows_carry_a_proxy_link_only_when_a_template_is_saved(make_env):  # noqa: F811
    env = make_env([paper(1), paper(2, doi="")], app_secret_key=SECRET)
    papers = env.client.get(f"/api/runs/{env.run_id}").json()["papers"]
    assert all("proxy_link" not in p for p in papers)
    env.client.put("/api/membership/proxy", json={"template": TEMPLATE})
    papers = {p["id"]: p for p in env.client.get(f"/api/runs/{env.run_id}").json()["papers"]}
    assert papers["W1"]["proxy_link"] == (
        "https://ezproxy.uni.example/login?url=https%3A%2F%2Fdoi.org%2F10.1%2F1")
    assert papers["W2"]["proxy_link"] is None  # no DOI and no link to wrap


# ---------------------------------------------------------------- uploads

def upload(c, source="openalex", paper_id="W1"):
    return c.post("/api/downloads/upload", params={"source": source, "paper_id": paper_id},
                  content=GOOD, headers=PDF_HEADERS)


def test_uploads_are_listed_and_deleted_by_the_owner(make_env):  # noqa: F811
    env = make_env([paper(1)], app_secret_key=SECRET)
    assert env.client.get("/api/uploads").json() == []
    assert upload(env.client).status_code == 200
    manual = env.client.post("/api/manuals/upload", content=GOOD, headers=PDF_HEADERS,
                             params={"title": "My manual", "run_id": env.run_id}).json()
    mid = manual["paper"]["id"]
    listing = env.client.get("/api/uploads").json()
    assert {(u["source"], u["id"], u["kind"]) for u in listing} == {
        ("openalex", "W1", "paper"), ("user", mid, "manual")}
    assert all("path" not in u and u["has_file"] for u in listing)
    assert len(list(env.root.rglob("*.pdf"))) == 2

    assert env.client.delete("/api/uploads", params={"source": "openalex",
                                                     "paper_id": "W1"}).status_code == 200
    assert env.client.get("/api/downloads/file", params={
        "source": "openalex", "paper_id": "W1"}).status_code == 404
    assert env.client.delete("/api/uploads", params={"source": "user",
                                                     "paper_id": mid}).status_code == 200
    assert env.client.get("/api/uploads").json() == []
    assert not list(env.root.rglob("*.pdf"))
    assert env.client.get("/api/manuals").json() == []
    run_ids = [p["id"] for p in env.client.get(f"/api/runs/{env.run_id}").json()["papers"]]
    assert mid not in run_ids and "W1" in run_ids
    assert env.client.delete("/api/uploads", params={"source": "user",
                                                     "paper_id": mid}).status_code == 404


def test_openly_downloaded_files_are_not_listed_as_uploads(make_env):  # noqa: F811
    env = make_env([paper(1, oa_url="https://oa.example/p1.pdf")], app_secret_key=SECRET)
    from test_downloads import pdf_resp, start
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    assert start(env, "W1").status_code == 202
    assert env.client.get("/api/uploads").json() == []
    assert env.client.delete("/api/uploads", params={"source": "openalex",
                                                     "paper_id": "W1"}).status_code == 404


def test_user_b_cannot_list_fetch_or_delete_user_a_uploads(make_env):  # noqa: F811
    env = make_env([paper(1)], app_secret_key=SECRET)
    upload(env.client)
    manual = env.client.post("/api/manuals/upload", content=GOOD, headers=PDF_HEADERS).json()
    mid = manual["paper"]["id"]
    bob = TestClient(env.app)
    with bob:
        register(bob, "bob@example.org", "another good password")
        assert bob.get("/api/uploads").json() == []
        for source, pid in (("openalex", "W1"), ("user", mid)):
            params = {"source": source, "paper_id": pid}
            assert bob.get("/api/downloads/file", params=params).status_code == 404
            assert bob.delete("/api/uploads", params=params).status_code == 404
        # Bob uploading under the same ids creates nothing of Alice's and vice versa.
        assert upload(bob).status_code == 404  # W1 is not in Bob's runs
    assert len(env.client.get("/api/uploads").json()) == 2
    assert env.client.get("/api/downloads/file", params={
        "source": "openalex", "paper_id": "W1"}).content == GOOD
    assert len(list(env.root.rglob("*.pdf"))) == 2


def test_get_user_key_needs_no_request_context(client):
    put_key(client)
    assert get_user_key(1, "ieee", client.settings) == KEY_A
