# BAYAN (Brilliant Analysis Yielding Academic kNowledge)

Agentic research assistant for general researchers. See `docs/BUILD_PLAN.md` for the full design,
rules and the 24 milestones. This repository currently contains the skeleton (milestone M1, minus
auth), the OpenAlex connector (M2) and the quota logic (M4).

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
