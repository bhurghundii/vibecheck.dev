# AGENTS.md

Notes for coding agents (and people) working in this repo. They cover what the code assumes, so you don't have to rediscover it. User-facing docs live in `docs/` and the README; this file is about working on the code.

## Contributing

AI-written PRs are welcome here (see the README). This repo runs vibecheck on its own PRs: the `grill` job comments a question link, and the PR's human author has to answer it before `vibecheck/verdict` goes green. PRs from forks are gated too: the workflow runs on `pull_request_target`, so a fork PR gets the repo's secrets and a question like any other PR. That is only safe because the workflow fetches the diff from the GitHub compare API and never checks out or runs the PR's code; keep it that way (see `docs/wire-it-up.md`).

## What vibecheck is

A self-hosted FastAPI service that quizzes a PR's author on their own diff before it merges. A GitHub Action (`.github/workflows/vibecheck.yml`) POSTs the PR diff to the server. The server reads the diff once to form a blind interpretation, writes a short sheet of questions, and returns a link. The author answers one screen at a time, Claude grades each answer, and the server posts the verdict as a `vibecheck/verdict` commit status. There is no GitHub App, OAuth or webhooks: only a bearer token between the Action and the server.

## Layout

| Path | What lives there |
|---|---|
| `app/main.py` | App setup, lifespan (migrations, pool, grader, status publisher), the API endpoints (`POST /sessions`, `GET /verdict`, `POST /pre-commit/sessions`, `/healthz`) |
| `app/web/routes.py`, `app/web/templates/` | The unauthenticated walkthrough at `/s/{token}`: answer, skip, explain, restart, result page |
| `app/db/` | Raw-SQL data access: `sessions.py` (create/lookup), `exam.py` (per-question state, verdict), `verdict.py`, `migrations.py` (the runner) |
| `migrations/V<n>__*.sql` | Schema. Applied automatically at startup, in numeric order, under an advisory lock |
| `app/ai/grading.py` | `RealGrader` (Anthropic Messages API over async httpx) and `FakeGrader` |
| `app/ai/evals/` | Grader eval cases (`make eval`). Recorded model calls go to `app/ai/evals/cassettes/`, and none are committed, so the first run needs `MODEL_API_KEY` |
| `app/evaluator.py` | `EVALUATOR` checks that reject a diff before any question is asked (size, changed lines) |
| `app/github/commit_status.py` | Posts `vibecheck/verdict`; a `NullStatusPublisher` when `GITHUB_STATUS_TOKEN` is unset |
| `app/config.py` | Every setting, read from the environment (documented in `docs/configuration.md` and `.env.example`) |
| `.github/workflows/vibecheck.yml` | The reference client of the API, and the repo's own merge gate |
| `.github/workflows/ci.yml` | The repo's test suite in CI |
| `docs/` + `mkdocs.yml` | The docs site |
| `scripts/seed.py` | Inserts a realistic session and prints its local URL (`make seed`) |
| `scripts/pre_commit_hook.py`, `.pre-commit-hooks.yaml` | The git pre-commit hook and its pre-commit.com manifest (see Pre-commit sessions) |

## Build, test, run

CI's test job runs these (CI also builds the Docker image). Run them before calling anything done:

```sh
uv lock --check
uv run --with ruff ruff check .
uv run pytest -q
```

- Python version comes from `.python-version`; dependencies from `uv.lock`. A `pyproject.toml` change needs `uv lock`, or the first gate fails.
- Tests start their own Postgres 16 with Testcontainers, so they need a working Docker API. On Podman, point `DOCKER_HOST` at the Podman socket (e.g. `unix:///run/user/$UID/podman/podman.sock`). If the Ryuk reaper can't connect, set `TESTCONTAINERS_RYUK_DISABLED=true`. "Could not find a valid Docker environment" or a refused connection at fixture setup is this, not a code failure.
- `make dev` runs the app against Compose Postgres. The database is published on host port **5433**, not 5432.
- `FAKE_GRADER=true` replaces the model. It passes any answer containing `looks-good`. Tests never call the real API.

## Conventions

- **Data access.** Raw SQL through psycopg3, async, with no ORM. Schema changes are a new `migrations/V<n>__*.sql`; never edit an applied migration.
- **Idempotency.** Create sessions with one `INSERT … ON CONFLICT … RETURNING *`, falling back to a `SELECT` only when the insert was skipped. Never select-then-insert: concurrent workflow runs would duplicate sessions. Look up an existing session *before* calling the grader, so a re-run doesn't pay for model calls it then throws away.
- **Outbound HTTP** from the server uses async httpx.
- **Comments explain why**, in full sentences, often at length. Module docstrings state purpose and invariants. Don't narrate what the next line does.
- **Tests** follow `tests/README.md`:
  - layout mirrors `app/`; endpoint tests go in `tests/api/`;
  - module docstrings, `-> None`, and test names that state the behaviour;
  - a real Postgres for anything touching SQL;
  - tokens from `secrets.token_urlsafe`.
