"""Settings validation: VIBECHECK_TOKEN strength and the VIBECHECK_ALLOWED_REPOS
allow-list. Pure pydantic validation, no DB involved — deliberately doesn't
pull in the Testcontainers-backed vibecheck_env fixture chain, since none of
this needs a real Postgres.
"""

from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch, *, token: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("VIBECHECK_BASE_URL", "https://vibecheck.example.com")
    monkeypatch.setenv("VIBECHECK_TOKEN", token)
    monkeypatch.setenv("FAKE_GRADER", "true")


def test_placeholder_token_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="changeme-generate-a-real-token")
    with pytest.raises(ValidationError, match="placeholder"):
        Settings()


def test_short_token_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="short-token-12")  # 14 chars
    with pytest.raises(ValidationError, match="too short"):
        Settings()


def test_generated_token_is_accepted(monkeypatch) -> None:
    # Mirrors tests/conftest.py's vibecheck_env fixture generation — a
    # regression proof that the new length floor doesn't break it.
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    Settings()


def test_get_settings_wraps_weak_token_as_system_exit(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="too-short")
    with pytest.raises(SystemExit, match="VIBECHECK_TOKEN"):
        get_settings()


def test_get_settings_error_does_not_echo_secrets(monkeypatch) -> None:
    # A model-level validator failing used to put every input value in the
    # exit message, which lands in the deployment's log. pydantic truncated
    # it, but kept the tail of whichever value came last — often a secret.
    # Which one depends on env and .env merge order, so assert the dump is
    # gone entirely, not just that a particular secret is missing from it.
    token = secrets.token_urlsafe(16)
    _set_required_env(monkeypatch, token=token)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:db-password-xyz@localhost:5432/db")
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.setenv("MODEL_API_KEY", "sk-ant-api03-must-not-appear-in-errors")
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "ghp_must-not-appear-in-errors")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "10")
    with pytest.raises(SystemExit, match="MAX_REQUEST_BODY_BYTES") as exc_info:
        get_settings()
    message = str(exc_info.value)
    assert "input_value" not in message
    # The tails, not the whole values: a truncated dump only keeps the ends.
    for secret in (
        token,
        "db-password-xyz",
        "sk-ant-api03-must-not-appear-in-errors",
        "ghp_must-not-appear-in-errors",
    ):
        assert secret[-8:] not in message


def test_settings_repr_hides_secrets(monkeypatch) -> None:
    token = secrets.token_urlsafe(16)
    _set_required_env(monkeypatch, token=token)
    monkeypatch.setenv("MODEL_API_KEY", "sk-ant-api03-hidden")
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "ghp_hidden")
    shown = repr(Settings())
    for secret in (token, "u:p@", "sk-ant-api03-hidden", "ghp_hidden"):
        assert secret not in shown


