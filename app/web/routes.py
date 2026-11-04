"""Developer-facing HTML routes: the paged walkthrough and per-screen
submission. Unauthenticated; the token in the URL is the credential."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.ai.grading import GradingError
from app.db.exam import record_attempt, record_explanation, skip_question
from app.db.sessions import build_session_url, fetch_session_by_token, restart_session
from app.evaluator import question_count_for
from app.logging.config import redact_session_token
from app.web.rendering import render_markdown

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Rendered via `|markdown`; see app/web/rendering.py for why it is sanitised.
templates.env.filters["markdown"] = render_markdown

router = APIRouter()
logger = logging.getLogger("vibecheck.web")

# (session id, question index) pairs with a model call in flight. A second
# request for the same screen bounces instead of paying for a duplicate call.
# Per-process only: it narrows duplicate spend on one replica but does not stop
# it across replicas. record_attempt's row lock is what keeps results correct.
_in_flight: set[tuple[object, int]] = set()

_NOT_FOUND = {"heading": "Not found", "message": "This link isn't valid."}
_EXPIRED = {
    "heading": "This session expired",
    "message": "Re-run the check on the PR to get a fresh link.",
}
_RESTART_OFF = {
    "heading": "Start over is off",
    "message": "This deployment doesn't allow restarting. Push a new commit to get a fresh attempt.",
}
# A pre-commit author is at the commit prompt with no PR or pushed commit yet,
# so the PR wording above would tell them to do something they can't.
_EXPIRED_PRE_COMMIT = {
    "heading": "This session expired",
    "message": "Run your commit again to get a fresh link.",
}
_RESTART_OFF_PRE_COMMIT = {
    "heading": "Start over is off",
    "message": (
        "This deployment doesn't allow restarting. "
        "To get a fresh attempt, change the staged content and commit again."
    ),
}
_ALREADY_DECIDED = {
    "heading": "Already answered",
    "message": "This session already has a verdict and can't be answered again.",
}


def _classify_diff(diff: str) -> list[tuple[str, str]]:
    """Classify each line as added/removed/other; list position is the 1-based line number."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            css = "add"
        elif line.startswith("-") and not line.startswith("---"):
            css = "remove"
        else:
            css = "context"
        lines.append((css, line))
    return lines


def _slice_diff(
    diff_lines: list[tuple[str, str]], start: int | None, end: int | None
) -> list[tuple[int, str, str]]:
    """Lines for one screen as (line number, css, text); bounds are re-clamped."""
    if not start or not end:
        return []
    lo, hi = max(start, 1), min(end, len(diff_lines))
    return [(n, css, line) for n, (css, line) in enumerate(diff_lines[lo - 1 : hi], start=lo)]


def _file_for_line(diff_lines: list[tuple[str, str]], line_number: int | None) -> str | None:
    """The file a slice starts in, from the nearest header at or above it."""
    if not line_number:
        return None
    for _, line in reversed(diff_lines[:line_number]):
        if line.startswith("+++ b/"):
            return line[len("+++ b/") :].strip() or None
        if line.startswith("diff --git ") and " b/" in line:
            return line.split(" b/", 1)[1].strip() or None
    return None


def _format_range(start: int | None, end: int | None) -> str | None:
    """Caption for a slice, e.g. "lines 40–44"."""
    if not start or not end:
        return None
    return f"line {start}" if start == end else f"lines {start}–{end}"


def _questions(session: dict) -> list[dict]:
    """The walkthrough questions as dicts; older rows fall back to the stored `question`."""
    questions = session.get("questions")
    if isinstance(questions, list) and questions:
        out = []
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                out.append(q)
            elif isinstance(q, str):  # pre-walkthrough: a bare string
                out.append({"question": q})
        if out:
            return out
    return [{"question": session["question"]}]


def _resolved(marks: dict, index: int) -> bool:
    entry = marks.get(str(index))
    return bool(entry) and entry.get("state") in ("passed", "skipped")


def _frontier(marks: dict, total: int) -> int:
    """Index of the first unresolved screen, or `total` if all are resolved."""
    for i in range(total):
        if not _resolved(marks, i):
            return i
    return total


