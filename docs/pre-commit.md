# Check commits before they land

The PR gate asks its questions once a pull request is open. The pre-commit hook asks them earlier: on `git commit`, about exactly what you have staged. It's the same walkthrough on the same server. The difference is that it is keyed by the staged diff instead of by a PR and head commit.

## How it works

1. You run `git commit`. The hook sends the staged diff (`git diff --cached`) to `POST /pre-commit/sessions` on your vibecheck server.
2. The first time the server sees that diff, it writes the questions. The hook then prints the link and **blocks the commit**.
3. You answer on the web page, the same as for a PR.
4. You run the same `git commit` again. The staged content hasn't changed, so the hook finds the same session. It has passed, so the commit goes through.

If you change what is staged, that is a different diff, and you get a new session. Running the commit again without changing anything reuses the session and costs no model calls.

The hook never posts a commit status to GitHub. There is no commit yet to post one to.

## Install

You need a running vibecheck server (see [Quickstart](quickstart.md)) and two environment variables in the shell you commit from:

| Variable | What |
|---|---|
| `VIBECHECK_BASE_URL` | Your server, e.g. `https://vibecheck.example.com` |
| `VIBECHECK_TOKEN` | The server's `VIBECHECK_TOKEN`, the same secret the GitHub Action uses |
| `VIBECHECK_REPO` | Optional. `owner/name`. Defaults to the repo your GitHub `origin` remote points at |

The repo must be in `VIBECHECK_ALLOWED_REPOS` if the server sets it.

### As a plain git hook

The hook is a single Python 3 file with no dependencies:

```sh
curl -fsSL https://github.com/bhurghundii/vibecheck.dev/raw/main/scripts/pre_commit_hook.py \
  -o .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

### With the pre-commit framework

Add this to `.pre-commit-config.yaml` (needs pre-commit 3.2.0 or later):

```yaml
repos:
  - repo: https://github.com/bhurghundii/vibecheck.dev
    rev: main  # pin a tag or commit SHA
    hooks:
      - id: vibecheck
```

The hook runs with `verbose: true`. Without it, pre-commit would hide the warnings described below.

## What blocks, and what doesn't

The hook **fails open**. When the server is down or the hook is misconfigured, that says nothing about your change, so the commit goes through with a warning:

| Situation | Commit |
|---|---|
| Session passed | goes through |
| Session pending (new link) or failed | **blocked**, link printed |
| Change rejected by the server's checks (`422`, e.g. more than 1,000 changed lines or over `MAX_DIFF_BYTES`), the same as the `grill` job going red | **blocked**, reason printed |
| `VIBECHECK_BASE_URL` / `VIBECHECK_TOKEN` unset, no GitHub `origin` and no `VIBECHECK_REPO` | goes through, warning |
| Server unreachable, or no progress for 120 s (the limit is 120 s per network operation, not a total deadline, so a server that trickles data can take longer) | goes through, warning |
| `git diff --cached` fails, or the hook hits any other unexpected error | goes through, warning |
| Any other error: `401`, `403`, `413` (request body over the server's cap), `5xx` | goes through, warning |

A failed session blocks that exact staged content until you change it. If the server sets `ALLOW_RESTART=true`, you can also use **Start over** on the result page.

`git commit --no-verify` skips the hook entirely. Treat the hook as an early warning for the author. The PR gate and its `vibecheck/verdict` status are still the enforcement.

## Before you roll it out

- **Your unpushed work leaves your machine.** The hook sends whatever is staged to the vibecheck server, and the server sends it on to the model provider (Anthropic). That includes anything you staged by accident, such as a secret in a config file. The server stores the diff in Postgres as plaintext.
- **Every developer needs `VIBECHECK_TOKEN`.** That is the same secret the GitHub Action uses. Set `VIBECHECK_ALLOWED_REPOS` on the server so a leaked token is limited to your repos.
- **Model spend is per staged diff, not per PR.** Each distinct staged diff costs the two session-creation model calls, plus grading. Repeat attempts at the same content are free. A developer who commits in many small steps creates many sessions, where the PR gate creates one per head commit.