def test_allowed_repos_unset_permits_any_repo(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.is_repo_allowed("anything/at-all")


@pytest.mark.parametrize("raw", ["", "  ", ",", " , , "])
def test_allowed_repos_blank_string_permits_any_repo(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", raw)
    settings = Settings()
    assert settings.is_repo_allowed("anything/at-all")


def test_allowed_repos_permits_listed_repos(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "octo/repo-a,octo/repo-b")
    settings = Settings()
    assert settings.is_repo_allowed("octo/repo-a")
    assert settings.is_repo_allowed("octo/repo-b")


def test_allowed_repos_rejects_unlisted_repo(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "octo/repo-a,octo/repo-b")
    settings = Settings()
    assert not settings.is_repo_allowed("evil/other")


def test_allowed_repos_trims_whitespace_around_entries(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", " octo/repo-a , octo/repo-b ")
    settings = Settings()
    assert settings.is_repo_allowed("octo/repo-a")
    assert settings.is_repo_allowed("octo/repo-b")


def test_allowed_repos_rejects_malformed_entry(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_ALLOWED_REPOS", "not-a-valid-repo")
    with pytest.raises(ValidationError, match="not-a-valid-repo"):
        Settings()


def test_body_size_defaults_are_generous(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.max_request_body_bytes > settings.max_diff_bytes
    assert settings.max_request_body_bytes > settings.max_answer_bytes


def test_max_request_body_bytes_below_max_diff_bytes_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "100")
    with pytest.raises(ValidationError, match="MAX_REQUEST_BODY_BYTES"):
        Settings()


def test_max_request_body_bytes_below_max_answer_bytes_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    # Above max_diff_bytes (400_000) but below the max_answer_bytes
    # default (20_000) is impossible, so raise max_diff_bytes too — this
    # isolates the second half of the validator from the first.
    monkeypatch.setenv("MAX_DIFF_BYTES", "100")
    monkeypatch.setenv("MAX_ANSWER_BYTES", "1000")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    with pytest.raises(ValidationError, match="MAX_REQUEST_BODY_BYTES"):
        Settings()




def test_meaniemode_defaults_to_false(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.meaniemode is False


def test_meaniemode_can_be_turned_on(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MEANIEMODE", "true")
    settings = Settings()
    assert settings.meaniemode is True


def test_exam_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.exam_question_count == 5
    assert settings.passingmarks == 3
    assert settings.max_question_attempts == 3


@pytest.mark.parametrize("value", ["0", "-1"])
def test_max_question_attempts_below_one_is_rejected(monkeypatch, value) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MAX_QUESTION_ATTEMPTS", value)
    with pytest.raises(ValidationError):
        Settings()


def test_passingmarks_over_question_count_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EXAM_QUESTION_COUNT", "3")
    monkeypatch.setenv("PASSINGMARKS", "4")
    with pytest.raises(ValidationError, match="PASSINGMARKS"):
        Settings()


def test_passingmarks_equal_to_question_count_is_allowed(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EXAM_QUESTION_COUNT", "4")
    monkeypatch.setenv("PASSINGMARKS", "4")
    assert Settings().passingmarks == 4


@pytest.mark.parametrize("value", ["0", "-1"])
def test_passingmarks_below_one_is_rejected(monkeypatch, value) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("PASSINGMARKS", value)
    with pytest.raises(ValidationError):
        Settings()


def test_passing_marks_for_clamps_to_a_short_sheet(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EXAM_QUESTION_COUNT", "5")
    monkeypatch.setenv("PASSINGMARKS", "3")
    settings = Settings()
    assert settings.passing_marks_for(2) == 2
    assert settings.passing_marks_for(5) == 3


def test_evaluator_unset_uses_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    size = Settings().evaluator_config().size
    assert size.max_changed_lines == 1000
    assert "*.lock" in size.exclude


@pytest.mark.parametrize("raw", ["", "   "])
def test_evaluator_blank_string_uses_defaults(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", raw)
    assert Settings().evaluator_config().size.max_changed_lines == 1000


def test_evaluator_partial_json_keeps_other_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", '{"size": {"max_changed_lines": 3000}}')
    size = Settings().evaluator_config().size
    assert size.max_changed_lines == 3000
    assert "*.lock" in size.exclude


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"size": {"max_changed_line": 5}}',  # typo: must not silently default
        '{"sizes": {}}',
        '{"size": {"max_changed_lines": -1}}',
        '{"size": {"max_changed_lines": "lots"}}',
        '{"size": {"exclude": "*.lock"}}',
    ],
)
def test_evaluator_rejects_invalid_config(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", raw)
    with pytest.raises(SystemExit, match="EVALUATOR is invalid"):
        get_settings()


def _legacy_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Run from an empty directory so a developer's own .env, which may still
    # hold the old GRUMPY_* names, cannot leak into these tests.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("FAKE_GRADER", "true")
    for name in ("BASE_URL", "TOKEN", "ALLOWED_REPOS"):
        monkeypatch.delenv(f"VIBECHECK_{name}", raising=False)


def test_old_grumpy_env_names_still_configure_the_server(monkeypatch, tmp_path) -> None:
    _legacy_env(monkeypatch, tmp_path)
    token = secrets.token_urlsafe(16)
    monkeypatch.setenv("GRUMPY_BASE_URL", "https://old.example.com")
    monkeypatch.setenv("GRUMPY_TOKEN", token)
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/repo")

    settings = Settings()

    assert settings.vibecheck_base_url == "https://old.example.com"
    assert settings.vibecheck_token.get_secret_value() == token
    assert settings.is_repo_allowed("octo/repo")
    assert not settings.is_repo_allowed("octo/other")


def test_vibecheck_env_names_win_over_the_old_ones(monkeypatch, tmp_path) -> None:
    _legacy_env(monkeypatch, tmp_path)
    new_token = secrets.token_urlsafe(16)
    monkeypatch.setenv("GRUMPY_BASE_URL", "https://old.example.com")
    monkeypatch.setenv("GRUMPY_TOKEN", secrets.token_urlsafe(16))
    monkeypatch.setenv("VIBECHECK_BASE_URL", "https://new.example.com")
    monkeypatch.setenv("VIBECHECK_TOKEN", new_token)

    settings = Settings()

    assert settings.vibecheck_base_url == "https://new.example.com"
    assert settings.vibecheck_token.get_secret_value() == new_token


def test_missing_token_is_reported_under_the_new_name(monkeypatch, tmp_path) -> None:
    _legacy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIBECHECK_BASE_URL", "https://new.example.com")

    with pytest.raises(SystemExit, match="VIBECHECK_TOKEN"):
        get_settings()


def test_anthropic_is_the_default_provider(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MODEL_PROVIDER", "")
    assert Settings().model_provider == "anthropic"


def test_bedrock_needs_no_model_api_key(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    settings = Settings()
    assert settings.model_provider == "bedrock"
    assert settings.aws_region == "eu-west-1"


def test_bedrock_requires_model_id_and_region(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("FAKE_GRADER", "false")
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(ValidationError, match="BEDROCK_MODEL_ID and AWS_REGION"):
        Settings()


def test_unknown_provider_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    with pytest.raises(ValidationError, match="model_provider"):
        Settings()
