"""POST /pre-commit/sessions: a session keyed by (repo, sha256 of the staged
diff) rather than by PR and head SHA, for the git pre-commit hook.

The repeat-POST tests matter more than they look. The hook POSTs on every
`git commit`, so the second call for the same staged content must find the
existing session before touching the grader, or each commit attempt would pay
for two model calls whose output is thrown away. The counting grader below is
installed after the app starts, so it sees every call the endpoint makes.
"""

from __future__ import annotations

import secrets

import psycopg
from fastapi.testclient import TestClient

from app.ai.grading import FakeGrader, GradingError
from app.main import app

_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+hello\n"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique_repo(label: str) -> str:
    return f"octo/{label}-{secrets.token_hex(4)}"


def _post(client: TestClient, token: str, *, repo: str, diff: str = _DIFF):
    return client.post(
        "/pre-commit/sessions", json={"repo": repo, "diff": diff}, headers=_headers(token)
    )


class _CountingGrader(FakeGrader):
    def __init__(self) -> None:
        self.interpret_calls = 0
        self.exam_calls = 0

    async def interpret(self, diff: str) -> str:
        self.interpret_calls += 1
        return await super().interpret(diff)

    async def generate_exam(self, diff, count, avoid=None):
        self.exam_calls += 1
        return await super().generate_exam(diff, count, avoid)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_first_post_creates_a_pending_session(vibecheck_env) -> None:
    with TestClient(app) as client:
        response = _post(client, vibecheck_env.token, repo=_unique_repo("pc-create"))

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending"
    assert data["question"]
    assert data["session_url"].startswith(f"{vibecheck_env.base_url}/s/")


def test_same_staged_diff_returns_200_with_the_same_url(vibecheck_env) -> None:
    repo = _unique_repo("pc-repeat")
    with TestClient(app) as client:
        first = _post(client, vibecheck_env.token, repo=repo)
        second = _post(client, vibecheck_env.token, repo=repo)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["session_url"] == first.json()["session_url"]


def test_repeat_post_does_not_call_the_grader_again(vibecheck_env) -> None:
    repo = _unique_repo("pc-nogen")
    grader = _CountingGrader()
    with TestClient(app) as client:
        app.state.grader = grader
        _post(client, vibecheck_env.token, repo=repo)
        _post(client, vibecheck_env.token, repo=repo)

    assert (grader.interpret_calls, grader.exam_calls) == (1, 1)


def test_a_different_staged_diff_gets_a_new_session(vibecheck_env) -> None:
    repo = _unique_repo("pc-other")
    with TestClient(app) as client:
        first = _post(client, vibecheck_env.token, repo=repo)
        second = _post(client, vibecheck_env.token, repo=repo, diff=_DIFF + "+again\n")

    assert second.status_code == 201
    assert second.json()["session_url"] != first.json()["session_url"]


def test_a_whitespace_only_difference_is_a_different_session(vibecheck_env) -> None:
    # The identity is the exact staged content; a trailing newline is a real change.
    repo = _unique_repo("pc-ws")
    with TestClient(app) as client:
        first = _post(client, vibecheck_env.token, repo=repo)
        second = _post(client, vibecheck_env.token, repo=repo, diff=_DIFF + "\n")

    assert second.status_code == 201
    assert second.json()["session_url"] != first.json()["session_url"]


def test_the_same_diff_in_another_repo_gets_a_new_session(vibecheck_env) -> None:
    with TestClient(app) as client:
        first = _post(client, vibecheck_env.token, repo=_unique_repo("pc-a"))
        second = _post(client, vibecheck_env.token, repo=_unique_repo("pc-b"))

    assert second.status_code == 201
    assert second.json()["session_url"] != first.json()["session_url"]


def _token(response) -> str:
    return response.json()["session_url"].rsplit("/", 1)[-1]


# The hook's second phase: the re-POST is the only way it learns the verdict,
# so the body must carry the session's stored status, not a fresh "pending".
# _DIFF has one changed line, so its sheet is the single high-level question.