def _resolve_step(raw: str | None, frontier: int, total: int) -> int:
    """Which screen to show: any answered one or the current one, never ahead.
    Bad input lands on the current screen."""
    highest = min(frontier, total - 1)
    try:
        return min(max(int(raw), 0), highest)
    except (TypeError, ValueError):
        return highest


def _is_pre_commit(session: dict) -> bool:
    return session.get("source") == "pre_commit"


def _expired(session: dict) -> dict:
    return _EXPIRED_PRE_COMMIT if _is_pre_commit(session) else _EXPIRED


async def _publish(request: Request, session: dict, status: str) -> None:
    """Post the commit status for a PR session. A pre-commit session has no
    commit on GitHub to post to; the hook reads its status back instead."""
    if _is_pre_commit(session):
        return
    settings = request.app.state.settings
    await request.app.state.status_publisher.publish(
        repo=session["repo"],
        head_sha=session["head_sha"],
        status=status,
        target_url=build_session_url(settings.vibecheck_base_url, session["token"]),
    )


def _is_expired(session: dict) -> bool:
    return session["expires_at"] < datetime.now(UTC)


def _form_js_active(value: object) -> bool:
    """The hidden `js_active` field set by nopaste.js, coerced like a FastAPI bool."""
    return str(value).strip().lower() in ("true", "on", "1")


def _log_if_unguarded(request: Request, session: dict, js_active: bool) -> None:
    """Log a submission from a page where the paste guard did not run; not refused."""
    if js_active:
        return
    logger.warning(
        "submitted without paste guard",
        extra={
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "outcome": "no_js",
            "path": redact_session_token(request.url.path),
        },
    )


def _log_grading_failure(session: dict, *, outcome: str) -> None:
    """Log why grading failed (the developer only sees "try again"). Call inside the `except`."""
    logger.warning(
        "grading call failed",
        extra={
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "head_sha": session["head_sha"],
            "outcome": outcome,
        },
        exc_info=True,
    )


def _screen_context(
    session: dict,
    questions: list[dict],
    marks: dict,
    number: int,
    max_attempts: int,
    *,
    error: str | None = None,
    draft: str | None = None,
) -> dict:
    """Context for one screen; index 0 has no anchor and shows the whole diff.
    Open screens show the form, resolved ones the outcome or revealed answer."""
    diff_lines = _classify_diff(session["diff"])
    total = len(questions)
    current = questions[number]
    start, end = current.get("start_line"), current.get("end_line")
    is_high_level = start is None and end is None
    status = session["status"]

    entry = marks.get(str(number)) or {}
    state = entry.get("state", "open")
    attempts = entry.get("attempts") or []
    last = attempts[-1] if attempts else None
    revealed = entry.get("revealed_answer") or (session.get("interpretation") if is_high_level else None)

    next_href = f"?step={number + 1}" if number + 1 < total else None

    return {
        "token": session["token"],
        "repo": session["repo"],
        "pr_number": session["pr_number"],
        "step_number": number + 1,
        "steps_total": total,
        "question": current["question"],
        "is_high_level": is_high_level,
        "diff_lines": diff_lines,
        "slice_lines": _slice_diff(diff_lines, start, end),
        "slice_file": _file_for_line(diff_lines, start),
        "slice_range": _format_range(start, end),
        "state": state,
        "last_answer": last["answer"] if last else "",
        "last_note": last["note"] if last else None,
        "revealed_answer": revealed,
        "by_user": bool(entry.get("by_user")),
        "explanation": entry.get("explanation"),
        "attempt_number": len(attempts) + 1,
        "max_attempts": max_attempts,
        "session_decided": status in ("passed", "failed"),
        "next_href": next_href,
        "error": error,
        "draft": draft if draft is not None else "",
    }


@router.get("/s/{token}")
async def view_session(request: Request, token: str) -> HTMLResponse:
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)

    questions = _questions(session)
    marks = session.get("marks") or {}

    raw_step = request.query_params.get("step")
    if session["status"] in ("passed", "failed") and raw_step is None:
        return _render_result(request, session, questions, marks)

    if session["status"] == "pending" and _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _expired(session), status_code=410)

    frontier = _frontier(marks, len(questions))
    number = _resolve_step(raw_step, frontier, len(questions))
    context = _screen_context(session, questions, marks, number, settings.max_question_attempts)
    return templates.TemplateResponse(request, "exam.html", context)


