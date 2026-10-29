# BAYAN (Brilliant Analysis Yielding Academic kNowledge)

Agentic research assistant for general researchers. See `docs/BUILD_PLAN.md` for the full design,
rules and the 24 milestones. This repository currently contains the skeleton (milestone M1, minus
auth), the OpenAlex connector (M2), the quota logic (M4) and the open connectors arXiv and
Europe PMC with cross-library de-duplication (M5), resumable downloads with a per-page full-text
index and a download manifest (M6), and the reader agent with a page-cited, verified evidence
table (M8). Crossref is not a search source: it is used
automatically to find the DOI of papers found elsewhere that have none (title, year and first
author must all match confidently; at most 50 lookups per run, cached; the run summary reports
`doi_lookup` counts). Semantic Scholar, CORE and Unpaywall are still planned.

## Reference types

Every library's own type labels are mapped to one common set, chosen from a dropdown
(default: Peer-reviewed papers): **Peer-reviewed papers**, **Proceedings papers**, **Theses**,
**Reports** and **Other** (everything that is not one of the four: preprints, books, book
chapters, datasets, standards, manuals, unknown). The library's own label is kept as
`native_type`, and `category_basis` records why a record got its category. When the metadata
does not decide it, the record goes to `Other`; titles and abstracts are never used to guess.

"Peer-reviewed" here only means the source metadata says journal article or review. It is not
verified peer review, and a preprint later published as a journal article is not upgraded
automatically. The run summary reports `by_category` (selected categories, zeros included) and
`filtered_by_type` (scanned records dropped because their category was not selected).

## PDF download (M14)

Tick papers in the results table and press **Download selected PDFs**. A PDF may only come from,
in order: a PDF you uploaded for that paper, the open-access URL the library listed, or an
[Unpaywall](https://unpaywall.org/products/api) lookup by DOI (needs `CONTACT_EMAIL`, sent as the
polite `email` parameter). Nothing else is tried: no scraping behind logins, no CAPTCHA bypass,
no stored library passwords.

A file is saved only if it starts with `%PDF-`, is not empty, is at most 100 MB and parses with
`pypdf`. It is written to a `.part` file and renamed atomically. The size, SHA-256, source URL,
final URL (query strings are never stored), HTTP status, retrieval time and the license/version
Unpaywall gives are recorded. After download the first pages are compared with the title and DOI.

| Status | Meaning |
| --- | --- |
| `downloaded_verified` | The DOI is printed in the file, or the title matches (similarity >= 0.9). |
| `downloaded_unverified_identity` | A valid PDF, but identity could not be confirmed (for example a scan with no text). |
| `identity_mismatch` | Suspected other paper. The file is kept under `_identity_mismatch/`, never served as the paper. |
| `needs_access` | No legal copy was obtained. `reason` is `no_open_link`, `html_returned`, `http_error`, `too_large`, `not_pdf`, `unsafe_url` or `network_error`, with a `detail` sentence. |
| `pending` | Queued, not finished. |

Requests: one at a time per host, at least `BAYAN_DOWNLOAD_HOST_DELAY` seconds (default 1)
between requests to a host, `Retry-After` honoured (capped at 60 s), at most 2 retries. Only public
http(s) URLs are fetched (no credentials, no private/loopback/link-local addresses, checked again on
every redirect, at most 5 redirects). Each user may queue `BAYAN_DOWNLOADS_PER_HOUR` papers per hour
(default 100); beyond that the API answers 429.

