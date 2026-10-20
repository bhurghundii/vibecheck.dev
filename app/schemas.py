"""Request shapes for the API endpoints.

head_sha/base_sha must be full 40-character SHAs: the unique index and verdict
lookups are exact matches, so a short SHA would create a second session for
the same commit.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, ValidationInfo, field_validator

_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _check_repo(value: str) -> str:
    if not _REPO_RE.match(value):
        raise ValueError("repo must be in 'owner/name' form, with both parts non-empty")
    return value


def _check_diff(value: str) -> str:
    if not value.strip():
        raise ValueError("diff must not be empty")
    return value


class CreateSessionRequest(BaseModel):
    repo: str
    pr_number: int = Field(gt=0)
    head_sha: str
    base_sha: str
    diff: str

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        return _check_repo(value)

    @field_validator("head_sha", "base_sha")
    @classmethod
    def _validate_sha(cls, value: str, info: ValidationInfo) -> str:
        if not _SHA_RE.match(value):
            raise ValueError(f"{info.field_name} must be a 40-character hex SHA")
        return value

    @field_validator("diff")
    @classmethod
    def _validate_diff(cls, value: str) -> str:
        return _check_diff(value)


class CreatePreCommitSessionRequest(BaseModel):
    """Staged changes have no PR and no commit, so the diff itself is the identity."""

    repo: str
    diff: str

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        return _check_repo(value)

    @field_validator("diff")
    @classmethod
    def _validate_diff(cls, value: str) -> str:
        return _check_diff(value)
