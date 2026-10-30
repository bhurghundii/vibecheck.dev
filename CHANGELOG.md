# Changelog

## Unreleased

**Changed**

- The server's dependencies moved to a `server` dependency group so the
  package itself only needs `httpx`. `uv sync` still installs everything; if
  you build your own image, install with `uv sync --frozen --no-dev
  --no-install-project` as the Dockerfile now does. The package is now named
  `vibecheck-dev`.
- Fork PRs are gated. The workflow now runs on `pull_request_target`
  instead of `pull_request`, so fork PRs get the secrets and are asked a
  question like any other PR; before, they were skipped. The diff comes
  from the GitHub compare API instead of a checkout, so the workflow never
  runs code from the PR. If you copied the workflow, copy it again, and
  don't add a checkout of the PR head to it.
- A diff over `MAX_DIFF_BYTES` is now a `422` rejection that fails the
  `grill` job, not a `413`. The non-blocking job turned the `413` into a
  warning, so the largest PRs were the only ones that skipped grumpy.
- The workflow's job is now called `grill` (was `ask`) and is non-blocking:
  if grumpy is unreachable or rejects the request, the job warns and still
  passes instead of failing the PR. The gate, if you want one, is still the
  `grumpy/verdict` commit status. If branch protection requires `ask`,
  remove it or switch it to `grumpy/verdict`.

**Added**

- A local `vibecheck` command: the same walkthrough in your terminal, against
  your branch (`vibecheck`), a PR (`vibecheck --pr 123`) or a diff file, with
  your own `ANTHROPIC_API_KEY` and no server. Practice only; it stores nothing
  and posts no status. Install with `brew install bhurghundii/tap/vibecheck`
  or `pipx install git+https://github.com/bhurghundii/vibecheck.dev`.
- The release workflow regenerates the Homebrew formula on each tag and pushes
  it to `bhurghundii/homebrew-tap` when `HOMEBREW_TAP_TOKEN` is set.
- `EVALUATOR`, a JSON config of checks that reject a PR before grumpy asks
  anything. The first is `size`: more than 1000 changed lines (added +
  removed, outside lockfiles by default) and `POST /sessions` returns `422`
  with the reason instead of creating a session. The `grill` job comments
  the reason on the PR and fails, the one case where it goes red. Set
  `{"size": {"max_changed_lines": 0}}` to turn it off.

- Per-repo grading strictness. `GRADING_STRICTNESS` sets the deployment-wide
  level (`lenient`, `standard` or `strict`) and `GRUMPY_REPO_STRICTNESS`
  overrides it per repo, so a scratch repo can accept rough answers while a
  critical one demands complete ones. `standard` is the existing grading
  rule, unchanged, so nothing changes unless you set either variable. Each
  answer records the level it was graded at, in `answers.strictness`.

## Unreleased — public release preparation

Findings from a pre-release audit, fixed.

**Fixed**

- A startup config error no longer prints part of a secret. When a
  cross-field check failed (e.g. `MAX_REQUEST_BODY_BYTES` below
  `MAX_DIFF_BYTES`), the exit message dumped the input values, truncated
  but keeping the tail of the last one, which could be `MODEL_API_KEY`,
  into the deployment's log. `DATABASE_URL`, `GRUMPY_TOKEN`,
  `GITHUB_STATUS_TOKEN` and `MODEL_API_KEY` are now also masked in any
  repr of the settings.

- A session that expired unanswered could never be answered. The expired
  page said to re-run the check, but the re-run's `POST /sessions` for the
  same head SHA handed back the same expired session, so `grumpy/verdict`
  stayed pending until the author pushed a new commit. The re-run now
  re-issues that session a fresh link and expiry, and the workflow comments
  it on the PR; answers and the attempt budget carry over.
- The Action sent the wrong diff. `git diff BASE HEAD` is a two-dot,
  tree-to-tree comparison, and `pull_request.base.sha` is the base branch
  tip at event time rather than the merge base — so once anything landed on
  the base branch, the diff also contained the reverse of those commits.
  Authors were shown a phantom revert of a colleague's work and graded on
  it. Now `git diff BASE...HEAD`.
- Fork pull requests failed closed. GitHub withholds secrets from
  fork-triggered runs, so the token was empty, the API returned 401, and the
  gate blocked an outside contributor with an error they could not fix. The
  gate now detects an unconfigured run and skips with an explanation.
- Session tokens were written to grumpy's own logs. The `/s/{token}` path
  segment is the credential; `log_requests` logged the raw path and
  uvicorn's access log repeated it. Both are redacted or disabled now, and
  `httpx`'s request logging is quieted for the same reason.
- Requesting a tutorial breakdown on the last remaining attempt charged an
  Anthropic call, locked the session as `FAILED`, and never displayed the
  breakdown. The final attempt is now reserved for answering.
- `INTEGRATION.md` did not work as written: it named a `GRUMPY_URL` secret
  the workflow never read, and documented a `"PENDING"` response value the
  API returns as `"pending"`.