Files live under `BAYAN_PDF_ROOT` (default `pdfs/`) as `<user_id>/<source>/<paper_id>.pdf` (ids are
escaped to be filesystem safe) and are served only to the owning signed-in user.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/downloads` | Body `{"run_id": 1, "papers": [{"source": "openalex", "id": "W1"}]}` (1 to 50 papers of your run). Returns 202 with `job_id`, `queued`, and `already_downloaded` (validated papers are not fetched again). |
| `GET /api/downloads/jobs/{job_id}` | Job `status` (`pending`, `running`, `done`, `failed`) and the per-paper records. Poll this. |
| `GET /api/downloads?run_id=1` | Download records for the papers of a run. |
| `POST /api/downloads/upload?source=..&paper_id=..` | Upload your own PDF as the raw body with `Content-Type: application/pdf` (max 100 MB, same validation; 415/413/422 on rejection). |
| `GET /api/downloads/file?source=..&paper_id=..` | The validated PDF; 404 unless it is yours and validated. |

The download job runs in the API process through FastAPI background tasks and the `jobs` table.
It is resumable (M6, below).

## Resumable downloads, page index and manifest (M6)

**Resumable.** A job renews a heartbeat after every paper. A background check in the API process
(every `BAYAN_JOB_RESUME_INTERVAL` seconds, default 60; `0` turns it off) takes over any `pending`
or `running` job whose heartbeat is older than `BAYAN_JOB_STALE_SECONDS` (default 900) and
continues it. Only papers still `pending` are processed: a paper finished before the interruption
keeps its result and is not fetched again. Taking a job is an atomic update, so two workers never
run the same job. Leftover `.part` files older than one hour are deleted by the same check. After
a restart, an interrupted job therefore continues within about 15 minutes.

**Page index.** Every validated PDF (download, upload or manual; not an `identity_mismatch`) is
split into pages with `pypdf` and stored in `pages` (physical page number from 1, the PDF's own page
label when it has one, the extracted text unchanged, the file's SHA-256), with a per-user FTS5 table
`page_search` using the same normalisation as paper search (accents, Arabic marks, Chinese bigrams).
A page with no extractable text is kept with empty text, never dropped. The download record gains
`page_count`, `pages_with_text` and `index_status`:

| `index_status` | Meaning |
| --- | --- |
| `indexed` | At least one page has text. |
| `no_text` | No page has text (scanned or image-only PDF); OCR is needed before it can be read. |
| `failed` | The PDF is valid but its text could not be extracted. |

Re-indexing happens only when the file changes (different SHA-256). Deleting an upload deletes its
pages. At most 2,000 pages per file and 200,000 characters per page are stored.

**Manifest.** `GET /api/downloads/manifest?run_id=1` (or `&format=csv`) lists every paper of the
run, including the ones never requested (`not_requested`), with status, reason and its sentence,
origin, size, SHA-256, source and final URL, HTTP status, retrieval time, license, version, identity
score and basis, and the page-index fields; `by_status` gives the counts. File paths are not shown.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/pages?source=..&paper_id=..` | Index status, a note, and the list of pages (`page`, `label`, `chars`). 404 unless you have a validated PDF of it. |
| `GET /api/pages/text?source=..&paper_id=..&page=3` | The text of one page, its label and the file's SHA-256. |
| `GET /api/pages/search?q=..[&run_id=1][&limit=20]` | Your pages matching every word, best first, with an excerpt of the original text. |
| `GET /api/downloads/manifest?run_id=1[&format=csv]` | The download manifest of a run. |

## Planner agent and editable search profile (M3)

"Suggest a search plan" (under the search form) sends the research-area text, the chosen search
languages and the period to Claude, and shows a draft plan **before anything is searched**:

- **Understood as**: the area restated, so you can check it was understood.
- **Please check**: ambiguities or assumptions (for example an acronym with two meanings).
- **Main query**: a keyword query with `OR`/`AND` and "phrases"; per-language queries for each
  chosen search language other than the main one.
- **Sub-tracks**: 2-6 strands of the area, each with its own query; pick one to search it instead.
- **OpenAlex topics**: the model suggests topic *names* only. Each name is looked up in OpenAlex
  by plain code; only topics OpenAlex returns are shown, marked "exact name match" or "closest
  OpenAlex match for ...". Names with no match are listed as not used. The model never supplies
  an ID.

Every part is editable. "Use this plan" saves your edits and fills the search form (query,
per-language queries, topic filter); you still press Search yourself. The topic filter is
applied by OpenAlex only (`topics.id`); other libraries list it under "Filters a library could
not apply". arXiv now understands `AND`/`OR`/`NOT`, parentheses and quoted phrases.

API: `GET /api/planner` (available?), `POST /api/plans` {area, languages, year_from, year_to,
fresh}, `GET /api/plans`, `GET`/`PUT /api/plans/{id}` (the original suggestion is kept beside
your edits), `GET /api/plans/{id}/profile?track=N` (a profile for `POST /api/search`).