def _current_screen(session: dict):
    """(questions, marks, frontier) for a pending session, or None if terminal or expired."""
    questions = _questions(session)
    marks = session.get("marks") or {}
    return questions, marks, _frontier(marks, len(questions))


@router.post("/s/{token}/answer")
async def submit_answer(request: Request, token: str) -> HTMLResponse:
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)
    if session["status"] in ("passed", "failed"):
        return templates.TemplateResponse(request, "error.html", _ALREADY_DECIDED, status_code=409)
    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _expired(session), status_code=410)

    questions, marks, frontier = _current_screen(session)

    form = await request.form()
    answer = str(form.get("answer", ""))
    js_active = _form_js_active(form.get("js_active"))
    try:
        index = int(form.get("index"))
    except (TypeError, ValueError):
        index = -1

    # Only the current screen is answerable; a stale or double submit bounces back.
    if index != frontier or frontier >= len(questions):
        return RedirectResponse(url=f"/s/{token}", status_code=303)

    def _rerender(error: str, code: int) -> HTMLResponse:
        context = _screen_context(
            session, questions, marks, index, settings.max_question_attempts, error=error, draft=answer
        )
        return templates.TemplateResponse(request, "exam.html", context, status_code=code)

    if not answer.strip():
        return _rerender("Write an answer before submitting.", 422)
    if len(answer.encode("utf-8")) > settings.max_answer_bytes:
        return _rerender(
            f"Answer is too long (max {settings.max_answer_bytes} bytes) — please shorten it.",
            422,
        )

    guard = (session["id"], index)
    if guard in _in_flight:
        return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)
    _in_flight.add(guard)
    grader = request.app.state.grader
    try:
        mark = await grader.grade_answer(
            session.get("interpretation") or "", questions[index]["question"], answer
        )
    except GradingError:
        _log_grading_failure(session, outcome="grade_failed")
        return _rerender("Grading failed — please try submitting your answer again.", 502)
    finally:
        _in_flight.discard(guard)

    _log_if_unguarded(request, session, js_active)
    status, written = await record_attempt(
        pool,
        session_id=session["id"],
        index=index,
        answer=answer,
        passed=mark.passed,
        note=mark.note,
        js_active=js_active,
        reference_answer=questions[index].get("reference_answer", ""),
        max_attempts=settings.max_question_attempts,
        question_count=len(questions),
        passing_marks=settings.passing_marks_for(len(questions)),
    )

    # Only a terminal verdict is posted; an undecided session already shows pending.
    if written and status in ("passed", "failed"):
        await _publish(request, session, status)

    # Post/redirect/get back to this screen.
    return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)


@router.post("/s/{token}/explain")
async def explain_question(request: Request, token: str) -> HTMLResponse:
    """Generate a plain-language explanation of the current section; never changes the verdict."""
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)
    if session["status"] in ("passed", "failed"):
        return templates.TemplateResponse(request, "error.html", _ALREADY_DECIDED, status_code=409)
    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _expired(session), status_code=410)

    questions, marks, frontier = _current_screen(session)
    form = await request.form()
    try:
        index = int(form.get("index"))
    except (TypeError, ValueError):
        index = -1
    if index != frontier or frontier >= len(questions):
        return RedirectResponse(url=f"/s/{token}", status_code=303)

    # One explanation per screen: repeat clicks reuse the stored one, free.
    if (marks.get(str(index)) or {}).get("explanation"):
        return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)

    guard = (session["id"], index)
    if guard in _in_flight:
        return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)
    _in_flight.add(guard)
    grader = request.app.state.grader
    try:
        explanation = await grader.explain(
            session.get("interpretation") or "", questions[index]["question"]
        )
    except GradingError:
        _log_grading_failure(session, outcome="explain_failed")
        context = _screen_context(
            session,
            questions,
            marks,
            index,
            settings.max_question_attempts,
            error="Couldn't generate an explanation just now — please try again.",
        )
        return templates.TemplateResponse(request, "exam.html", context, status_code=502)
    finally:
        _in_flight.discard(guard)

    await record_explanation(pool, session_id=session["id"], index=index, explanation=explanation)
    return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)


