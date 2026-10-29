"""M6: page index of validated PDFs, resumable download jobs and the download manifest."""

import csv
import io
import json
import os
import sqlite3
import time

import pytest
from test_accounts_search import register
from test_downloads import (  # noqa: F401  (make_env is a fixture)
    GOOD,
    TITLE,
    html_resp,
    make_env,
    make_pdf,
    paper,
    part_files,
    pdf_resp,
    run_job,
    start,
)

from bayan.db import connect, init_db
from bayan.downloads import PENDING, remove_stale_parts, resume_stale_jobs
from bayan.pages import excerpt, index_pdf, search_pages


@pytest.fixture
def env_of(make_env):  # noqa: F811  (the shared fixture from test_downloads)
    return make_env


def make_pages_pdf(pages):
    """A PDF with one page per list of text lines; an empty list gives an image-like page."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    n = len(pages)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [{}] /Count {} >>".format(
                " ".join(f"{4 + 2 * i} 0 R" for i in range(n)), n),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, lines in enumerate(pages):
        stream = ("BT /F1 12 Tf 72 720 Td 14 TL "
                  + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i}"
                    " 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


THREE = make_pages_pdf([
    [TITLE, "A. Author", "Abstract. We study transport of neutrons."],
    ["Methods", "Monte Carlo simulation with continuous energy cross sections."],
    ["Results", "The multiplication factor agrees within 200 pcm of the benchmark."],
])


def pages_of(env, pid="W1", client=None):
    client = client or env.client
    return client.get("/api/pages", params={"source": "openalex", "paper_id": pid})


def text_of(env, page, pid="W1"):
    return env.client.get("/api/pages/text",
                          params={"source": "openalex", "paper_id": pid, "page": page})


# ---------------------------------------------------------------- page index

def test_download_builds_page_index_with_page_numbers(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(THREE)
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "downloaded_verified"
    assert (got["page_count"], got["pages_with_text"], got["index_status"]) == (3, 3, "indexed")
    listing = pages_of(env).json()
    assert [p["page"] for p in listing["pages"]] == [1, 2, 3]
    assert listing["pages"][0]["label"] == "1" and listing["index_status"] == "indexed"
    page2 = text_of(env, 2).json()
    assert "Monte Carlo" in page2["text"] and page2["has_text"]
    assert page2["sha256"] == got["sha256"]
    assert text_of(env, 4).status_code == 404


def test_page_search_finds_the_cited_page(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(THREE)
    run_job(env, "W1")
    hits = env.client.get("/api/pages/search", params={"q": "multiplication BENCHMARK"}).json()
    assert [(h["id"], h["page"]) for h in hits] == [("W1", 3)]
    assert "multiplication factor" in hits[0]["excerpt"]
    scoped = env.client.get("/api/pages/search",
                            params={"q": "monte carlo", "run_id": env.run_id}).json()
    assert [h["page"] for h in scoped] == [2]
    assert env.client.get("/api/pages/search", params={"q": "absentword"}).json() == []
    assert env.client.get("/api/pages/search", params={"q": "!!"}).json() == []


def test_pages_are_private_to_the_owner(env_of):
    from fastapi.testclient import TestClient

    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(THREE)
    run_job(env, "W1")
    with TestClient(env.app) as other:
        register(other, "other@example.org")
        assert pages_of(env, client=other).status_code == 404
        assert other.get("/api/pages/text", params={
            "source": "openalex", "paper_id": "W1", "page": 1}).status_code == 404
        assert other.get("/api/pages/search", params={"q": "monte"}).json() == []
        assert other.get("/api/pages/search", params={
            "q": "monte", "run_id": env.run_id}).status_code == 404


def test_scanned_pdf_keeps_its_pages_and_says_ocr_is_needed(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(make_pages_pdf([[], []]))
    got = run_job(env, "W1")["W1"]
    assert got["has_file"]
    assert (got["page_count"], got["pages_with_text"], got["index_status"]) == (2, 0, "no_text")
    listing = pages_of(env).json()
    assert len(listing["pages"]) == 2 and "OCR" in listing["note"]
    assert text_of(env, 1).json()["has_text"] is False


def test_identity_mismatch_is_not_indexed(env_of):
    other = make_pages_pdf([["Consumer attitudes towards sustainable packaging in retail",
                             "We surveyed shoppers across several regions and compared stated",
                             "preferences with observed purchasing behaviour over twelve months",
                             "of loyalty card data. Results show a persistent gap between",
                             "intention and action among respondents in every region."]])
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(other)
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "identity_mismatch" and got["index_status"] is None
    assert pages_of(env).status_code == 404
    assert env.client.get("/api/pages/search", params={"q": "shoppers"}).json() == []


def test_html_or_captcha_is_never_indexed(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: html_resp()
    got = run_job(env, "W1")["W1"]
    assert got["status"] == "needs_access" and got["reason"] == "html_returned"
    assert pages_of(env).status_code == 404
    with connect(env.settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_upload_is_indexed_and_delete_clears_the_index(env_of):
    env = env_of([paper(1)])
    r = env.client.post("/api/downloads/upload", params={"source": "openalex", "paper_id": "W1"},
                        content=THREE, headers={"content-type": "application/pdf"})
    assert r.status_code == 200, r.text
    assert r.json()["index_status"] == "indexed" and r.json()["page_count"] == 3
    assert env.client.get("/api/pages/search", params={"q": "monte"}).json()[0]["page"] == 2
    r = env.client.delete("/api/uploads", params={"source": "openalex", "paper_id": "W1"})
    assert r.status_code == 200
    assert env.client.get("/api/pages/search", params={"q": "monte"}).json() == []
    with connect(env.settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_reindex_replaces_pages_only_when_the_file_changes(tmp_path):
    db = tmp_path / "t.sqlite"
    init_db(db)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(THREE)
    b.write_bytes(GOOD)
    with connect(db) as conn:
        conn.execute("INSERT INTO users(id, email, password_hash) VALUES(1, 'u@x.org', 'x')")
        conn.execute("INSERT INTO downloads(user_id, source, paper_id, status)"
                     " VALUES(1, 'openalex', 'W1', 'downloaded_verified')")
        assert index_pdf(conn, 1, "openalex", "W1", a, "sha-a") == "indexed"
        assert index_pdf(conn, 1, "openalex", "W1", a, "sha-a") == "indexed"
        assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM page_search").fetchone()[0] == 3
        index_pdf(conn, 1, "openalex", "W1", b, "sha-b")
        assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM page_search").fetchone()[0] == 1
        assert search_pages(conn, 1, "monte") == []
        assert search_pages(conn, 1, "neutrons")[0]["page"] == 1
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 nothing here")
        assert index_pdf(conn, 1, "openalex", "W1", broken, "sha-c") == "failed"
        assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_accent_and_case_blind_search_and_excerpt(tmp_path):
    db = tmp_path / "t.sqlite"
    init_db(db)
    with connect(db) as conn:
        conn.execute("INSERT INTO users(id, email, password_hash) VALUES(1, 'u@x.org', 'x')")
        text = "Préambule. " + "x " * 200 + "Le réacteur à neutrons rapides est étudié."
        conn.execute("INSERT INTO pages(user_id, source, paper_id, page, label, text, chars,"
                     " sha256) VALUES(1, 'hal', 'H1', 7, 'vii', ?, ?, 's')", (text, len(text)))
        from bayan.textnorm import index_text
        conn.execute("INSERT INTO page_search(user_id, source, paper_id, page, body)"
                     " VALUES(1, 'hal', 'H1', 7, ?)", (index_text(text),))
        hits = search_pages(conn, 1, "REACTEUR rapides")
        assert [(h["page"], h["label"]) for h in hits] == [(7, "vii")]
        assert "réacteur à neutrons" in hits[0]["excerpt"] and hits[0]["excerpt"][0] == "…"
        assert search_pages(conn, 2, "reacteur") == []
        assert search_pages(conn, 1, "reacteur", papers=[]) == []
    assert excerpt("short text", "nothing") == "short text"


def test_old_database_gets_the_new_download_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE downloads(user_id INTEGER NOT NULL, source TEXT NOT NULL,"
                 " paper_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(user_id, source,"
                 " paper_id))")
    conn.commit()
    conn.close()
    init_db(db)
    with connect(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(downloads)")}
    assert {"page_count", "pages_with_text", "index_status"} <= cols


# ---------------------------------------------------------------- resumable jobs

def queue_job(env, ids, age="-1 hour", status="running"):
    """Insert a job as if a server had stopped while running it (no background task)."""
    with connect(env.settings.database_path) as conn:
        for pid in ids:
            conn.execute("INSERT INTO downloads(user_id, source, paper_id, status)"
                         " VALUES(1, 'openalex', ?, ?) ON CONFLICT DO UPDATE SET status = ?",
                         (pid, PENDING, PENDING))
        cur = conn.execute(
            "INSERT INTO jobs(kind, payload, status, updated_at) VALUES('pdf_download', ?, ?,"
            " datetime('now', ?))",
            (json.dumps({"user_id": 1, "run_id": env.run_id,
                         "papers": [["openalex", i] for i in ids]}), status, age))
        conn.commit()
        return cur.lastrowid


def job_status(env, job_id):
    return env.client.get(f"/api/downloads/jobs/{job_id}").json()


def test_interrupted_job_resumes_only_unfinished_papers(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf"),
                    paper(2, oa_url="https://oa.example/p2.pdf")], job_resume_interval=0)
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(THREE)
    env.net.routes["https://oa.example/p2.pdf"] = lambda r: pdf_resp(GOOD)
    job_id = queue_job(env, ["W1", "W2"])
    with connect(env.settings.database_path) as conn:  # W1 finished before the interruption
        conn.execute("UPDATE downloads SET status = 'needs_access', reason = 'http_error'"
                     " WHERE paper_id = 'W1'")
        conn.commit()
    assert resume_stale_jobs(env.settings.database_path, env.fetcher, env.settings) == [job_id]
    job = job_status(env, job_id)
    assert job["status"] == "done" and job["finished"] == 2
    by_id = {p["id"]: p for p in job["papers"]}
    assert by_id["W1"]["status"] == "needs_access"  # kept, not fetched again
    assert by_id["W2"]["status"] == "downloaded_verified"
    assert env.net.urls() == ["https://oa.example/p2.pdf"]
    # Nothing left to resume, and a finished job is never run twice.
    assert resume_stale_jobs(env.settings.database_path, env.fetcher, env.settings) == []
    assert len(env.net.requests) == 1


def test_recent_job_is_left_to_its_running_worker(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")], job_resume_interval=0)
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    job_id = queue_job(env, ["W1"], age="-10 seconds")
    assert resume_stale_jobs(env.settings.database_path, env.fetcher, env.settings) == []
    assert job_status(env, job_id)["status"] == "running" and not env.net.requests


def test_resume_runs_at_startup(env_of, tmp_path):
    from fastapi.testclient import TestClient

    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")], job_resume_interval=0)
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    job_id = queue_job(env, ["W1"], status="pending")
    # A restarted server (same database) with the resume check switched on.
    from bayan.api import create_app
    from bayan.config import Settings

    settings = Settings(**{**env.settings.__dict__, "job_resume_interval": 30.0})
    app = create_app(settings, connector_factory=lambda lib: None, fetcher=env.fetcher)
    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job_status(env, job_id)["status"] != "done":
            time.sleep(0.05)
    job = job_status(env, job_id)
    assert job["status"] == "done" and job["papers"][0]["status"] == "downloaded_verified"


def test_normal_job_does_not_take_a_job_that_is_already_running(env_of):
    from bayan.downloads import run_download_job

    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf")], job_resume_interval=0)
    job_id = queue_job(env, ["W1"], age="-0 seconds", status="running")
    run_download_job(env.settings.database_path, job_id, env.fetcher, env.settings)
    assert not env.net.requests and job_status(env, job_id)["status"] == "running"


def test_stale_part_files_are_removed(tmp_path):
    old, new = tmp_path / "1" / "openalex" / "W1.pdf.aaaa.part", tmp_path / "W2.pdf.bbbb.part"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    keep = tmp_path / "W3.pdf"
    keep.write_bytes(b"%PDF-")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert remove_stale_parts(tmp_path) == 1
    assert not old.exists() and new.exists() and keep.exists()
    assert remove_stale_parts(tmp_path / "missing") == 0
    assert part_files(tmp_path) == [new]


# ---------------------------------------------------------------- manifest

def test_manifest_accounts_for_every_paper_of_the_run(env_of):
    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf"), paper(2), paper(3)])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp(THREE)
    run_job(env, "W1", "W2")
    m = env.client.get("/api/downloads/manifest", params={"run_id": env.run_id}).json()
    assert m["run_id"] == env.run_id and m["total"] == 3 and m["generated_at"].endswith("Z")
    assert m["by_status"] == {"downloaded_verified": 1, "needs_access": 1, "not_requested": 1}
    rows = {r["id"]: r for r in m["papers"]}
    assert set(rows) == {"W1", "W2", "W3"}
    w1 = rows["W1"]
    assert w1["sha256"] and w1["size"] == len(THREE) and w1["page_count"] == 3
    assert w1["source_url"] == "https://oa.example/p1.pdf" and w1["title"].startswith(TITLE)
    assert "path" not in w1
    assert rows["W2"]["reason"] == "no_open_link" and rows["W2"]["reason_text"]
    assert rows["W3"]["status"] == "not_requested"


def test_manifest_csv_and_privacy(env_of):
    from fastapi.testclient import TestClient

    env = env_of([paper(1, oa_url="https://oa.example/p1.pdf", title="=HYPERLINK(x)")])
    env.net.routes["https://oa.example/p1.pdf"] = lambda r: pdf_resp()
    start(env, "W1")
    r = env.client.get("/api/downloads/manifest", params={"run_id": env.run_id, "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "manifest.csv" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1 and rows[0]["id"] == "W1" and rows[0]["title"] == "'=HYPERLINK(x)"
    with TestClient(env.app) as other:
        register(other, "other@example.org")
        assert other.get("/api/downloads/manifest",
                         params={"run_id": env.run_id}).status_code == 404
