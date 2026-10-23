"""SQLite storage. Starts from the reference schema; later milestones add tables."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  source TEXT NOT NULL, id TEXT NOT NULL, doi TEXT, title TEXT, abstract TEXT, year INTEGER,
  document_type TEXT, language TEXT, venue TEXT, first_author TEXT, countries TEXT,
  citations INTEGER, is_oa INTEGER, oa_url TEXT, landing_url TEXT, keywords TEXT, topic TEXT,
  PRIMARY KEY(source, id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS paper_search USING fts5(
  source UNINDEXED, id UNINDEXED, title, abstract, keywords, topic
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_runs(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  request TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS run_papers(
  run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, rank INTEGER NOT NULL,
  PRIMARY KEY(run_id, source, paper_id)
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
  error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS doi_lookup_cache(
  cache_key TEXT PRIMARY KEY, doi TEXT NOT NULL DEFAULT '', score REAL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, user_id TEXT, month TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
