"""`vibecheck`: the walkthrough in your terminal, against your branch or a PR.

    vibecheck                  # this branch against its base (origin's default branch)
    vibecheck --pr 123         # a GitHub PR, fetched with `gh pr diff`
    vibecheck --diff my.diff   # any diff file

Practice only. Nothing is stored and no commit status is posted, so passing
here does not turn `vibecheck/verdict` green; the server is still the gate. It
reuses the server's grader (app.ai.grading) and pre-question checks
(app.evaluator), so the questions, marking and rejections are the same.

Needs only httpx, so it imports nothing from the server side of the app.
Exit status: 0 passed, 1 failed, 2 rejected or could not run, 130 stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TextIO

from app.ai.grading import ExamQuestion, FakeGrader, Grader, GradingError, RealGrader
from app.evaluator import count_changed_lines, evaluate, question_count_for

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2
EXIT_STOPPED = 130

# The server's defaults (app.config.Settings / SizeCheck), repeated here because
# the CLI runs without pydantic-settings. tests/test_cli.py checks they match.
DEFAULT_QUESTION_COUNT = 5
DEFAULT_PASSING_MARKS = 3
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_DIFF_BYTES = 400_000
DEFAULT_MAX_ANSWER_BYTES = 20_000
DEFAULT_MAX_CHANGED_LINES = 1000
DEFAULT_SIZE_EXCLUDE = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum")

_EXPLAIN = ":explain"
_SKIP = ":skip"
_QUIT = ":quit"


class CliError(Exception):
    """Something stopped the CLI before the walkthrough; the message is for the user."""


# --- settings ---------------------------------------------------------------


@dataclass
class _SizeCheck:
    max_changed_lines: int
    exclude: list[str]


@dataclass
class _EvaluatorConfig:
    size: _SizeCheck


@dataclass
class LocalSettings:
    """The slice of app.config.Settings that app.evaluator and the walkthrough read."""

    exam_question_count: int = DEFAULT_QUESTION_COUNT
    passingmarks: int = DEFAULT_PASSING_MARKS
    max_question_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
    max_answer_bytes: int = DEFAULT_MAX_ANSWER_BYTES
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES
    size_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_SIZE_EXCLUDE))

    def evaluator_config(self) -> _EvaluatorConfig:
        return _EvaluatorConfig(size=_SizeCheck(self.max_changed_lines, self.size_exclude))

    def passing_marks_for(self, question_count: int) -> int:
        return min(self.passingmarks, question_count)


# --- getting the diff -------------------------------------------------------


def _run(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise CliError(f"`{args[0]}` is not installed or not on PATH.") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CliError(f"`{' '.join(args)}` failed: {detail}")
    return result.stdout


def _git_ref_exists(ref: str, cwd: Path | None) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def default_base(cwd: Path | None = None) -> str:
    """origin's default branch if git knows it, else the first of the usual names."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if _git_ref_exists(candidate, cwd):
            return candidate
    raise CliError("Could not work out the base branch. Pass it with --base, e.g. --base main.")


def branch_diff(base: str | None, *, working_tree: bool, cwd: Path | None = None) -> str:
    """This branch's change since it left `base`, the same three-dot diff the Action
    sends. working_tree adds uncommitted changes to tracked files."""
    _run(["git", "rev-parse", "--git-dir"], cwd)  # a clear error outside a repo
    base = base or default_base(cwd)
    if not _git_ref_exists(base, cwd):
        raise CliError(f"Base `{base}` is not a commit git knows about. Fetch it or pass --base.")
    diff_args = ["git", "diff", "--no-color", "--no-ext-diff"]
    if working_tree:
        merge_base = _run(["git", "merge-base", base, "HEAD"], cwd).strip()
        return _run([*diff_args, merge_base], cwd)
    return _run([*diff_args, f"{base}...HEAD"], cwd)