Cost control: the same request reuses the cached answer at no cost ("Suggest again" asks
afresh); token use is recorded in `usage`; `BAYAN_PLANS_PER_HOUR` (default 10) limits new calls.
Settings: `BAYAN_MODEL_PLANNER` (default `claude-sonnet-5-5`), `BAYAN_PLANNER_EFFORT` (default
`medium`). Without `ANTHROPIC_API_KEY` the button is disabled and the API answers 503.

## Reader agent and evidence table (M8)

Tick papers and send them to the reader (`POST /api/reader`, 1 to 20 papers of your run). For each
paper the reader uses, in order: your validated PDF's indexed pages (M6, level `full_text`), else
the library abstract (level `abstract`), else nothing (`not_reviewed`, with the reason, and no
model call). The text goes to Claude (`BAYAN_MODEL_READER`, default `claude-sonnet-5-5`; adaptive
thinking, effort `BAYAN_READER_EFFORT`, structured JSON output, server-side refusal fallback),
which returns items for four dimensions, **methods, data, validation, limitations**: a short
statement, a verbatim quote in the paper's own language, and the page.

**Verifier.** Deterministic code, not a model, checks every item: the quote (20–600 characters, no
ellipsis) must occur on the cited physical page, ignoring only layout (whitespace, line-end
hyphenation, ligatures, typographic quotes and dashes; case and digits must match), and the page
must exist. Items that fail are stored as rejected with a reason and are never shown as evidence.
Per paper each dimension is `reported` (verified items), `not_reported` (the model found nothing)
or `no_verified_evidence` (items proposed, none verified). Reading again replaces the evidence.

**Cost control.** Answers are cached by (prompt version, model, exact text), so the same file is
never sent twice; token use is written to `usage`. At most `BAYAN_READINGS_PER_HOUR` papers per
user per hour (default 20). A document needing more than 5 requests of 400,000 characters is not
read (`not_reviewed`, "too long"). The paper text is treated as data: instructions inside it are
ignored. Without a real `ANTHROPIC_API_KEY` the endpoint answers 503 ("not configured").

| Endpoint | Purpose |
| --- | --- |
| `POST /api/reader` | Body `{"run_id": 1, "papers": [{"source": "openalex", "id": "W1"}]}`. Returns 202 with `job_id`, `model` and a privacy note. |
| `GET /api/reader/jobs/{job_id}` | Job status and per-paper results: `status` (`done`, `not_reviewed`, `failed`, `refused`), `level`, `dimensions`, `verified`/`rejected` counts. |
| `GET /api/evidence?run_id=1[&include_rejected=true]` | Evidence of every paper of the run that was read: dimension, statement, quote, physical page and printed label, level. |

Limit: reader jobs are not resumed after a restart (download jobs are); papers left unread can be
sent again. Usage caps per month are M11.

## Document-type filter and manuals (M21)

**Counts per type.** `GET /api/runs/{id}` returns `type_report`: one row for every reference
type you selected, with its count (0 included) and a sentence, for example
`Theses: 0 found in the chosen libraries (no selected library lists theses).` When a selected
library can list the type but returned none, the sentence says so instead. Which library can
supply a type comes from each connector's `supported_categories` (what its classifier can
return: arXiv only `other`, Europe PMC `peer_reviewed` and `other`, OpenAlex and Crossref all
five); a library without a recorded capability is reported as "not recorded", never assumed.
Manuals are a separate row and are not counted under Other. The results card shows the whole
result set next to how many rows are loaded in the table.

**Filter without searching again.** Chips above the table (type and count) narrow the rows
already in the browser. Ticked papers stay ticked when the filter changes, "select all" applies
to the filtered rows, and the Export box offers *Ticked papers*, *Filtered view* or *All
results*. Export files carry `Document kind` and `Origin` columns.

