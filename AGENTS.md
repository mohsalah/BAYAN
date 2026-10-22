# BAYAN (Brilliant Analysis Yielding Academic kNowledge)

Agentic research assistant for general researchers: choose a research area, libraries, period,
languages, document types and geography; retrieve papers; download chosen PDFs; read them with
page-cited evidence; map concepts across papers; find research gaps and trends; draft proposals.
Full design and milestones: `docs/BUILD_PLAN.md`. Read it before starting any issue.

## Non-negotiable rules

- Never invent papers, DOIs, quotes, results, costs or facility access. Report real counts and
  log shortfalls; never pad.
- Every claim shown to a user links to a paper ID, and to a page/section when it comes from full text.
- Label evidence `full_text` or `abstract`. Keep `not reviewed`, `not reported`, `absent` distinct.
- A research gap is a candidate until counterevidence and a broader search were checked. Never
  infer a gap from counts or missing metadata alone.
- Never bypass access controls, never store library passwords, never scrape behind logins.
  Membership access = the user's own API key, institutional proxy link, or uploaded PDF.
- A file counts as downloaded only after PDF validation and an identity check (title/DOI).
  An HTML page or CAPTCHA is never saved as a PDF.
- Evidence quotes stay in the paper's original language. Translations are marked machine
  translated and are never used as evidence quotes.
- No API keys in code, prompts, logs, error messages or committed files.
- Languages for search/interface: en, ar, fr, zh, de, es only. The translator outputs en or ar only.
- Study-location inference is NOT in v1; geography uses author affiliation / repository country.

## Conventions

- Python >= 3.12, FastAPI, SQLite (FTS5) for now, pydantic models, `httpx` for HTTP.
- Connectors implement `bayan.connectors.base.Connector`; add each to
  `libraries.py` and mark `available` only when it really works.
- Retrieval (search, dedupe, quotas) is deterministic code, not an LLM call.
- Tests must not call live services; use `httpx.MockTransport` and fixtures.

## Commands

- Setup: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Test: `.venv/bin/pytest`
- Lint: `.venv/bin/ruff check .`
- Run: `.venv/bin/uvicorn bayan.api:app --reload`
