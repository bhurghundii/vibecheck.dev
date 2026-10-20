"""Unit tests for BedrockGrader: the Converse request it builds, SigV4 and
bearer-token auth, and how a Converse reply maps onto the shared parsing.

All against a mocked HTTP transport and static credentials — no AWS account,
no network. The prompt logic it shares with RealGrader is covered in
test_grading.py.
"""

from __future__ import annotations

import json

import httpx
import pytest
from botocore.credentials import Credentials

from app.ai.grading import HIGH_LEVEL_QUESTION, BedrockGrader, GradingError

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def _converse_response(text: str, *, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": stop_reason,
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        },
    )


def _grader(requests: list[httpx.Request], *responses: httpx.Response, **kwargs) -> BedrockGrader:
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return remaining.pop(0)

    transport = httpx.MockTransport(handler)
    kwargs.setdefault("credentials_provider", lambda: Credentials("AKIDEXAMPLE", "secret"))
    return BedrockGrader(
        MODEL_ID,
        "us-east-1",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        **kwargs,
    )


@pytest.mark.anyio
async def test_calls_converse_for_the_model_with_a_sigv4_signature() -> None:
    requests: list[httpx.Request] = []
    grader = _grader(requests, _converse_response("- adds a TTL"))
    assert await grader.interpret("the diff") == "- adds a TTL"

    request = requests[0]
    assert request.url.host == "bedrock-runtime.us-east-1.amazonaws.com"
    assert request.url.raw_path.decode() == (
        "/model/us.anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse"
    )
    auth = request.headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in auth
    assert "x-amz-date" in request.headers

    body = json.loads(request.content)
    assert "the diff" in body["messages"][0]["content"][0]["text"]
    assert body["system"][0]["text"]
    assert body["inferenceConfig"]["maxTokens"] == 4096


@pytest.mark.anyio
async def test_session_token_is_signed_in() -> None:
    requests: list[httpx.Request] = []
    grader = _grader(
        requests,
        _converse_response("x"),
        credentials_provider=lambda: Credentials("AKID", "secret", "the-session-token"),
    )
    await grader.interpret("diff")
    assert requests[0].headers["x-amz-security-token"] == "the-session-token"


@pytest.mark.anyio
async def test_bearer_token_skips_sigv4() -> None:
    def no_credentials():
        raise AssertionError("the credential chain is not consulted with a bearer token")

    requests: list[httpx.Request] = []
    grader = _grader(
        requests,
        _converse_response("x"),
        bearer_token="bedrock-api-key",
        credentials_provider=no_credentials,
    )
    await grader.interpret("diff")
    assert requests[0].headers["authorization"] == "Bearer bedrock-api-key"


@pytest.mark.anyio
async def test_missing_credentials_is_a_grading_error() -> None:
    grader = _grader([], credentials_provider=lambda: None)
    with pytest.raises(GradingError, match="no AWS credentials"):
        await grader.interpret("diff")


@pytest.mark.anyio
async def test_endpoint_url_override() -> None:
    requests: list[httpx.Request] = []
    grader = _grader(
        requests, _converse_response("x"), endpoint_url="https://vpce.example.internal/"
    )
    await grader.interpret("diff")
    assert str(requests[0].url).startswith("https://vpce.example.internal/model/")


@pytest.mark.anyio
async def test_schema_is_asked_for_in_the_system_prompt() -> None:
    requests: list[httpx.Request] = []
    reply = json.dumps(
        {"questions": [{"question": "q2", "start_line": 1, "end_line": 2, "reference_answer": "r"}]}
    )
    grader = _grader(requests, _converse_response(f"```json\n{reply}\n```"))
    questions = await grader.generate_exam("l1\nl2", 2)

    assert [q.question for q in questions] == [HIGH_LEVEL_QUESTION, "q2"]
    system = json.loads(requests[0].content)["system"][0]["text"]
    assert "JSON Schema" in system and '"questions"' in system


@pytest.mark.anyio
async def test_mark_records_the_bedrock_model() -> None:
    grader = _grader([], _converse_response(json.dumps({"passed": True, "note": "ok"})))
    mark = await grader.grade_answer("interp", "q", "a")
    assert mark.passed is True
    assert mark.model == MODEL_ID


@pytest.mark.anyio
async def test_skips_reasoning_blocks() -> None:
    response = httpx.Response(
        200,
        json={
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
                        {"text": "the explanation"},
                    ],
                }
            },
            "stopReason": "end_turn",
        },
    )
    grader = _grader([], response)
    assert await grader.explain("interp", "q") == "the explanation"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stop_reason", "match"),
    [
        ("guardrail_intervened", "declined"),
        ("content_filtered", "declined"),
        ("max_tokens", "4096-token cap"),
    ],
)
async def test_unusable_stop_reasons_raise(stop_reason: str, match: str) -> None:
    grader = _grader([], _converse_response("partial", stop_reason=stop_reason))
    with pytest.raises(GradingError, match=match):
        await grader.interpret("diff")


@pytest.mark.anyio
async def test_access_denied_surfaces_the_aws_message() -> None:
    denied = httpx.Response(
        403, json={"message": "You don't have access to the model with the specified model ID."}
    )
    grader = _grader([], denied)
    with pytest.raises(GradingError, match="403.*have access to the model"):
        await grader.interpret("diff")


@pytest.mark.anyio
async def test_retries_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import grading

    async def fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr(grading.asyncio, "sleep", fake_sleep)
    requests: list[httpx.Request] = []
    grader = _grader(
        requests,
        httpx.Response(429, json={"message": "ThrottlingException"}),
        _converse_response("ok"),
    )
    assert await grader.interpret("diff") == "ok"
    assert len(requests) == 2