- **Style.** `from __future__ import annotations` at the top of every module, except `app/web/__init__.py` and the empty `__init__.py` files. Ruff runs with line length 100, rules E/F/I/UP/B, E501 ignored.
- **User-visible changes** get an entry under `## Unreleased` in `CHANGELOG.md` (Added / Changed / Fixed). A feature also gets a page in `docs/` and an entry in the `mkdocs.yml` nav.
- No new runtime or dev dependency without a reason the maintainer agrees with.

## Invariants: don't break these

- **The token in `/s/{token}` is the credential.** It never reaches logs (see `redact_session_token`). Build links from `VIBECHECK_BASE_URL`, never from the Host header.
- **The pages make no outbound requests.** The CSP is `default-src 'none'` plus inline styles and same-origin script and images. The only JavaScript is `app/web/static/nopaste.js`, and every page works without it. Keep it that way.
- **The PR job never blocks on an outage.** The workflow's `grill` job goes red for exactly one reason: an `EVALUATOR` rejection (422). Anything else is a warning, and the job passes. The gate is the `vibecheck/verdict` status, not the job.
- **Request bodies are capped by bytes actually received** (`app/middleware.py`, 413), separately from `MAX_DIFF_BYTES` (a 422 rejection with a reason).
- **Diffs are stored in plaintext.** Treat the database like source code.

## Gotchas

- **Old project name.** The project used to be called "grumpy". `GRUMPY_*` env vars and secrets are still accepted as aliases (`app/config.py`, `vibecheck.yml`), and older CHANGELOG entries say `grumpy/verdict`. Keep the aliases; don't "fix" the history.
- **`INTEGRATION.md` no longer exists.** Comments in `.env.example` and `vibecheck.yml` still point to it. Its content now lives in `docs/wire-it-up.md`.
- **Files that aren't ignored.** `.pytest-results/` and `coverage.xml` are not in `.gitignore`. Don't commit them; stage explicit paths rather than `git add -A`.
- **Two Python versions in play.** `.python-version` pins 3.14, but ruff's `target-version` is `py312` and `requires-python` is `>=3.12`. Don't write syntax newer than 3.12.
- **Coverage tooling isn't a dependency.** Measure coverage without adding it: `uv run --with pytest-cov pytest --cov=app --cov=scripts --cov-branch`.

## Pre-commit sessions

A second way into the same walkthrough: an author can be quizzed on their **staged** changes at `git commit`, before any PR exists.

- **Two kinds of session in one table.** V2 adds `source` (`'pr'` | `'pre_commit'`) and `diff_sha256`. A CHECK makes a `pr` row carry `pr_number`/`head_sha`/`base_sha`, and a `pre_commit` row carry `diff_sha256`. Pre-commit identity is `(repo, sha256(diff))`, enforced by a partial unique index. Its `ON CONFLICT` must repeat the `WHERE source = 'pre_commit'` predicate, or Postgres can't infer the index. The V1 index on `(repo, pr_number, head_sha)` is unchanged: pre-commit rows have NULLs there, and NULLs never collide.
- **`POST /pre-commit/sessions {repo, diff}`** has the same auth, allow-list and evaluator as `POST /sessions`, and the same lookup-before-grader rule. It returns `{session_url, status, question}`. The hook reads `status` from the re-POST, so there is no separate verdict endpoint.
- **No commit status for pre-commit sessions.** There is no commit yet. Every publish in `routes.py` goes through `_publish()`, which returns early for them. Expired and restart-off messages, and the page header, have pre-commit wording.
- **`scripts/pre_commit_hook.py`** is stdlib-only Python, shipped both as a plain `.git/hooks/pre-commit` copy and through `.pre-commit-hooks.yaml` (`language: script`). It must stay stdlib-only and executable.
  - **It fails open.** Only `pending`, `failed` and a 422 with `rejection` block the commit. Everything else warns and exits 0: unset config, an unreachable or slow server, any other status, a malformed reply, git failing, or a bug in the hook.
  - **How fail-open is layered.** The request's except clause warns "could not reach the vibecheck server". A catch-all in `main()` warns "the hook failed unexpectedly". Tests for a specific failure assert the specific text, because "a warning appeared" can't tell which layer handled it.
  - **Git output is decoded with `errors="replace"`.** A non-UTF-8 file is still sent and quizzed, not crashed on.
  - **The timeout is 120 s per network operation, not a total deadline.**
- **Tests.** `tests/api/test_pre_commit.py` covers the endpoint, `tests/db/test_pre_commit_schema.py` the CHECKs, and `tests/scripts/test_pre_commit_hook.py` the hook. The hook tests run in-process against a localhost stub server or raw socket, plus a real throwaway git repo for the staged-diff tests.
- User docs: `docs/pre-commit.md`.
