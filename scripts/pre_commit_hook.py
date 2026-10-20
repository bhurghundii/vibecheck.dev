#!/usr/bin/env python3
"""vibecheck as a git pre-commit hook: quiz the author on their staged diff
before the commit lands.

Two phases, no state on the developer's machine. The first commit attempt
POSTs the staged diff to /pre-commit/sessions, prints the link and blocks.
After the author passes on the web page, the same commit (same staged content,
so the same session) finds the session passed and goes through. Changing what
is staged is a new diff, and so a new session.

Fail-open by design, like the GitHub Action: an outage or a misconfiguration
is not evidence that the change is wrong, so anything other than the server's
own verdict warns and exits 0. Only pending, failed and an evaluator rejection
(422 with `rejection`) block. `git commit --no-verify` skips the hook
entirely; the PR gate is the enforcement, this is the early warning.

Standard library only, so it runs from a bare .git/hooks/pre-commit copy or
from the pre-commit framework's `language: script` without an install step.

Environment:
  VIBECHECK_BASE_URL  the vibecheck server, e.g. https://vibecheck.example.com
  VIBECHECK_TOKEN     the server's bearer token (the same one the Action uses)
  VIBECHECK_REPO      owner/name; defaults to the GitHub `origin` remote
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping

# The first POST for a diff runs two model calls, each retried with backoff,
# so it can legitimately take a while. Without a bound urllib waits forever and
# a stuck server would hang `git commit`.
DEFAULT_TIMEOUT_SECONDS = 120.0

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$"
)


def _say(message: str) -> None:
    print(f"vibecheck: {message}", file=sys.stderr)


def _warn(message: str) -> None:
    _say(f"warning: {message} (commit allowed)")


def parse_github_repo(url: str) -> str | None:
    """owner/name from a GitHub https or ssh remote URL, else None."""
    match = _GITHUB_REMOTE_RE.match(url.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['name']}"


def _git(*args: str) -> str | None:
    """git's stdout, or None if git is missing or fails."""
    try:
        result = subprocess.run(["git", *args], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    # Repos hold Latin-1 and binary-ish text too. A strict decode would crash
    # the hook and block the commit; replacing the odd byte still sends a diff
    # the server can quiz on.
    return result.stdout.decode("utf-8", errors="replace")


def staged_diff() -> str | None:
    """The staged diff ("" when nothing is staged), or None if git failed."""
    # --no-ext-diff and --no-color keep a user's diff config out of it: the
    # server counts lines after `diff --git` headers and needs plain git format.
    return _git("diff", "--cached", "--no-ext-diff", "--no-color")


def resolve_repo(env: Mapping[str, str]) -> str | None:
    if env.get("VIBECHECK_REPO"):
        return env["VIBECHECK_REPO"]
    origin = _git("remote", "get-url", "origin")
    return parse_github_repo(origin) if origin else None


def _post(base_url: str, token: str, repo: str, diff: str, timeout: float) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/pre-commit/sessions",
        data=json.dumps({"repo": repo, "diff": diff}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = None
        return exc.code, body


def main(
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    diff: str | None = None,
) -> int:
    # The last line of fail-open: whatever goes wrong that the checks below
    # didn't anticipate, a bug in the hook is not a reason to block a commit.
    # KeyboardInterrupt is not an Exception, so Ctrl-C still aborts.
    try:
        return _check(os.environ if env is None else env, timeout, diff)
    except Exception as exc:
        _warn(f"the hook failed unexpectedly ({type(exc).__name__}: {exc})")
        return 0


def _check(env: Mapping[str, str], timeout: float, diff: str | None) -> int:
    base_url, token = env.get("VIBECHECK_BASE_URL"), env.get("VIBECHECK_TOKEN")
    if not base_url or not token:
        missing = "VIBECHECK_BASE_URL" if not base_url else "VIBECHECK_TOKEN"
        _warn(f"{missing} is not set, so this commit was not checked")
        return 0

    if diff is None:
        diff = staged_diff()
        if diff is None:
            _warn("`git diff --cached` failed, so the staged changes could not be read")
            return 0
    if not diff.strip():
        return 0

    repo = resolve_repo(env)
    if repo is None:
        _warn("set VIBECHECK_REPO=owner/name; origin is not a GitHub remote")
        return 0

    try:
        code, body = _post(base_url, token, repo, diff, timeout)
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as exc:
        # URLError, a socket timeout and a refused connection are all OSError;
        # ValueError is a 2xx body that isn't JSON; HTTPException is a reply
        # that isn't valid HTTP or is cut off mid-body; RecursionError is JSON
        # nested too deep for json.loads.
        _warn(f"could not reach the vibecheck server ({exc})")
        return 0

    if not isinstance(body, dict):
        _warn(f"unexpected response from the vibecheck server (HTTP {code})")
        return 0

    if code == 422 and body.get("rejection"):
        _say(f"this change can't be checked: {body['rejection']}")
        return 1

    if code in (200, 201):
        status, url = body.get("status"), body.get("session_url")
        if status == "passed":
            _say("passed — committing.")
            return 0
        if status == "pending":
            _say(f"answer a few questions about this change first: {url}")
            _say("then run the same commit again.")
            return 1
        if status == "failed":
            _say(f"this change failed its check: {url}")
            _say("change the staged content (or start over on that page) and commit again.")
            return 1

    _warn(f"the vibecheck server answered HTTP {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
