# BAYAN (Brilliant Analysis Yielding Academic kNowledge)

Agentic research assistant for general researchers. See `docs/BUILD_PLAN.md` for the full design,
rules and the 24 milestones. This repository currently contains the skeleton (milestone M1, minus
auth), the OpenAlex connector (M2), the quota logic (M4) and the open connectors arXiv and
Europe PMC with cross-library de-duplication (M5). Crossref is not a search source: it is used
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

The download job runs in the API process through FastAPI background tasks and the `jobs` table; a
restart while it runs leaves its papers `pending`, and selecting them again re-queues them.

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
