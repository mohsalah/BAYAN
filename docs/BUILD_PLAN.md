# BAYAN — Build Plan

Name: BAYAN (Brilliant Analysis Yielding Academic kNowledge); repository `Brilliant-research-agent` to be renamed `BAYAN`. Audience: general researchers, public sign-up.
Reference projects: `~/Downloads/Reactor physics papers` (corpus, SQLite, evidence rules) and Paperpal (research/writing assistant UX; not inspected in detail, review it before the UI milestone).

## 1. What the app does

1. The user states a **research area** (free text) and reviews an editable **search profile** (topics, keywords, sub-tracks).
2. The user picks **libraries** and a **period** (from/to year, optional per-year weights).
3. The app retrieves and de-duplicates papers (results show title, year, language, access status, abstract).
4. The user **ticks the papers they want** and the app downloads the full PDF of each one (legal open copy, the user's own key/proxy, or the user's upload). Papers with no legal copy stay in a "needs access" list; nothing is silently skipped.
5. The agent **reads** the chosen papers and extracts page-cited evidence.
6. The agent builds an interactive **mind map** that links concepts across the chosen papers (section 6a).
7. From the map and the evidence the agent proposes **research-gap candidates** and a **trend report** for the area (section 6b), then **drafts proposals** from the gaps the user accepts.
8. The user can restrict by **document type** (articles, reviews, reports, theses, manuals; section 4b) and by **geography** (region or country; section 4c).
9. Any paper, abstract or page can be sent to the **translator** (any language to English or Arabic; section 4d).
10. All of it works in **English, Arabic, French, Chinese, German and Spanish** (section 4a).

## 2. Rules carried over from the reference project (go in AGENTS.md)

- Never invent papers, DOIs, quotes, results, costs or facility access. Report real counts and shortfalls.
- Every claim shown to the user links to a paper ID, and to a page/section when from full text.
- Label evidence as `full_text` or `abstract`; keep `not reviewed`, `not reported`, `absent` distinct.
- A gap is a **candidate** until counterevidence and a broader search were checked. Never infer a gap from counts or missing metadata alone.
- Never bypass access controls, never store library passwords, never scrape behind logins.
- A downloaded file counts only after PDF validation and identity check (title/DOI on the document).
- No API keys in pages, prompts, logs or the repo.

## 3. Architecture

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI | Reuses the reference code; easy for Claude to extend |
| Agents | Anthropic SDK (or Atomic Agents) with typed input/output schemas | Structured, testable steps |
| Database | SQLite + FTS5 at first, Postgres when hosted for many users | Reference already uses SQLite/FTS5 |
| Jobs | Background worker with a persistent queue table (resumable) | Retrieval/download take long |
| Frontend | Server-rendered pages or small React app | Keep simple first |
| Auth | Email + password or magic link; per-user workspace | Public sign-up |
| Hosting | Render (default), Fly.io as alternative | Deploys from GitHub on merge |

## 4. Source connectors

One interface: `search(profile, years, cursor) -> Iterator[Paper]` and `find_fulltext(paper) -> Optional[Location]`.

| Tier | Source | Access |
|---|---|---|
| Open | OpenAlex (primary index), Crossref, arXiv, Europe PMC/PubMed, Semantic Scholar, CORE, Unpaywall | Free; send a contact email for polite pools; OpenAlex key from env |
| Membership | IEEE Xplore, Elsevier/ScienceDirect, Springer Nature, Wiley | Per-user API key/token (encrypted), institutional proxy link, or user-uploaded PDF |

Membership handling (decided): the user can (a) paste their own API key/token, (b) enter an institutional proxy URL template (EZproxy/OpenAthens) so the app emits links the user opens while logged in, (c) upload PDFs they have rights to, (d) use open sources only. An email address alone gives no access; it is used only for Unpaywall and polite-pool headers.
Uploaded PDFs and keys are private to that user. Metadata and links are the default shared layer; PDFs are not redistributed.

### 4a. Languages (search and reading limited to English, Arabic, French, Chinese, German, Spanish)

