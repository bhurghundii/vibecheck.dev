"""Lifespan: which grader the real settings select, and the startup line that
says so. The grader classes are tested on their own in tests/ai/; this checks
the wiring from MODEL_PROVIDER and friends to the instance the app uses."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.ai.grading import BedrockGrader, RealGrader
from app.main import app


def _startup_messages() -> tuple[object, list[str]]:
    """Run the lifespan and return the grader plus vibecheck.startup's messages.

    The handler sits on vibecheck.startup itself, not root: configure_logging()
    replaces root.handlers during startup, which would drop a root handler
    before the lines this test is after are emitted."""
    messages: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Capture()
    startup = logging.getLogger("vibecheck.startup")
    startup.addHandler(handler)
    try:
        with TestClient(app):
            grader = app.state.grader
    finally:
        startup.removeHandler(handler)
    return grader, messages


def test_bedrock_provider_builds_a_bedrock_grader_from_settings(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    monkeypatch.setenv("BEDROCK_ENDPOINT_URL", "https://bedrock.vpce.example.com")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-api-key")
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "2048")

    grader, messages = _startup_messages()

    assert isinstance(grader, BedrockGrader)
    assert grader._region == "eu-north-1"
    assert grader._bearer_token == "test-bedrock-api-key"
    assert grader._max_tokens == 2048
    assert grader._url == (
        "https://bedrock.vpce.example.com/model/"
        "eu.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse"
    )
    assert (
        "grading via Amazon Bedrock model eu.anthropic.claude-haiku-4-5-20251001-v1:0" in messages
    )


def test_bedrock_without_an_api_key_uses_the_credential_chain(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    grader, _ = _startup_messages()

    assert isinstance(grader, BedrockGrader)
    assert grader._bearer_token is None


def test_anthropic_provider_builds_the_messages_api_grader(vibecheck_env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "test-anthropic-key")

    grader, messages = _startup_messages()

    assert type(grader) is RealGrader
    assert not any("Amazon Bedrock" in m for m in messages)