**Manuals (no web search).** A manual is added by a public PDF link or an uploaded PDF. It is
stored as category `other`, document kind `manual`, source `user`, always labelled
user-supplied, and is visible only to the account that added it. The title is yours and
editable; because of that the file is stored as `downloaded_unverified_identity` (basis
`user_supplied`), never as verified. Links go through the same SSRF-safe fetcher as paper
downloads (public http/https only, every redirect hop checked, query strings not stored), count
towards the hourly download limit, and must be a real PDF (HTML pages, CAPTCHAs and other files
are rejected and nothing is saved). The same file cannot be added twice.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/manuals/url` | `{"url": "https://…/manual.pdf", "title": "optional", "run_id": 1}`. `run_id` omitted = personal collection only. 422 for unsafe URL / not a PDF, 413 too large, 502 server unreachable or HTTP error, 409 duplicate. |
| `POST /api/manuals/upload?title=..&run_id=..` | Raw PDF body with `Content-Type: application/pdf` (415/413/422 on rejection). |
| `GET /api/manuals` | Your manuals collection. |
| `PATCH /api/manuals/{paper_id}` | `{"title": "…"}` edit the title (404 unless it is yours). |
| `POST /api/runs/{run_id}/manuals` | `{"paper_id": "m-…"}` put a collection manual into one of your runs. |

## Geographic filter (M23)

The profile takes `regions` (ids from `GET /api/regions`), `countries` (ISO 3166-1 alpha-2),
`geo_mode` (`include` default, or `exclude`) and `geo_basis` (only `author_affiliation` in v1;
anything else is rejected). Study location is not inferred.

- **Regions** are fixed, sorted country lists in `bayan/regions.py` with a version string
  (`REGIONS_VERSION`). Each country is in exactly one region; the file documents the rule for
  ambiguous cases (Russia is Europe, Turkey and Egypt are Middle East and North Africa, Mexico is
  Latin America and Caribbean, Kazakhstan is Asia) and the few territories left out.
- **Expansion.** Regions plus explicit countries become one sorted, de-duplicated code list at
  search time. The run summary (`summary.geography`) stores the chosen regions, countries, the
  expanded codes, the mode, the basis and the regions version, so a rerun is reproducible.
- **Applying it.** OpenAlex gets `authorships.institutions.country_code:` with the codes joined by
  `|` (include) or prefixed with `!` (exclude). A library that cannot apply it lists `countries` in
  `unapplied_filters`; nothing is post-filtered by guessing. Only countries a record itself
  reports (OpenAlex affiliations) are checked, so excluded records cannot slip through.
- **Unknown location.** A paper with no affiliation country is never dropped and never counted as
  a match. The run shows *Matched (n)*, *Country unknown (n)* and, when relevant, *Outside the
  chosen area (n)* (these were dropped), with the unknown papers in their own expandable list.
  Unknown does not mean excluded. Results have a Geography chip row (regions, countries present,
  Unknown) that filters the loaded results in the browser without a new search.
- **Export.** CSV and Excel have *Affiliation countries*, *Region* and *Geography match* columns;
  missing values read `unknown`.

## Languages (M15)

Exactly six languages are offered anywhere: English, Arabic, French, Chinese, German, Spanish
(`en ar fr zh de es`). Any other code is rejected with a message (`unsupported language(s): ja`).
The **interface language** and the **search languages** are separate choices.

**Interface.** `bayan/locales/<code>.json` holds every visible string, tooltip and aria label
(English is the source of truth; served at `/api/locales/<code>`). The selector next to Theme is
saved in `localStorage` (`bayan.lang`); without a saved choice the browser language is used only if
it is one of the six, otherwise English. `<html lang>` and `dir` follow it. A test fails if a locale
misses or adds a key, or if a placeholder such as `{n}` differs from English.
**The five non-English translations are machine-drafted and await native-speaker review.**
Not translated: paper titles, abstracts and venues (shown as stored), and text the server writes
(API error messages, reference-type explanations in the type report, library warnings, the
Crossref/DOI detail lines). Those stay in English for now.

**Right-to-left and CJK.** Arabic sets `dir="rtl"`; the CSS uses logical properties
(`margin-inline-*`, `inset-inline-*`, `text-align: start`). Titles, abstracts, venues and authors
carry `dir="auto"` and `lang=<paper language>`, so Arabic papers read right-to-left inside an
English page and the other way round. DOIs, URLs, years and e-mail fields are forced
left-to-right. Fonts are system fonts only (no downloads), with Arabic and CJK families listed.
The pure functions are tested under node; layout was **not** checked in a real browser here, so
please look at Arabic and Chinese once by eye.

**Keywords per language.** With more than one search language, the search card shows one editable
keyword box per language (starting as a copy of the research area). They are sent as
`profile.queries_by_language` (keys only from the six codes, each at most 1000 characters, like the
main query). A library that can filter by language (OpenAlex, Europe PMC) runs one query per chosen
language with that language's text and filter, interleaved so one language cannot fill the limit;
de-duplication, the result limit and year quotas apply to the combined stream. A library that cannot
filter by language runs the main query once and the run summary says so. The run summary keeps
`language_queries` (text per language, which libraries split, notes) and the stored request keeps
`queries_by_language`, so a run can be repeated. No model proposes keywords yet (that is M3).

**Display.** Each paper shows a language name, not a code. The Language chip row filters loaded
results in the browser, lists every chosen search language even at 0, lists other languages found,
and always lists "Unknown language" (papers whose library gave no language are shown, not hidden).
The export has a "Language code" and a "Language" (name) column; the Search sheet records the keywords used per language.

**Local full-text table.** `paper_search` (SQLite FTS5) already exists, but nothing queries it yet.
`bayan/textnorm.py` now fills it: text is case-folded, Latin accents are removed (fr/de/es; `ß` →
`ss`), Arabic tashkeel and tatweel are removed and alef variants unified (matching only; the
`papers` table keeps the original text), and Chinese runs are split into overlapping character pairs
(no segmenter dependency). Limits: pairs match across word boundaries, and Arabic prefixes
(such as و "and") are not stripped. Rows stored by earlier versions are not re-indexed
automatically; searching a paper again refreshes its row. No new search backend was added.

## Membership access (M7)

An email address alone gives no access to paid content. A user can instead (a) store their own
publisher API key, (b) save an institutional proxy link template, or (c) upload PDFs they have
the right to use. **No publisher connector uses the stored keys yet**; this milestone only stores
them and exposes `bayan.membership.get_user_key(user_id, publisher)` for a future connector.

**Keys.** Publishers come from `bayan/libraries.py` (libraries with the `api_key` access mode:
`ieee`, `elsevier`, `springer`, `wiley`). Each key is encrypted with Fernet; the Fernet key is
derived from `APP_SECRET_KEY` with HKDF-SHA256 and a fixed context string. If `APP_SECRET_KEY` is
unset, shorter than 16 characters or still a placeholder such as `change-me`, saving a key answers
503 and nothing is stored (there is no plaintext fallback). The full key is never returned by any
endpoint, logged, put in an error message, run summary or export; the status shows saved yes/no,
the last four characters (only for keys of 12+ characters) and the save time. If the secret is
rotated, old keys cannot be decrypted: they show `needs_reentry` and the user pastes the key
again. Rotating the secret does not migrate keys.

**Proxy link.** One template per user, `https` only, no username or password in it, host must
not be localhost, a private/link-local address or a `.local`/`.internal`-style name (no DNS lookup
is made: BAYAN never connects to this host), at most 500 characters, and it must contain `{url}`
(the paper link, percent-encoded) or `{doi}`, for example `https://ezproxy.myuni.edu/login?url={url}`
or `https://go.openathens.net/redirector/myuni.edu?url={url}`. A DOI stands in for `{url}` as
`https://doi.org/<doi>`. BAYAN only builds the link; the user opens it in their browser while
logged in to their institution. It does not log in, does not fetch through the proxy and stores
no library password. When a template is saved, result rows get an *Open via my institution* link.

