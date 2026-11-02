"""Checks that reject a PR at POST /sessions (a 422 with the reason) before any
question is asked. Configured by EVALUATOR (app.config.EvaluatorConfig). Each
check in _CHECKS returns a reason or None; the first reason wins.

Also sizes the walkthrough: question_count_for() shrinks it for small diffs.
"""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

# Type-only: the local CLI (app/cli.py) runs these checks without the server's
# pydantic-settings installed, passing its own object with the same attributes.
if TYPE_CHECKING:
    from app.config import Settings

_GIT_HEADER = "diff --git "

# One scoped question per this many changed lines, so a one-line PR is not
# quizzed on git headers and boilerplate for lack of anything real to ask.
CHANGED_LINES_PER_QUESTION = 15


def _is_excluded(path: str | None, exclude: list[str]) -> bool:
    # Match the basename too, so "package-lock.json" covers web/package-lock.json.
    if path is None:
        return False
    name = PurePosixPath(path).name
    return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in exclude)


def count_changed_lines(diff: str, exclude: list[str]) -> int:
    """Added + removed lines outside the `exclude` globs. Only lines after a
    file's first `@@` count, so `---`/`+++` headers never do."""
    total = 0
    path: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith(_GIT_HEADER):
            # "diff --git a/X b/X": the b/ side is the path after the change.
            _, sep, after = line.rpartition(" b/")
            path = after if sep else None
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-") and not _is_excluded(path, exclude):
            total += 1
    return total


def _check_diff_bytes(diff: str, settings: Settings) -> str | None:
    size = len(diff.encode("utf-8"))
    if size <= settings.max_diff_bytes:
        return None
    return (
        f"This diff is {size:,} bytes, over the {settings.max_diff_bytes:,}-byte limit. "
        "Split it into smaller PRs."
    )


def _check_changed_lines(diff: str, settings: Settings) -> str | None:
    size = settings.evaluator_config().size
    if size.max_changed_lines == 0:
        return None
    changed = count_changed_lines(diff, size.exclude)
    if changed <= size.max_changed_lines:
        return None
    excluded = f", not counting {', '.join(size.exclude)}" if size.exclude else ""
    return (
        f"This PR changes {changed:,} lines, over the {size.max_changed_lines:,}-line "
        f"limit{excluded}. Split it into smaller PRs."
    )


# Byte size first: it is the cheap check.
_CHECKS: tuple[Callable[[str, Settings], str | None], ...] = (
    _check_diff_bytes,
    _check_changed_lines,
)


def evaluate(diff: str, settings: Settings) -> str | None:
    """The first failing check's reason, or None if the diff passes them all."""
    for check in _CHECKS:
        reason = check(diff, settings)
        if reason is not None:
            return reason
    return None


def question_count_for(diff: str, settings: Settings) -> int:
    """Walkthrough length: the fixed high-level question plus one scoped question
    per CHANGED_LINES_PER_QUESTION changed lines, capped at EXAM_QUESTION_COUNT."""
    changed = count_changed_lines(diff, settings.evaluator_config().size.exclude)
    return min(settings.exam_question_count, 1 + changed // CHANGED_LINES_PER_QUESTION)