The user chooses one or more of the six languages for **search**, and separately for the **interface and agent output**. No other language is offered for search or the interface. (The translator in 4d is the one place that accepts any input language.)

- **Cross-language search:** the planner writes the keyword set in each chosen language (and shows it for editing), because a literal translation often misses the field's real terms. OpenAlex, Crossref, Semantic Scholar and CORE carry multilingual records and a language field; filter on it.
- **Language-specific sources to evaluate** (availability, terms and API access must be verified before building; some may be membership-only or have no public API):
  - Arabic: ASJP (Algerian journals), Arab World Research Source / Al Manhal (membership), Dar Almandumah (membership).
  - French: HAL, Persée, Cairn (partly membership), OpenEdition.
  - Chinese: CNKI and Wanfang (membership, API access uncertain), CSTR/ScienceDB and arXiv/OpenAlex Chinese-language records.
  - German: BASE (Bielefeld), DNB, SSOAR, and publisher repositories.
  - Spanish: SciELO, Redalyc, Dialnet, La Referencia (Latin American repositories).
  Each one becomes a connector only after its access terms are confirmed; unconfirmed ones are listed in the UI as "not available yet", never faked.
- **Reading across languages:** the reader agent extracts evidence in the paper's own language, keeps the original quote and page, and adds a translation into the user's output language. The verifier checks the original-language quote, never the translation.
- **Interface:** right-to-left layout for Arabic, UTF-8 everywhere, CJK-capable fonts and PDF text extraction, per-language full-text search tokenization (Chinese needs a segmenter), and translated UI strings in a single `locales/` folder.
- **Labelling:** each paper shows its language; machine translations are always marked as machine translations.

### 4b. Document types: articles, reviews, reports, theses, manuals

The user ticks which document types to search. Each paper stores a `document_type` and the filter uses it.

| Type | Where it is found | Notes |
|---|---|---|
| Journal articles, conference papers, reviews | OpenAlex, Crossref, Semantic Scholar, Europe PMC, arXiv | Default |
| Technical and institutional **reports** | OpenAlex `report` records, OSTI, INIS, NASA NTRS, CORE, national repositories | Strongest in energy, space and government research |
| **Theses and dissertations** | OpenAlex `dissertation` records, OATD, NDLTD, DART-Europe, EThOS, university repositories; ProQuest is membership-only | Labelled as theses, not peer-reviewed articles |
| **Manuals** (technical, user, code or software manuals) | Mostly not indexed by scholarly databases | Supported through user-supplied URLs and uploaded PDFs, plus an optional web search; clearly labelled as grey literature |

Honest limits: coverage of reports and especially manuals is much thinner than for articles, so the results page shows the counts per type and says when a type returned little or nothing. Peer-review status is shown as "peer-reviewed", "not peer-reviewed" or "unknown", never guessed.

### 4c. Geographic filter

- **Regions:** North America, Latin America and Caribbean, Europe, Africa, Middle East and North Africa, Asia, Oceania. Each region is a fixed country list kept in the repo (ISO country codes), so results are reproducible.
- **Countries:** a multi-select of single countries (for example USA, Canada, Egypt). Regions and countries can be combined, with include or exclude mode.
- **What the filter means (the user picks one, shown clearly):**
  1. *Author affiliation*: the country of the authors' institutions (available in OpenAlex). Default.
  2. *Publisher or repository country*: useful for reports and theses (for example a US national laboratory report).
  3. *Study location* (not in v1): where the work was carried out or what it studied. This is not a reliable metadata field; if added later, the agent infers it from title/abstract/full text, marks it as inferred, and shows the supporting quote.
- **Caveat shown in the UI:** an affiliation filter is not the same as research about that place, and papers with missing affiliation data are listed separately as "location unknown", not dropped silently.
- The trend report (6b) can break results down by country and region.

### 4d. Translator (any language to English or Arabic)