def pr_diff(pr: str, repo: str | None = None) -> str:
    """A GitHub PR's diff via the gh CLI, which handles auth and GitHub Enterprise."""
    if shutil.which("gh") is None:
        raise CliError(
            "--pr needs the GitHub CLI: `brew install gh`, then `gh auth login`. "
            "Or check out the PR's branch and run vibecheck with no --pr."
        )
    args = ["gh", "pr", "diff", pr, "--color", "never"]
    if repo:
        args += ["--repo", repo]
    return _run(args)


def file_diff(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"Could not read {path}: {exc.strerror or exc}") from exc


# --- terminal output --------------------------------------------------------


class _Style:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


def _color_enabled(stream: TextIO) -> bool:
    # https://no-color.org
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _file_for_line(lines: list[str], line_number: int) -> str | None:
    """The file a slice starts in, from the nearest header at or above it
    (mirrors app.web.routes._file_for_line)."""
    for line in reversed(lines[:line_number]):
        if line.startswith("+++ b/"):
            return line[len("+++ b/") :].strip() or None
        if line.startswith("diff --git ") and " b/" in line:
            return line.split(" b/", 1)[1].strip() or None
    return None


# --- the walkthrough --------------------------------------------------------


@dataclass
class Tally:
    passed: int = 0
    resolved: int = 0

    def verdict(self, question_count: int, passing_marks: int) -> str:
        """Same rule as app.db.exam.verdict_for."""
        if self.passed >= passing_marks:
            return "passed"
        if self.passed + (question_count - self.resolved) < passing_marks:
            return "failed"
        return "pending"


class Quit(Exception):
    """The user asked to stop, or input ended."""