def test_repost_after_passing_reports_passed(vibecheck_env) -> None:
    repo = _unique_repo("pc-passed")
    with TestClient(app) as client:
        token = _token(_post(client, vibecheck_env.token, repo=repo))
        client.post(f"/s/{token}/answer", data={"index": "0", "answer": "looks-good"})
        again = _post(client, vibecheck_env.token, repo=repo)

    assert again.status_code == 200
    assert again.json()["status"] == "passed"


def test_repost_after_failing_reports_failed(vibecheck_env) -> None:
    repo = _unique_repo("pc-failed")
    with TestClient(app) as client:
        token = _token(_post(client, vibecheck_env.token, repo=repo))
        client.post(f"/s/{token}/skip", data={"index": "0"})
        again = _post(client, vibecheck_env.token, repo=repo)

    assert again.status_code == 200
    assert again.json()["status"] == "failed"


def test_missing_bearer_token_is_401(vibecheck_env) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/pre-commit/sessions", json={"repo": _unique_repo("pc-noauth"), "diff": _DIFF}
        )

    assert response.status_code == 401


def test_repo_not_in_allowlist_is_403(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "octo/only-this-one")
    with TestClient(app) as client:
        response = _post(client, vibecheck_env.token, repo=_unique_repo("pc-denied"))

    assert response.status_code == 403


def test_empty_diff_is_422(vibecheck_env) -> None:
    with TestClient(app) as client:
        response = _post(client, vibecheck_env.token, repo=_unique_repo("pc-empty"), diff="  \n")

    assert response.status_code == 422
    assert "diff" in {err["loc"][-1] for err in response.json()["detail"]}


def test_malformed_repo_is_422(vibecheck_env) -> None:
    with TestClient(app) as client:
        response = _post(client, vibecheck_env.token, repo="not-a-repo")

    assert response.status_code == 422
    assert "repo" in {err["loc"][-1] for err in response.json()["detail"]}


def test_evaluator_rejection_is_422_and_creates_no_session(vibecheck_env, database_url) -> None:
    repo = _unique_repo("pc-big")
    diff = "diff --git a/a.py b/a.py\n@@ -0,0 +1,1001 @@\n" + "+x\n" * 1001
    with TestClient(app) as client:
        response = _post(client, vibecheck_env.token, repo=repo, diff=diff)

    assert response.status_code == 422
    assert "1,001 lines" in response.json()["rejection"]
    with psycopg.connect(database_url) as conn:
        count = conn.execute("SELECT count(*) FROM sessions WHERE repo = %s", (repo,)).fetchone()
    assert count == (0,)


def test_creating_a_pre_commit_session_posts_no_commit_status(vibecheck_env) -> None:
    recorder = _RecordingPublisher()
    repo = _unique_repo("pc-nostatus")
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        _post(client, vibecheck_env.token, repo=repo)
        _post(client, vibecheck_env.token, repo=repo)

    assert recorder.calls == []


def test_expired_pending_session_is_reissued_with_a_fresh_link(vibecheck_env, database_url) -> None:
    repo = _unique_repo("pc-expired")
    with TestClient(app) as client:
        first = _post(client, vibecheck_env.token, repo=repo)
        with psycopg.connect(database_url) as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = now() - interval '1 day' WHERE repo = %s",
                (repo,),
            )
            conn.commit()
        second = _post(client, vibecheck_env.token, repo=repo)

    assert second.status_code == 201
    assert second.json()["status"] == "pending"
    assert second.json()["session_url"] != first.json()["session_url"]


def test_grader_failure_on_first_sight_is_502(vibecheck_env) -> None:
    class _Down(FakeGrader):
        async def interpret(self, diff: str) -> str:
            raise GradingError("model unavailable")

    with TestClient(app) as client:
        app.state.grader = _Down()
        response = _post(client, vibecheck_env.token, repo=_unique_repo("pc-down"))

    assert response.status_code == 502