- **Input:** any language, detected automatically (the user can override). **Output:** English or Arabic only.
- **What can be translated:** pasted text; a paper's title and abstract; selected pages of a PDF; or a whole PDF, shown side by side with the original and aligned by page. Scanned PDFs go through OCR first and are marked as OCR text.
- **Quality rules:** keep numbers, units, equations, symbols and citations unchanged; keep a user-editable glossary per research area so technical terms translate consistently; flag low-confidence passages instead of smoothing them over.
- **Labelling:** every translation is marked machine translated. A translation is never used as an evidence quote: evidence quotes always stay in the original language (6, verifier), with the translation shown beside them.
- **Cost:** translation counts against the user's monthly cap (section 7); long documents show an estimated size before they run, and results are cached by text hash.
- **Separate from search:** the search languages are the six in 4a; the translator is the only feature that accepts other input languages.

## 5. Data model (extends the reference schema)

Additional tables for this version: `paper_selections` (user, paper, requested_at, download_status), `languages` on papers and profiles, `concepts` (label, language, normalized_id), `concept_mentions` (concept, paper, page, quote, evidence_level), `concept_links` (concept A, concept B, relation, supporting mentions, paper IDs, strength), `maps` (workspace, paper set, layout state), `trend_reports` (area, period, method, results, caveats), `translations` (source text hash, source language, target language en/ar, text, machine_translated flag, glossary version), `glossaries` (workspace, area, term pairs), `paper_geo` (paper, affiliation countries, repository country, inferred study location with quote, basis), `regions` (name, ISO country list), and `document_type` / `peer_review_status` columns on papers.

`users`, `workspaces`, `search_profiles` (area text, topics, keywords, tracks, years, weights, libraries), `papers` (OpenAlex ID primary, DOI, report numbers), `files` (path, SHA-256, source URL, license, version, identity status), `pages` (+FTS), `topic_groups`/`group_members`, `evidence` (dimension, value, level, page, section, quote, source), `gap_candidates` (claim, supporting IDs, counterevidence IDs, coverage, uncertainty, follow-up query, validation plan), `proposals`, `credentials` (encrypted), `usage` (tokens, cost, month), `jobs`.

## 6. Agents

1. **Planner:** area text -> OpenAlex topic IDs, keyword patterns, sub-tracks. Output is shown for user edit before any search.
2. **Retriever:** deterministic code, not an LLM. Applies quotas, de-dupes, logs shortfalls.
3. **Screener (optional):** relevance and document-type labels from title/abstract, with the reason recorded.
4. **Reader:** per paper, extracts methods/data/validation/limitations with page-cited quotes; marks abstract-only.
5. **Gap analyst:** works on a user-selected group; must run a counterevidence pass and a broader follow-up search before status can leave `unverified candidate`.
6. **Proposal writer:** 1-5 proposals from user-accepted gaps: question/hypothesis, objectives, methods, comparators, validation, uncertainty, resources, risks, deliverables. No invented novelty or citations.
7. **Verifier:** checks every quote exists on the cited page and every cited ID exists in the database; fails the output otherwise.
8. **Concept mapper:** see 6a.
9. **Trend analyst:** see 6b.

### 6a. Mind map linking concepts across papers

- **Extraction:** from the chosen papers' full text (or abstract, labelled), the concept mapper extracts concepts (methods, materials/systems, quantities, data sets, codes, assumptions, results) with the page and quote of each mention.
- **Normalization:** merge synonyms and cross-language equivalents into one node (e.g. the same concept named in Arabic, French and English), keeping every original label.
- **Links:** an edge needs evidence. Allowed relations: `uses`, `extends`, `contradicts`, `validates`, `compared-with`, `depends-on`, `co-occurs-with`. Each edge stores the paper IDs and quotes that support it; an edge with no supporting quote is not drawn. Co-occurrence is shown as a weaker, dashed edge and labelled as co-occurrence only.
- **Interactive view:** nodes sized by number of supporting papers, coloured by concept type or by cluster; click a node or edge to see the papers, pages and quotes; filter by year, language, relation; drag, pin, merge or split nodes; the user edits the map and the edits are kept. Export as PNG, SVG, JSON and Markdown outline.
- **Gap hints from the map (candidates only):** isolated nodes, weakly linked clusters, `contradicts` edges with no resolving paper, concept pairs studied separately but never together in the chosen set, and methods validated on only one system. Each hint is shown with its evidence and the limit that the map covers only the chosen papers, so "never studied together" means "not together in this set" until the broader search is done.