@router.post("/s/{token}/restart")
async def restart_walkthrough(request: Request, token: str) -> HTMLResponse:
    """Start over after a failed verdict: a fresh sheet on the same commit and link.

    Unlimited by design. The questions are regenerated, avoiding the ones already
    seen (their answers were revealed), and the blind interpretation is reused, so
    a restart costs one model call."""
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)
    if not settings.allow_restart:
        restart_off = _RESTART_OFF_PRE_COMMIT if _is_pre_commit(session) else _RESTART_OFF
        return templates.TemplateResponse(request, "error.html", restart_off, status_code=403)
    if session["status"] != "failed":
        return RedirectResponse(url=f"/s/{token}", status_code=303)

    guard = (session["id"], -1)
    if guard in _in_flight:
        return RedirectResponse(url=f"/s/{token}", status_code=303)
    _in_flight.add(guard)
    try:
        grader = request.app.state.grader
        try:
            fresh = await grader.generate_exam(
                session["diff"],
                question_count_for(session["diff"], settings),
                avoid=[q["question"] for q in _questions(session)],
            )
        except GradingError:
            _log_grading_failure(session, outcome="restart_failed")
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "heading": "Couldn't start over",
                    "message": "Writing a fresh set of questions failed. Try again in a moment.",
                },
                status_code=502,
            )
        restarted = await restart_session(
            pool,
            session_id=session["id"],
            question=fresh[0].question,
            questions=[asdict(q) for q in fresh],
            ttl_days=settings.session_ttl_days,
        )
    finally:
        _in_flight.discard(guard)

    if restarted is not None:
        logger.info(
            "session restarted",
            extra={
                "outcome": "restarted",
                "repo": session["repo"],
                "pr_number": session["pr_number"],
                "head_sha": session["head_sha"],
            },
        )
        await _publish(request, session, "pending")
    return RedirectResponse(url=f"/s/{token}", status_code=303)


@router.post("/s/{token}/skip")
async def skip_current_question(request: Request, token: str) -> HTMLResponse:
    """Fail the current question on request: reveal its answer and move on."""
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)
    if session["status"] in ("passed", "failed"):
        return templates.TemplateResponse(request, "error.html", _ALREADY_DECIDED, status_code=409)
    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _expired(session), status_code=410)

    questions, marks, frontier = _current_screen(session)
    form = await request.form()
    try:
        index = int(form.get("index"))
    except (TypeError, ValueError):
        index = -1
    if index != frontier or frontier >= len(questions):
        return RedirectResponse(url=f"/s/{token}", status_code=303)

    status, written = await skip_question(
        pool,
        session_id=session["id"],
        index=index,
        reference_answer=questions[index].get("reference_answer", ""),
        question_count=len(questions),
        passing_marks=settings.passing_marks_for(len(questions)),
    )

    if written and status in ("passed", "failed"):
        await _publish(request, session, status)

    return RedirectResponse(url=f"/s/{token}?step={index}", status_code=303)


def _render_result(request: Request, session: dict, questions: list[dict], marks: dict) -> HTMLResponse:
    """The summary once decided: each question with its outcome and any revealed answer."""
    items = []
    for i, q in enumerate(questions):
        entry = marks.get(str(i)) or {}
        attempts = entry.get("attempts") or []
        last = attempts[-1] if attempts else None
        revealed = entry.get("revealed_answer") or (
            session.get("interpretation") if not q.get("start_line") and not q.get("end_line") else None
        )
        items.append(
            {
                "question": q["question"],
                "state": entry.get("state", "unreached"),
                "answer": last["answer"] if last else "",
                "note": last["note"] if last else None,
                "revealed_answer": revealed if entry.get("state") == "skipped" else None,
                "by_user": bool(entry.get("by_user")),
            }
        )
    passed = sum(1 for m in marks.values() if m.get("state") == "passed")
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "diff_lines": _classify_diff(session["diff"]),
            "status": session["status"],
            "items": items,
            "score": passed,
            "total": len(questions),
            "token": session["token"],
            "allow_restart": request.app.state.settings.allow_restart,
            "meme": request.app.state.settings.meaniemode,
        },
    )