class Walkthrough:
    """One run of questions in the terminal. `ask` reads one line (input() by
    default) so tests can script the answers."""

    def __init__(
        self,
        grader: Grader,
        diff: str,
        settings: LocalSettings,
        *,
        out: TextIO = sys.stdout,
        ask: Callable[[str], str] = input,
    ) -> None:
        self._grader = grader
        self._diff = diff
        self._lines = diff.splitlines()
        self._settings = settings
        self._out = out
        self._ask = ask
        self._s = _Style(_color_enabled(out))

    def _say(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    async def run(self) -> int:
        settings = self._settings
        self._say(self._s.dim("Reading the diff…"))
        try:
            interpretation = await self._grader.interpret(self._diff)
            questions = await self._grader.generate_exam(
                self._diff, question_count_for(self._diff, settings)
            )
        except GradingError as exc:
            raise CliError(f"Could not build the questions: {exc}") from exc

        seen: list[str] = []
        try:
            while True:
                status = await self._sheet(interpretation, questions)
                seen += [q.question for q in questions]
                if status == "passed":
                    return EXIT_PASSED
                if not self._confirm("Try again with fresh questions? [y/N] "):
                    return EXIT_FAILED
                self._say(self._s.dim("Writing new questions…"))
                try:
                    questions = await self._grader.generate_exam(
                        self._diff, question_count_for(self._diff, settings), avoid=seen
                    )
                except GradingError as exc:
                    raise CliError(f"Could not build new questions: {exc}") from exc
        except Quit:
            self._say()
            self._say("Stopped.")
            return EXIT_STOPPED

    async def _sheet(self, interpretation: str, questions: list[ExamQuestion]) -> str:
        total = len(questions)
        need = self._settings.passing_marks_for(total)
        self._say()
        self._say(
            self._s.bold(f"{total} question{'s' if total != 1 else ''}, {need} correct to pass.")
            + self._s.dim(
                f"  Finish an answer with an empty line. {_EXPLAIN}, {_SKIP}, {_QUIT}."
            )
        )
        tally = Tally()
        for index, question in enumerate(questions):
            await self._screen(interpretation, question, index, total, tally)
            status = tally.verdict(total, need)
            if status != "pending":
                break
        self._say()
        if status == "passed":
            self._say(self._s.green(self._s.bold(f"PASSED ({tally.passed}/{total} correct)")))
            self._say(
                self._s.dim("Practice only: the PR's vibecheck/verdict still needs the real run.")
            )
        else:
            self._say(self._s.red(self._s.bold(f"FAILED ({tally.passed}/{total} correct)")))
        return status

    def _show_screen(self, question: ExamQuestion, index: int, total: int) -> None:
        self._say()
        self._say(self._s.bold(f"── Question {index + 1} of {total} ──"))
        start, end = question.start_line, question.end_line
        if start and end:
            lo, hi = max(start, 1), min(end, len(self._lines))
            where = f"line {lo}" if lo == hi else f"lines {lo}–{hi}"
            path = _file_for_line(self._lines, lo)
            self._say(self._s.dim(f"{path} · {where}" if path else where))
            width = len(str(hi))
            for n in range(lo, hi + 1):
                line = self._lines[n - 1]
                if line.startswith("+") and not line.startswith("+++"):
                    line = self._s.green(line)
                elif line.startswith("-") and not line.startswith("---"):
                    line = self._s.red(line)
                self._say(f"{self._s.dim(str(n).rjust(width))} {line}")
            self._say()
        self._say(self._s.bold(question.question))

    async def _screen(
        self,
        interpretation: str,
        question: ExamQuestion,
        index: int,
        total: int,
        tally: Tally,
    ) -> None:
        self._show_screen(question, index, total)
        max_attempts = self._settings.max_question_attempts
        attempts = 0
        explanation: str | None = None
        while True:
            answer = self._read_answer(attempts + 1, max_attempts)
            command = answer.strip().lower()
            if command == _QUIT:
                raise Quit
            if command == _SKIP:
                self._reveal(question, "Skipped")
                tally.resolved += 1
                return
            if command == _EXPLAIN:
                # One explanation per question, like the web page; repeats are free.
                if explanation is None:
                    self._say(self._s.dim("Explaining…"))
                    try:
                        explanation = await self._grader.explain(interpretation, question.question)
                    except GradingError:
                        self._say(self._s.yellow("Couldn't explain just now, try again."))
                        continue
                self._say()
                self._say(explanation)
                self._say()
                continue
            if len(answer.encode("utf-8")) > self._settings.max_answer_bytes:
                self._say(
                    self._s.yellow(
                        f"Too long (max {self._settings.max_answer_bytes} bytes), shorten it."
                    )
                )
                continue

            self._say(self._s.dim("Grading…"))
            try:
                mark = await self._grader.grade_answer(interpretation, question.question, answer)
            except GradingError:
                # Not the answer's fault, so it doesn't use up an attempt.
                self._say(self._s.yellow("Grading failed, submit your answer again."))
                continue
            attempts += 1
            if mark.passed:
                self._say(self._s.green("✓ Correct. ") + mark.note)
                tally.passed += 1
                tally.resolved += 1
                return
            self._say(self._s.red("✗ Not quite. ") + mark.note)
            if attempts >= max_attempts:
                self._reveal(question, "Out of tries")
                tally.resolved += 1
                return

    def _reveal(self, question: ExamQuestion, heading: str) -> None:
        self._say(self._s.yellow(heading) + self._s.dim(" (counts as a fail)"))
        if question.reference_answer:
            self._say(self._s.dim("Answer: ") + question.reference_answer)

    def _read_answer(self, attempt: int, max_attempts: int) -> str:
        """Lines up to the first empty one. A command on the first line returns at once."""
        label = f"attempt {attempt}/{max_attempts}" if max_attempts > 1 else "answer"
        lines: list[str] = []
        while True:
            prompt = self._s.dim(f"[{label}] > ") if not lines else self._s.dim("... ")
            try:
                line = self._ask(prompt)
            except EOFError:
                if lines:  # Ctrl-D ends the answer, as an empty line would.
                    return "\n".join(lines)
                raise Quit from None
            if not lines and line.strip().lower() in (_EXPLAIN, _SKIP, _QUIT):
                return line
            if not line.strip():
                if lines:
                    return "\n".join(lines)
                continue
            lines.append(line)

    def _confirm(self, prompt: str) -> bool:
        try:
            return self._ask(prompt).strip().lower() in ("y", "yes")
        except EOFError:
            return False


# --- entry point ------------------------------------------------------------


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _version() -> str:
    try:
        return package_version("vibecheck-dev")
    except PackageNotFoundError:  # run from a checkout without installing
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibecheck",
        description=(
            "Quiz yourself on a change before you open or merge the PR. Asks the same "
            "questions the vibecheck server would and grades your answers. Practice "
            "only: nothing is stored or posted to GitHub."
        ),
        epilog=(
            "Needs ANTHROPIC_API_KEY (or MODEL_API_KEY) unless --fake is given. "
            "Exit status: 0 passed, 1 failed, 2 rejected or error, 130 stopped."
        ),
    )
    parser.add_argument("--version", action="version", version=f"vibecheck {_version()}")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--pr",
        metavar="PR",
        help="a PR number, URL or branch, fetched with `gh pr diff` (needs the gh CLI)",
    )
    source.add_argument("--diff", metavar="FILE", help="read the diff from a file")
    source.add_argument(
        "--base",
        metavar="REF",
        help="diff this branch against REF (default: origin's default branch)",
    )
    parser.add_argument("--repo", metavar="OWNER/NAME", help="with --pr: the repository")
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="include uncommitted changes to tracked files (not with --pr or --diff)",
    )
    parser.add_argument(
        "--questions",
        type=_positive_int,
        default=DEFAULT_QUESTION_COUNT,
        metavar="N",
        help=f"most questions to ask, the first included (default {DEFAULT_QUESTION_COUNT})",
    )
    parser.add_argument(
        "--pass",
        dest="passing_marks",
        type=_positive_int,
        default=DEFAULT_PASSING_MARKS,
        metavar="N",
        help=f"correct answers needed to pass (default {DEFAULT_PASSING_MARKS})",
    )
    parser.add_argument(
        "--attempts",
        type=_positive_int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help=f"tries per question before the answer is shown (default {DEFAULT_MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--max-lines",
        type=_non_negative_int,
        default=DEFAULT_MAX_CHANGED_LINES,
        metavar="N",
        help=(
            "reject a change over N changed lines, lockfiles not counted; 0 turns it off "
            f"(default {DEFAULT_MAX_CHANGED_LINES})"
        ),
    )
    parser.add_argument(
        "--mean", action="store_true", help="sarcastic grading, like MEANIEMODE on the server"
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help=f"no model calls: canned questions, and answers containing "
        f"'{FakeGrader.MARKER}' pass",
    )
    return parser


