"""POST /sessions: creation shape, non-concurrent repeat behaviour,
one rejection test per validated field, and the EVALUATOR rejections
(oversized diff, too many changed lines).

(Genuine concurrent-request idempotency is tested separately in
tests/api/test_idempotency.py — a sequential test here proves nothing about the race.)
"""

from __future__ import annotations

import logging
import secrets

from fastapi.testclient import TestClient

from app.ai.grading import FakeGrader, GradingError
from app.logging.config import JsonFormatter
from app.main import app

_VALID_BODY = {
    "repo": "octo/repo",
    "pr_number": 10,
    "head_sha": "1" * 40,
    "base_sha": "2" * 40,
    "diff": "diff --git a/x b/x\n+hello\n",
}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique_repo(label: str) -> str:
    return f"octo/{label}-{secrets.token_hex(4)}"


def test_create_session_returns_201_with_url_and_question(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("create")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending"
    assert data["question"]
    assert data["session_url"].startswith(f"{vibecheck_env.base_url}/s/")


def test_repeated_create_returns_200_with_same_url(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("repeat")}
    with TestClient(app) as client:
        first = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))
        second = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["session_url"] == second.json()["session_url"]


def test_rejects_invalid_repo(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": "not-a-valid-repo", "head_sha": "3" * 40, "base_sha": "4" * 40}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "repo" in fields


def test_rejects_non_positive_pr_number(vibecheck_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("badpr"),
        "pr_number": 0,
        "head_sha": "5" * 40,
        "base_sha": "6" * 40,
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "pr_number" in fields


def test_rejects_invalid_head_sha(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("badhead"), "head_sha": "not-hex", "base_sha": "7" * 40}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "head_sha" in fields


def test_rejects_invalid_base_sha(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("badbase"), "head_sha": "8" * 40, "base_sha": "short"}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "base_sha" in fields


def test_rejects_empty_diff(vibecheck_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("emptydiff"),
        "head_sha": "9" * 40,
        "base_sha": "a1" * 20,
        "diff": "   ",
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "diff" in fields


def _changed_lines_diff(path: str, lines: int) -> str:
    return f"diff --git a/{path} b/{path}\n@@ -0,0 +1,{lines} @@\n" + "+x\n" * lines


def test_rejects_oversized_diff_with_422_rejection(vibecheck_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("bigdiff"),
        "head_sha": "b2" * 20,
        "base_sha": "c3" * 20,
        "diff": "x" * 400_001,  # default max_diff_bytes is 400_000
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    assert "400,000-byte limit" in response.json()["rejection"]


def test_rejects_too_many_changed_lines_without_creating_a_session(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("biglines"), "diff": _changed_lines_diff("a.py", 1001)}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))
        verdict = client.get(
            "/verdict",
            params={"repo": body["repo"], "pr_number": body["pr_number"], "head_sha": body["head_sha"]},
            headers=_headers(vibecheck_env.token),
        )

    assert response.status_code == 422
    assert "1,001 lines" in response.json()["rejection"]
    assert verdict.json() == {"status": "UNKNOWN"}


def test_lockfile_changes_do_not_count_toward_the_line_limit(vibecheck_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("lockfile"), "diff": _changed_lines_diff("uv.lock", 5000)}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 201


def test_evaluator_env_sets_the_line_limit(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("EVALUATOR", '{"size": {"max_changed_lines": 5}}')
    body = {**_VALID_BODY, "repo": _unique_repo("custom"), "diff": _changed_lines_diff("a.py", 6)}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 422
    assert "5-line limit" in response.json()["rejection"]


def test_repo_allowlist_unset_permits_any_repo(vibecheck_env) -> None:
    # VIBECHECK_ALLOWED_REPOS is never set by the vibecheck_env fixture — this is
    # an explicit regression test for that default (unrestricted) behavior,
    # not just an incidental side effect of the other tests in this file.
    body = {**_VALID_BODY, "repo": _unique_repo("unrestricted")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 201


def test_create_session_rejects_repo_not_in_allowlist(vibecheck_env, monkeypatch) -> None:
    # Must be set before `with TestClient(app)`: get_settings() runs once,
    # inside the lifespan, at TestClient.__enter__().
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "octo/only-allowed")
    body = {**_VALID_BODY, "repo": _unique_repo("disallowed")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 403


def test_create_session_allows_repo_in_allowlist(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "octo/only-allowed")
    body = {**_VALID_BODY, "repo": "octo/only-allowed"}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))

    assert response.status_code == 201


def test_a_tiny_diff_gets_only_the_high_level_question(vibecheck_env) -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+hello\n"
    with TestClient(app) as client:
        resp = client.post(
            "/sessions",
            json={
                "repo": "acme/widgets",
                "pr_number": 1,
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "diff": diff,
            },
            headers={"Authorization": f"Bearer {vibecheck_env.token}"},
        )
        assert resp.status_code in (200, 201)
        page = client.get(resp.json()["session_url"])
    assert "Question 1 of 1" in page.text


def test_grading_failure_on_create_logs_why_and_returns_502(vibecheck_env) -> None:
    # The Action only gets the 502 body; without this line an operator reading
    # the server's logs sees `outcome: error` and nothing about the cause.
    class FailingGrader(FakeGrader):
        async def interpret(self, diff):
            raise GradingError("model API returned 404: Model use case details have not been submitted")

    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    body = {**_VALID_BODY, "repo": _unique_repo("create-fail")}
    with TestClient(app) as client:
        app.state.grader = FailingGrader()
        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            response = client.post("/sessions", json=body, headers=_headers(vibecheck_env.token))
        finally:
            root.removeHandler(handler)

    assert response.status_code == 502
    failures = [line for line in emitted if '"outcome": "create_failed"' in line]
    assert len(failures) == 1
    assert "use case details have not been submitted" in failures[0]
    assert body["repo"] in failures[0]