- Transient model-API failures (429, 529, 5xx, timeouts) surfaced to the
  developer as "grading failed, please try again". They are now retried with
  exponential backoff, honouring `Retry-After`.
- `INTEGRATION.md` documented a verdict-polling loop that no longer matched
  the shipped workflow. It now describes the workflow's actual ten-minute
  budget and states the trade-off (runner minutes versus a check that
  resolves itself) rather than contradicting it.
- `MAX_DIFF_BYTES` defaulted to 1 MB, above the model's context window, so
  oversized diffs failed at grading time with no usable message instead of
  at session creation. Lowered to 400 KB.
- The pass page hotlinked a copyrighted image from a third-party CDN. It is
  an inline SVG now, which also let the CSP drop `img-src` entirely.
- A grading or tutorial failure left no trace. The developer is shown a
  deliberately vague "please try again", and all three `GradingError`
  handlers discarded the exception without logging it, so the cause was
  recorded nowhere and a truncated response was indistinguishable from an
  expired API key. They now log the traceback against the session's repo
  and head SHA.
- Tutorial generation could exhaust `max_tokens`. Thinking is on by default
  on `claude-opus-5` and shares that budget, and a tutorial — up to six
  steps of a paragraph or two — is much longer than a verdict, so it hit the
  4096 cap first and came back as truncated JSON. The parser reported that
  as malformed model output, blaming the model for a budget problem. The cap
  is 16000 now, and a `max_tokens` stop reason says so instead of failing
  later as a parse error.

**Added**

- MIT `LICENSE` — the repository previously granted no rights at all.
- `SECURITY.md`, with the threat model and the known, accepted trade-offs.
- A `ci` workflow running lint, the test suite, `uv lock --check`, and a
  Docker build. Nothing ran the tests on a PR before.
- Ruff configuration.
- A paste guard on the answer and explain-back textareas, suggested by
  csgrant. A small same-origin script (`app/static/nopaste.js`) blocks
  pasting and dropping text in. That's friction, not enforcement, so whether
  it was running is recorded per submission (`answers.js_active`,
  `tutorials.explanation_js_active`, migration V6) and logged as
  `outcome: no_js` when it wasn't — never used to fail an answer. The CSP
  gains `script-src 'self'`; inline script is still refused.

**Changed**

- The merge gate is now a `grumpy/verdict` commit status that grumpy posts
  itself: pending when the session is created, success or failure as soon as
  the answer is graded. Opt in with the new `GITHUB_STATUS_TOKEN` (and
  `GITHUB_API_URL` for GitHub Enterprise Server). This replaces the
  workflow's ten-minute polling loop, which failed nearly every PR: authors
  rarely answered within ten minutes, and couldn't have, because the session
  link was only commented after the loop had finished. The workflow's job is
  now called `ask`. It opens the session, comments the link, checks the
  status landed and exits in seconds. Point branch protection at
  `grumpy/verdict` instead of the old `grumpy` job.
- Python pinned to 3.14 via `.python-version`, matching the Dockerfile, so
  local development and CI stop running a different interpreter from the
  one the image ships. Suite verified green on 3.14.
- README corrected in place rather than rewritten: the three-dot diff range,
  the reserved final attempt, `MAX_DIFF_BYTES`, the exact CSP, and the fact
  that grumpy now redacts session tokens from its own logs.
- `INTEGRATION.md` linked to `README.md#before-you-deploy-publicly`, an
  anchor that no longer exists — the heading is now "Self-hosting: before
  you make it public".

## Development phases

grumpy was built in numbered phases; the README used to narrate them.

### Phase 4 — real grading

Replaced `FakeGrader` with `RealGrader`: two sequential calls to the
Anthropic Messages API over async httpx, structured outputs plus defensive
parsing. Interpretation runs blind (diff only, no answer) so the comparison
call cannot rationalise toward the answer. Added `model`/`prompt_version`
recording, the persisted and displayed `reasoning`, the eval set in
`evals/cases/`, `MEANIEMODE`, markdown rendering with `nh3` sanitization,
answer retries under `MAX_SESSION_ATTEMPTS`, and the tutorial-breakdown
flow with its step-through walkthrough.

### Phase 3 — the developer-facing loop

`GET /s/{token}` and `POST /s/{token}/answer`, unauthenticated by design
with the token as the credential. Server-rendered Jinja2, no JavaScript.
Post/redirect/get so a refresh cannot resubmit. `FakeGrader` stood in for a
real model so the loop could be proved end to end for free.

### Phase 2 — the Action-facing API

`POST /sessions` and `GET /verdict` behind bearer auth. Idempotent session
creation on `(repo, pr_number, head_sha)` via `INSERT … ON CONFLICT DO
NOTHING RETURNING *`, because two concurrent Action runs on one SHA are the
normal case. The four-state verdict, keeping `UNKNOWN` distinct from
`PENDING` so a force-push creates a fresh session instead of blocking on a
stale one.

### Phase 1 — the skeleton

FastAPI, uvicorn, `uv`, psycopg3 with an async pool and raw SQL, Postgres
16. Startup config validation, structured JSON logging, the advisory-locked
migration runner, and `/healthz`.
