"""app/cli.py: the local `vibecheck` command. The walkthrough is driven with
scripted answers against the fake grader, and the git diffs against a real
throwaway repo, so nothing calls the model API or needs a database.

Two tests guard against drift from the server rather than testing the CLI on
its own: its defaults must match app.config's, and its pass/fail rule must
match app.db.exam.verdict_for, since the CLI repeats both to stay free of the
server's dependencies.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import cli
from app.ai.grading import FakeGrader, GradingError, QuestionMark
from app.cli import EXIT_ERROR, EXIT_FAILED, EXIT_PASSED, EXIT_STOPPED, LocalSettings, Walkthrough

SAMPLE_DIFF = (Path(__file__).resolve().parent.parent / "scripts/fixtures/sample.diff").read_text()
PASS = FakeGrader.MARKER


def _script(*lines: str):
    """An `ask` that returns these lines in order, then raises EOFError like input()."""
    remaining: Iterator[str] = iter(lines)

    def ask(_prompt: str) -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    return ask


def _run(grader, *answers: str, **settings) -> tuple[int, str]:
    out = io.StringIO()
    walkthrough = Walkthrough(
        grader, SAMPLE_DIFF, LocalSettings(**settings), out=out, ask=_script(*answers)
    )
    return asyncio.run(walkthrough.run()), out.getvalue()


# --- drift from the server --------------------------------------------------


def test_defaults_match_the_server_settings() -> None:
    from app.config import Settings, SizeCheck

    fields = Settings.model_fields
    assert cli.DEFAULT_QUESTION_COUNT == fields["exam_question_count"].default
    assert cli.DEFAULT_PASSING_MARKS == fields["passingmarks"].default
    assert cli.DEFAULT_MAX_ATTEMPTS == fields["max_question_attempts"].default
    assert cli.DEFAULT_MAX_DIFF_BYTES == fields["max_diff_bytes"].default
    assert cli.DEFAULT_MAX_ANSWER_BYTES == fields["max_answer_bytes"].default
    assert cli.DEFAULT_MAX_CHANGED_LINES == SizeCheck().max_changed_lines
    assert list(cli.DEFAULT_SIZE_EXCLUDE) == SizeCheck().exclude


@pytest.mark.parametrize("question_count", [1, 3, 5])
def test_verdict_rule_matches_the_server(question_count: int) -> None:
    from app.db.exam import verdict_for

    for need in range(1, question_count + 1):
        for passed in range(question_count + 1):
            for skipped in range(question_count - passed + 1):
                marks = {str(i): {"state": "passed"} for i in range(passed)}
                marks |= {str(passed + i): {"state": "skipped"} for i in range(skipped)}
                tally = cli.Tally(passed=passed, resolved=passed + skipped)
                assert tally.verdict(question_count, need) == verdict_for(
                    marks, question_count, need
                )


def test_importing_the_cli_does_not_need_the_server_dependencies() -> None:
    # A fresh interpreter, so modules other tests imported don't count.
    code = (
        "import sys, app.cli; "
        "bad = sorted({'pydantic', 'pydantic_settings', 'fastapi', 'psycopg'} & set(sys.modules)); "
        "sys.exit(', '.join(bad) or None)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- the walkthrough --------------------------------------------------------


def test_correct_answers_pass() -> None:
    code, out = _run(FakeGrader(), PASS, "", PASS, "", PASS, "")
    assert code == EXIT_PASSED
    assert "PASSED (3/3 correct)" in out


def test_stops_asking_once_the_pass_mark_is_reached() -> None:
    code, out = _run(FakeGrader(), PASS, "", PASS, "", exam_question_count=5, passingmarks=2)
    assert code == EXIT_PASSED
    assert "Question 3 of" not in out


def test_answer_spans_lines_until_an_empty_one() -> None:
    code, _ = _run(FakeGrader(), "first line", f"then {PASS}", "", PASS, "", PASS, "")
    assert code == EXIT_PASSED


def test_running_out_of_tries_reveals_the_answer_and_fails_when_out_of_reach() -> None:
    code, out = _run(FakeGrader(), "no", "", "nope", "", max_question_attempts=2, passingmarks=3)
    assert code == EXIT_FAILED
    assert "Out of tries" in out
    assert "FAKE reference answer 1" in out


def test_skip_reveals_the_answer_and_counts_as_a_fail() -> None:
    code, out = _run(FakeGrader(), ":skip", "n")
    assert code == EXIT_FAILED
    assert "Skipped" in out
    assert "FAKE reference answer 1" in out


def test_explain_does_not_use_an_attempt() -> None:
    code, out = _run(FakeGrader(), ":explain", PASS, "", PASS, "", PASS, "", max_question_attempts=1)
    assert code == EXIT_PASSED
    assert "FAKE explanation for" in out


def test_quit_stops_with_its_own_exit_status() -> None:
    code, out = _run(FakeGrader(), ":quit")
    assert code == EXIT_STOPPED
    assert "Stopped." in out


def test_end_of_input_stops() -> None:
    code, _ = _run(FakeGrader())
    assert code == EXIT_STOPPED


def test_a_failed_run_can_start_over_with_fresh_questions() -> None:
    class Recording(FakeGrader):
        avoided: list[str] | None = None

        async def generate_exam(self, diff, count, avoid=None):
            self.avoided = avoid
            return await super().generate_exam(diff, count, avoid)

    grader = Recording()
    code, out = _run(grader, ":skip", "y", PASS, "", PASS, "", PASS, "")
    assert code == EXIT_PASSED
    assert "FAILED" in out and "PASSED" in out
    assert grader.avoided and grader.avoided[0] == "How does this change work on a high level?"


def test_a_grading_error_does_not_use_an_attempt() -> None:
    class Flaky(FakeGrader):
        calls = 0

        async def grade_answer(self, interpretation, question, answer):
            self.calls += 1
            if self.calls == 1:
                raise GradingError("boom")
            return QuestionMark(passed=True, note="ok")

    code, out = _run(Flaky(), "a", "", "a", "", "a", "", "a", "", max_question_attempts=1)
    assert code == EXIT_PASSED
    assert "Grading failed" in out


def test_a_scoped_question_shows_its_slice_of_the_diff() -> None:
    _, out = _run(FakeGrader(), PASS, "", ":quit")
    assert "Question 2 of" in out
    assert "billing/webhooks.py · lines" in out


# --- main -------------------------------------------------------------------


def _main(monkeypatch, argv: list[str], *answers: str) -> int:
    monkeypatch.setattr("builtins.input", _script(*answers))
    return cli.main(argv)


def test_main_runs_a_diff_file(tmp_path: Path, monkeypatch) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text(SAMPLE_DIFF)
    assert _main(monkeypatch, ["--fake", "--diff", str(diff)], PASS, "", PASS, "", PASS, "") == 0


def test_main_rejects_an_oversized_change_before_asking(tmp_path: Path, monkeypatch, capsys) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text(SAMPLE_DIFF)
    assert _main(monkeypatch, ["--fake", "--diff", str(diff), "--max-lines", "1"]) == EXIT_ERROR
    assert "Rejected before any questions" in capsys.readouterr().err


def test_main_needs_an_api_key_without_fake(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert _main(monkeypatch, ["--diff", str(tmp_path / "x.diff")]) == EXIT_ERROR
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_refuses_a_pass_mark_above_the_question_count(monkeypatch) -> None:
    assert _main(monkeypatch, ["--fake", "--questions", "2", "--pass", "3"]) == EXIT_ERROR


# --- git diffs --------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main with one commit, and a `feature` branch off it with one more. main then
    moves on, so a two-dot diff would wrongly include main's later change."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "a.txt").write_text("one\nfeature\n")
    _git(tmp_path, "commit", "-qam", "feature")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "b.txt").write_text("main moved on\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "later on main")
    _git(tmp_path, "checkout", "-q", "feature")
    return tmp_path


def test_branch_diff_is_only_this_branchs_change(repo: Path) -> None:
    diff = cli.branch_diff("main", working_tree=False, cwd=repo)
    assert "+feature" in diff
    assert "b.txt" not in diff


def test_branch_diff_finds_the_base_on_its_own(repo: Path) -> None:
    assert "+feature" in cli.branch_diff(None, working_tree=False, cwd=repo)


def test_working_tree_adds_uncommitted_changes(repo: Path) -> None:
    (repo / "a.txt").write_text("one\nfeature\nuncommitted\n")
    assert "+uncommitted" not in cli.branch_diff("main", working_tree=False, cwd=repo)
    assert "+uncommitted" in cli.branch_diff("main", working_tree=True, cwd=repo)


def test_unknown_base_is_a_clear_error(repo: Path) -> None:
    with pytest.raises(cli.CliError, match="nope"):
        cli.branch_diff("nope", working_tree=False, cwd=repo)