def _grader_from(args: argparse.Namespace) -> Grader:
    if args.fake:
        return FakeGrader()
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MODEL_API_KEY")
    if not key:
        raise CliError(
            "Set ANTHROPIC_API_KEY to your Anthropic API key (or use --fake to try it out)."
        )
    return RealGrader(api_key=key, meaniemode=args.mean)


def _load_diff(args: argparse.Namespace) -> str:
    if args.working_tree and (args.pr or args.diff):
        raise CliError("--working-tree only works on the current branch, not --pr or --diff.")
    if args.repo and not args.pr:
        raise CliError("--repo only goes with --pr.")
    if args.pr:
        return pr_diff(args.pr, args.repo)
    if args.diff:
        return file_diff(args.diff)
    return branch_diff(args.base, working_tree=args.working_tree)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.passing_marks > args.questions:
        print(
            f"vibecheck: --pass ({args.passing_marks}) is more than --questions "
            f"({args.questions}), so it could never pass.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if sys.stdin.isatty():
        try:
            import readline  # noqa: F401  (line editing and history for input())
        except ImportError:  # Windows
            pass
    settings = LocalSettings(
        exam_question_count=args.questions,
        passingmarks=args.passing_marks,
        max_question_attempts=args.attempts,
        max_changed_lines=args.max_lines,
    )
    try:
        grader = _grader_from(args)
        diff = _load_diff(args)
        if count_changed_lines(diff, []) == 0:
            raise CliError("No changes to ask about. Commit something, or try --working-tree.")
        rejection = evaluate(diff, settings)
        if rejection is not None:
            raise CliError(f"Rejected before any questions: {rejection}")
        return asyncio.run(Walkthrough(grader, diff, settings, out=sys.stdout, ask=input).run())
    except CliError as exc:
        print(f"vibecheck: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())