**Uploads.** The M14/M21 upload routes are reused. `GET /api/uploads` lists your uploaded PDFs
(papers and manuals) and `DELETE /api/uploads?source=..&paper_id=..` removes the file and its
record (a manual is removed entirely). Everything is scoped to the signed-in user: another
user's files cannot be listed, fetched or deleted (404).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/membership` | Status per publisher (`saved`, `needs_reentry`, `last4`, `saved_at`), whether keys can be saved, and the proxy template with an example link. |
| `PUT /api/membership/{publisher}/key` | `{"key": "..."}` (8 to 512 characters, no spaces). 503 without a usable `APP_SECRET_KEY`, 422 for a bad key, 404 for an unknown publisher. |
| `DELETE /api/membership/{publisher}/key` | Remove the stored key. |
| `PUT /api/membership/proxy` / `DELETE /api/membership/proxy` | Save (validated) or remove the proxy template. |
| `POST /api/membership/proxy-link` | `{"doi": "10.1/x"}` or `{"url": "https://..."}`, optional `template` to preview an unsaved one. Returns `link`. |
| `GET /api/uploads`, `DELETE /api/uploads` | Your uploaded PDFs. |

## Translator (M24)

Translates text, a paper's title and abstract, or pages of a PDF you uploaded into **English or
Arabic only** (source: en, ar, fr, zh, de, es, or auto-detect). It is a service in
`bayan/translate.py` behind a small `Engine` interface (`name`, `translate(text, source, target)`),
so the engine can be swapped.

