from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai.grading import BedrockGrader, FakeGrader, GradingError, RealGrader
from app.auth.bearer import require_bearer_token
from app.auth.repos import require_allowed_repo
from app.config import get_settings
from app.db.migrations import run_migrations
from app.db.pool import create_pool
from app.db.sessions import build_session_url, create_or_get_session, fetch_session_by_pr
from app.db.verdict import fetch_verdict
from app.evaluator import evaluate, question_count_for
from app.github.commit_status import GitHubStatusPublisher, NullStatusPublisher
from app.logging.config import configure_logging, redact_session_token
from app.middleware import MaxBodySizeMiddleware
from app.schemas import CreateSessionRequest
from app.web import router as web_router

request_logger = logging.getLogger("vibecheck.request")
startup_logger = logging.getLogger("vibecheck.startup")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings

    startup_logger.info("running migrations")
    applied = await run_migrations(
        settings.database_url.get_secret_value(), settings.migrations_dir
    )
    startup_logger.info(
        "migrations complete", extra={"outcome": "ok", "path": ",".join(applied) or "none pending"}
    )

    pool = await create_pool(
        settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    app.state.pool = pool

    if settings.fake_grader:
        app.state.grader = FakeGrader()
    elif settings.model_provider == "bedrock":
        bearer = settings.aws_bearer_token_bedrock
        app.state.grader = BedrockGrader(
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            bearer_token=bearer.get_secret_value() if bearer else None,
            max_tokens=settings.bedrock_max_tokens,
            endpoint_url=settings.bedrock_endpoint_url,
            meaniemode=settings.meaniemode,
        )
        startup_logger.info(
            f"grading via Amazon Bedrock model {settings.bedrock_model_id}",
            extra={"outcome": "bedrock"},
        )
    else:
        app.state.grader = RealGrader(
            api_key=settings.model_api_key.get_secret_value(), meaniemode=settings.meaniemode
        )

    # NullStatusPublisher is silent, so log which mode we are in: `disabled`
    # means the token was not loaded, `enabled` plus a warning means GitHub
    # refused it. The token is read once, at startup.
    if settings.github_status_token and settings.github_status_token.get_secret_value():
        app.state.status_publisher = GitHubStatusPublisher(
            settings.github_status_token.get_secret_value(), api_url=settings.github_api_url
        )
        startup_logger.info("commit status reporting enabled", extra={"outcome": "enabled"})
    else:
        app.state.status_publisher = NullStatusPublisher()
        startup_logger.info(
            "commit status reporting disabled (GITHUB_STATUS_TOKEN unset)",
            extra={"outcome": "disabled"},
        )

    startup_logger.info("startup complete", extra={"outcome": "ok"})

    try:
        yield
    finally:
        await pool.close()

app = FastAPI(title="vibecheck", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(web_router)

app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "web" / "static"), name="static")
# Images that ship with the app (the pass-page meme in MEANIEMODE). Served from
# our own origin, so the CSP only needs `img-src 'self'`, never a third party.
app.mount("/assets", StaticFiles(directory=Path(__file__).resolve().parent.parent / "assets"), name="assets")

app.add_middleware(MaxBodySizeMiddleware)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    outcome = "ok"
    try:
        response: Response = await call_next(request)
        if response.status_code >= 500:
            outcome = "error"
        return response
    except Exception:
        outcome = "error"
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        head_sha = getattr(request.state, "head_sha", None)
        request_logger.info(
            "request completed",
            extra={
                "repo": getattr(request.state, "repo", None),
                "pr_number": getattr(request.state, "pr_number", None),
                "head_sha": head_sha[:7] if head_sha else None,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "method": request.method,
                # Never the raw path: /s/{token} is the credential.
                "path": redact_session_token(request.url.path),
            },
        )

# Not great - need better ideas for security headers. 
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self'"
    ),
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.get("/healthz")
async def healthz() -> JSONResponse:
    pool = app.state.pool
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        request_logger.exception("healthz db check failed")
        return JSONResponse(status_code=503, content={"status": "error"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.post("/sessions", dependencies=[Depends(require_bearer_token)])
async def create_session(payload: CreateSessionRequest, request: Request) -> JSONResponse:
    request.state.repo = payload.repo
    request.state.pr_number = payload.pr_number
    request.state.head_sha = payload.head_sha

    settings = app.state.settings
    require_allowed_repo(settings, payload.repo)

    # Reject early if an evaluator check fails.
    rejection = evaluate(payload.diff, settings)
    if rejection is not None:
        return JSONResponse(status_code=422, content={"rejection": rejection})

    # Build the walkthrough up front so each screen grades with one model call.
    # A failure fails this POST, and the next workflow run retries. A session that
    # already exists keeps its original walkthrough (create_or_get_session never
    # rewrites it), so a re-run reuses it instead of paying for two model calls
    # whose output would be discarded.
    existing = await fetch_session_by_pr(
        app.state.pool, repo=payload.repo, pr_number=payload.pr_number, head_sha=payload.head_sha
    )
    if existing is not None:
        interpretation = existing["interpretation"]
        question_dicts = existing["questions"]
        first_question = existing["question"]
    else:
        grader = app.state.grader
        try:
            interpretation = await grader.interpret(payload.diff)
            questions = await grader.generate_exam(
                payload.diff, question_count_for(payload.diff, settings)
            )
        except GradingError as exc:
            return JSONResponse(
                status_code=502, content={"detail": f"could not build the walkthrough: {exc}"}
            )
        question_dicts = [asdict(q) for q in questions]
        first_question = questions[0].question
    token = secrets.token_urlsafe(32)

    row, created = await create_or_get_session(
        app.state.pool,
        repo=payload.repo,
        pr_number=payload.pr_number,
        head_sha=payload.head_sha,
        base_sha=payload.base_sha,
        diff=payload.diff,
        question=first_question,
        questions=question_dicts,
        interpretation=interpretation,
        token=token,
        ttl_days=settings.session_ttl_days,
    )

    session_url = build_session_url(settings.vibecheck_base_url, row["token"])

    # On every call, so re-running the workflow re-posts a status lost to a GitHub hiccup.
    await app.state.status_publisher.publish(
        repo=payload.repo,
        head_sha=payload.head_sha,
        status=row["status"],
        target_url=session_url,
    )
    body = {
        "session_url": session_url,
        "status": row["status"],
        "question": row["question"],
    }
    return JSONResponse(status_code=201 if created else 200, content=body)


@app.get("/verdict", dependencies=[Depends(require_bearer_token)])
async def get_verdict(
    request: Request,
    repo: str = Query(...),
    pr_number: int = Query(..., gt=0),
    head_sha: str = Query(...),
) -> dict:
    request.state.repo = repo
    request.state.pr_number = pr_number
    request.state.head_sha = head_sha

    settings = app.state.settings
    require_allowed_repo(settings, repo)

    status_value = await fetch_verdict(
        app.state.pool, repo=repo, pr_number=pr_number, head_sha=head_sha
    )
    return {"status": status_value}
