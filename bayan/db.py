"""SQLite storage. Starts from the reference schema; later milestones add tables."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  source TEXT NOT NULL, id TEXT NOT NULL, doi TEXT, title TEXT, abstract TEXT, year INTEGER,
  document_type TEXT, language TEXT, venue TEXT, first_author TEXT, countries TEXT,
  citations INTEGER, is_oa INTEGER, oa_url TEXT, landing_url TEXT, keywords TEXT, topic TEXT,
  category TEXT, native_type TEXT, category_basis TEXT, document_kind TEXT,
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
CREATE TABLE IF NOT EXISTS analysis_requests(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, run_id INTEGER, request TEXT NOT NULL, prompt TEXT NOT NULL,
  packet TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
  error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS doi_lookup_cache(
  cache_key TEXT PRIMARY KEY, doi TEXT NOT NULL DEFAULT '', score REAL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS downloads(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, detail TEXT, origin TEXT, path TEXT, size INTEGER, sha256 TEXT,
  source_url TEXT, final_url TEXT, http_status INTEGER, retrieved_at TEXT,
  license TEXT, version TEXT, identity_score REAL, identity_basis TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, source, paper_id)
);
CREATE TABLE IF NOT EXISTS pages(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, page INTEGER NOT NULL, label TEXT,
  text TEXT NOT NULL, chars INTEGER NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY(user_id, source, paper_id, page)
);
CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(
  user_id UNINDEXED, source UNINDEXED, paper_id UNINDEXED, page UNINDEXED, body
);
CREATE TABLE IF NOT EXISTS download_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS manuals(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  paper_id TEXT NOT NULL, origin TEXT NOT NULL, source_url TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, paper_id)
);
CREATE TABLE IF NOT EXISTS membership_keys(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  publisher TEXT NOT NULL, ciphertext TEXT NOT NULL, saved_at TEXT NOT NULL,
  PRIMARY KEY(user_id, publisher)
);
CREATE TABLE IF NOT EXISTS membership_proxy(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  template TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_language TEXT NOT NULL, target_language TEXT NOT NULL,
  term_key TEXT NOT NULL, source_term TEXT NOT NULL, target_term TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, source_language, target_language, term_key)
);
CREATE TABLE IF NOT EXISTS translation_cache(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  text_hash TEXT NOT NULL, source_language TEXT NOT NULL, target_language TEXT NOT NULL,
  engine TEXT NOT NULL, glossary_version TEXT NOT NULL, origin TEXT NOT NULL,
  result TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, text_hash, source_language, target_language, engine, glossary_version,
              origin)
);
CREATE TABLE IF NOT EXISTS translate_usage(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  chars INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reader_cache(
  cache_key TEXT PRIMARY KEY, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  output TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reader_results(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, job_id INTEGER, status TEXT NOT NULL,
  level TEXT, detail TEXT, dimensions TEXT, verified INTEGER, rejected INTEGER, parts INTEGER,
  cached_parts INTEGER, model TEXT, prompt_version TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, source, paper_id)
);
CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, dimension TEXT NOT NULL, statement TEXT NOT NULL,
  quote TEXT NOT NULL, page INTEGER, page_label TEXT, level TEXT NOT NULL,
  verified INTEGER NOT NULL, reject_reason TEXT, model TEXT, prompt_version TEXT,
  file_sha256 TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS evidence_paper ON evidence(user_id, source, paper_id);
CREATE TABLE IF NOT EXISTS reader_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS concept_results(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, job_id INTEGER, status TEXT NOT NULL,
  level TEXT, detail TEXT, concepts INTEGER, verified INTEGER, rejected INTEGER, parts INTEGER,
  cached_parts INTEGER, model TEXT, prompt_version TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, source, paper_id)
);
CREATE TABLE IF NOT EXISTS concept_mentions(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL, paper_id TEXT NOT NULL, concept_key TEXT NOT NULL, type TEXT NOT NULL,
  label TEXT NOT NULL, name_en TEXT NOT NULL, language TEXT, quote TEXT NOT NULL, page INTEGER,
  page_label TEXT, level TEXT NOT NULL, verified INTEGER NOT NULL, reject_reason TEXT,
  model TEXT, prompt_version TEXT, file_sha256 TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS concept_mentions_paper ON concept_mentions(user_id, source, paper_id);
CREATE TABLE IF NOT EXISTS concept_merges(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL, concept_key TEXT NOT NULL, canonical_key TEXT NOT NULL,
  origin TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, run_id, concept_key)
);
CREATE TABLE IF NOT EXISTS concept_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS idea_runs(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL, job_id INTEGER, status TEXT NOT NULL, detail TEXT,
  background TEXT NOT NULL, request TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '[]',
  result TEXT, model TEXT, calls INTEGER NOT NULL DEFAULT 0,
  cached_calls INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idea_runs_user ON idea_runs(user_id, run_id, created_at);
CREATE TABLE IF NOT EXISTS idea_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topic_maps(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL, job_id INTEGER, status TEXT NOT NULL, detail TEXT,
  scope TEXT NOT NULL, request TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '[]', result TEXT,
  model TEXT, calls INTEGER NOT NULL DEFAULT 0, cached_calls INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS topic_maps_user ON topic_maps(user_id, run_id, created_at);
CREATE TABLE IF NOT EXISTS topic_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  area TEXT NOT NULL, request TEXT NOT NULL, suggested TEXT NOT NULL, plan TEXT NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, model TEXT,
  prompt_version TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS plans_user ON plans(user_id, created_at);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, user_id TEXT, month TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may open, use and close a request's connection on different worker threads (one
    # after another, never at the same time), which SQLite refuses unless this check is off.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added after the first release; init_db adds them to an existing file in place.
COLUMNS_ADDED = {
    "papers": (("category", "TEXT"), ("native_type", "TEXT"), ("category_basis", "TEXT"),
               ("document_kind", "TEXT")),
    "downloads": (("page_count", "INTEGER"), ("pages_with_text", "INTEGER"),
                  ("index_status", "TEXT")),
}


def init_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in COLUMNS_ADDED.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns:
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