### 6b. Trend research gaps for the area

- **Method:** from the whole retrieved corpus for the area and period (not only the chosen papers), compute publication volume per year, emerging and declining concepts/keywords, citation velocity, new method-system pairings, and growth by language and country. Counts come from the database, not from the model.
- **Interpretation:** the model proposes what the pattern may mean (for example a rising topic with few validation studies) and labels it `candidate trend gap`. Each one lists supporting paper IDs, counterevidence searched, coverage limits (sources, languages and period actually searched) and a follow-up query.
- **Honest limits:** a drop in papers can reflect indexing lag or source coverage; results from membership sources the user did not connect are missing; the report says which sources and languages were covered and which were not.
- **Output:** a dated report with charts, a ranked list of candidate trend gaps, and a button to carry a chosen gap into proposal drafting.

## 7. Free tier and cost control

- Model: Claude. Each user gets a monthly token/cost cap (suggested starting point: a small fixed budget per user per month, tuned after measuring real usage).
- Track usage per call in `usage`; block with a clear message at the cap; admin override.
- Cache reader output per (paper checksum, prompt version) so the same paper is never re-read.
- Use a smaller model for screening and a larger one for gap/proposal steps.
- Optional later: users bring their own key to lift the cap.

## 8. Security and legal

- Encrypt credentials at rest (key from environment, rotated); never log them.
- Per-user data isolation tested; rate limits on search/download endpoints; per-host download throttling and Retry-After.
- SSRF protection on any user-supplied URL (proxy template, PDF URL): public http(s) only, as in the reference `public_url`.
- Terms page: users confirm they have the right to access content they upload or fetch with their keys.
- Privacy: say clearly that text is sent to the Anthropic API for analysis.

## 9. GitHub + Claude in the cloud

1. Create the GitHub repo `BAYAN`; push this folder.
2. Add `AGENTS.md` (rules in section 2 plus commands) and `CLAUDE.md` containing `@AGENTS.md`.
3. In Claude Code run `/install-github-app` (installs the Claude GitHub app and workflow), or add `.github/workflows/claude.yml` using `anthropics/claude-code-action`.
4. Repo Settings -> Secrets: `CLAUDE_CODE_OAUTH_TOKEN` (Max subscription login used by the GitHub Action; `/install-github-app` creates it), `APP_SECRET_KEY`, `OPENALEX_API_KEY` (optional). `ANTHROPIC_API_KEY` is needed only when the app itself calls Claude for users (M3 onward), billed per use; later `DATABASE_URL`.
5. Protect `main`: pull request + passing CI required.
6. Create one GitHub issue per milestone below; comment `@claude implement this` on each. Review each PR before merging.
7. CI (`.github/workflows/ci.yml`): ruff, pytest, and a check that fails on committed secrets.
8. Claude in the cloud edits code and opens PRs; it does not host the running app. Hosting is step 10.
9. Connect Render (or Fly.io) to the repo: auto-deploy `main`, set the same secrets there, add a persistent disk or Postgres.
10. Add a `/health` endpoint and a staging service before production.

## 10. Milestones (one GitHub issue each)