- **Engine:** the default is a LibreTranslate-compatible server, which is free when self-hosted and
  needs no paid key. Set `BAYAN_TRANSLATE_URL` (and optionally `BAYAN_TRANSLATE_API_KEY`). With no
  URL the translator reports **not configured** (HTTP 503, and `status: not_configured` on
  `/api/translate/status`); nothing is ever faked. Keys are never logged or returned.
- **Protected items:** numbers with units, equations (`$...$`, `\(...\)`, `a = b`), math symbols,
  nuclide and chemical names (U-235, 235U, UO2, H2O), DOIs, URLs and citations (`[4]`,
  `(Dupont et al., 2019)`) are replaced by placeholders before translation and restored after. If an
  engine loses a placeholder the result carries a "Low confidence" warning and is not cached.
- **Labelling:** every result says `machine translated` with source language, target language and
  engine, and has `evidence_quote: false`. Translations are not stored on papers and never enter
  evidence packets or prompts; evidence quotes stay in the original language.
- **Glossary:** per-account term pairs (source term -> target term, per language pair), stored in
  SQLite and private to the account. Terms are matched case-insensitively and always rendered the
  same way; changing the glossary invalidates cached translations.
- **PDFs:** only your own validated PDFs, page by page (`pages`, e.g. `1-3,5`, at most 50 pages).
  Scanned pages need OCR, which is an optional extra (`pip install -e ".[ocr]"` plus the Tesseract
  program). Without it you get a clear message instead of a translation. OCR text is marked `ocr`.
- **Limits:** login required; at most `BAYAN_TRANSLATE_MAX_CHARS` (default 20000) characters per
  request and `BAYAN_TRANSLATIONS_PER_HOUR` (default 30) requests per user. Results are cached per
  user by text hash.
- **Web page:** a Translator card shows the original on the left and the translation on the right
  (`dir="auto"`, so Arabic runs right-to-left), plus the glossary editor.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/translate/status` | Engine, `ready` / `not_configured`, targets, limits and OCR availability. |
| `POST /api/translate` | `{"text", "source_language": "auto", "target_language": "en"}`. |
| `POST /api/translate/paper` | `{"source", "id", "target_language"}`: title and abstract of a paper in your runs or your manuals. |
| `POST /api/translate/pdf` | `{"source", "id", "target_language", "pages"}`: pages of your validated PDF, aligned by page. |
| `GET` / `PUT` / `DELETE /api/translate/glossary` | List, save (`source_language`, `target_language`, `source_term`, `target_term`) or remove your terms. |

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then edit
.venv/bin/pytest
.venv/bin/uvicorn bayan.api:app --reload
```

Open http://127.0.0.1:8000/health and http://127.0.0.1:8000/docs.

## Build with Claude through GitHub

1. Create the repo and push (see `docs/BUILD_PLAN.md`, section 9).
2. In Claude Code run `/install-github-app`, or keep `.github/workflows/claude.yml`.
3. Repo Settings -> Secrets: `CLAUDE_CODE_OAUTH_TOKEN` (Claude Max login for building through GitHub; `/install-github-app` creates it), `APP_SECRET_KEY`, `OPENALEX_API_KEY` (optional). Add `ANTHROPIC_API_KEY` later, when the app itself calls Claude for users (M3 onward).
4. `python scripts/create_issues.py` previews one issue per milestone; add `--create` to open them.
5. Comment `@claude implement this` on an issue and review the pull request.
Status: early development.