| # | Milestone | Done when |
|---|---|---|
| M1 | Skeleton: FastAPI, SQLite, auth, CI, Dockerfile | Sign-up/login works; CI green; deploys to staging |
| M2 | Connector interface + OpenAlex | Search by profile and years returns real papers; shortfalls logged |
| M3 | Planner agent + editable search profile | Area text produces a reviewable profile |
| M4 | Period and quotas (port `year_quotas.json` logic) | Quota counts match largest-remainder allocation in tests |
| M5 | Other open connectors + de-duplication | Same work from two sources stored once |
| M6 | Downloader + PDF validation + page index | Resumable; HTML/CAPTCHA never saved as PDF; manifest complete |
| M7 | Membership settings (keys, proxy, upload) | Keys encrypted; proxy links generated; uploads private per user |
| M8 | Reader agent + evidence table | Every row has quote + page; verifier rejects fake quotes |
| M9 | Groups, gap analyst, counterevidence pass | Gaps stay `unverified candidate` until checks recorded |
| M10 | Proposal writer (1-5) | Each proposal links to source IDs; no invented citations |
| M11 | Usage metering + monthly cap | Cap enforced; usage visible to the user |
| M12 | Export (CSV/Excel/BibTeX), UI polish, Paperpal-style review of UX | Selected records and evidence export correctly |
| M13 | Security review, terms/privacy, production launch | Checklist in section 8 complete |
| M14 | Paper selection and on-demand PDF download | User ticks papers; each gets a validated PDF or a visible "needs access" reason |
| M15 | Languages: six-language search profile, language field, per-language indexing, RTL and CJK support | Search and display work in en/ar/fr/zh/de/es; no other search languages offered |
| M16 | Language-specific connectors (only those with confirmed access terms) | Each connector documents its terms; unconfirmed ones show "not available yet" |
| M17 | Concept extraction and cross-language normalization | Every concept mention has paper, page and quote; synonyms across languages merge |
| M18 | Interactive mind map (evidence-backed edges, edit, export) | Edges without a supporting quote are not drawn; user edits persist |
| M19 | Map-based gap hints | Hints carry evidence and the "chosen set only" caveat |
| M20 | Trend analysis and trend-gap report | Counts reproducible from the database; coverage limits listed; gaps stay candidates |

| M21 | Document-type filter (articles, reviews, reports, theses, manuals) | Counts per type shown; empty types stated, not hidden; manuals via URL/upload |
| M22 | Report and thesis connectors (OSTI, INIS, NTRS, OATD, NDLTD, DART-Europe, EThOS where terms allow) | Each documents its terms; unconfirmed ones show "not available yet" |
| M23 | Geographic filter (regions, countries, basis selector, unknown-location bucket) | Region lists tested; results reproducible; unknown location listed separately |
| M24 | Translator (any language to English or Arabic) with glossary, OCR and side-by-side view | Numbers/units/equations preserved in tests; translations marked and never used as evidence quotes |

Suggested order after M8: M14, M15, M17, M18, M9 (gap analyst can then use the map), M19, M20, M16, M10-M13.

## 11. Testing

- Unit tests for quotas, de-dupe, PDF validation, SSRF guard, cap enforcement.
- Connector tests against recorded responses (no live calls in CI).
- Agent tests with fixed fixtures: the verifier must reject fabricated quotes and unknown IDs.
- A small end-to-end test: area -> profile -> search -> read -> gap -> proposal on a 20-paper fixture.

## 12. Open items to confirm during build

- Final app name and domain.
- Exact monthly cap per free user.
- Hosting: Render assumed (cheapest workable); revisit if storage for PDFs grows.
- Which membership publishers to support first (suggest Elsevier and IEEE, then Springer, Wiley).
- Which language-specific sources really offer legal API or bulk access (CNKI, Wanfang, Al Manhal, Dar Almandumah, Cairn, Dialnet are the uncertain ones).
- DECIDED: manuals come from user uploads and pasted URLs at launch; a web-search connector (allowlisted sites, labelled grey literature) is added later only if users ask.
- DECIDED: study-location inference is not in v1. The geographic filter uses author affiliation (default) and publisher/repository country only; inference may follow later as an optional, always-labelled filter.
- Default geographic basis: author affiliation.
- Mind-map front-end library (e.g. a graph library such as Cytoscape.js or D3) and the maximum number of papers per map before it becomes unreadable.
- Whether the reader/translator should use one model for all six languages or a stronger one for Arabic and Chinese after testing.
- Paperpal features worth copying; to be decided after reviewing the site together.
