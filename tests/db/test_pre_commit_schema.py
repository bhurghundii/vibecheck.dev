"""The V2 schema: the constraints and index that keep the two session kinds
well-formed, and what V2 does to rows written before it.

A PR session must carry its PR number and both SHAs; a pre-commit session
must carry the hash of its staged diff, and a repo can hold only one pre-commit
session per staged diff, since that is what pre-commit idempotency rests on.
These rows are written straight to the table, bypassing the endpoints, because
the endpoints can never produce them: the point is that the database refuses
them even if a future code path tries.

V2 must also leave existing PR sessions intact: they read as 'pr' sessions
afterwards, and the V1 unique index PR idempotency relies on is untouched. The
shared test database is already at V2 by the time any test runs, so that test
builds a throwaway database of its own, applies V1 alone, writes a row, and only
then applies V2.

Kept apart from tests/db/test_migrations.py, which is pure; these need the app
lifespan to apply migrations against a real Postgres.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.db.migrations import run_migrations
from app.main import app

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _insert(database_url: str, **columns) -> None:
    row = {
        "repo": f"octo/schema-{secrets.token_hex(4)}",
        "diff": "diff --git a/x b/x\n+x\n",
        "question": "q?",
        "token": secrets.token_urlsafe(16),
        "status": "pending",
        **columns,
    }
    names = ", ".join(row)
    placeholders = ", ".join(f"%({n})s" for n in row)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            f"INSERT INTO sessions ({names}, expires_at) VALUES ({placeholders}, now())", row
        )
        conn.commit()


def _migrate() -> None:
    with TestClient(app):
        pass


def test_pr_session_without_pr_number_is_refused(vibecheck_env, database_url: str) -> None:
    _migrate()
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(database_url, source="pr", head_sha="1" * 40, base_sha="2" * 40)


def test_pre_commit_session_without_diff_hash_is_refused(vibecheck_env, database_url: str) -> None:
    _migrate()
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(database_url, source="pre_commit")


def test_a_second_pre_commit_session_for_the_same_staged_diff_is_refused(
    vibecheck_env, database_url: str
) -> None:
    _migrate()
    repo = f"octo/schema-{secrets.token_hex(4)}"
    diff_sha256 = secrets.token_hex(32)
    _insert(database_url, source="pre_commit", repo=repo, diff_sha256=diff_sha256)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(database_url, source="pre_commit", repo=repo, diff_sha256=diff_sha256)

    _insert(
        database_url,
        source="pre_commit",
        repo=f"octo/schema-{secrets.token_hex(4)}",
        diff_sha256=diff_sha256,
    )


def _v1_index_definition(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes"
        " WHERE tablename = 'sessions' AND indexname = 'sessions_repo_pr_number_head_sha_key'"
    ).fetchone()
    assert row is not None
    return row[0]


def test_existing_pr_sessions_read_as_pr_sessions_after_v2(
    database_url: str, tmp_path: Path
) -> None:
    dbname = f"vc_{secrets.token_hex(8)}"
    fresh_dsn = make_conninfo(database_url, dbname=dbname)
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    try:
        v1_only = tmp_path / "migrations"
        v1_only.mkdir()
        for v1 in MIGRATIONS_DIR.glob("V1__*.sql"):
            shutil.copy(v1, v1_only)
        asyncio.run(run_migrations(fresh_dsn, v1_only))

        token = secrets.token_urlsafe(16)
        with psycopg.connect(fresh_dsn) as conn:
            conn.execute(
                "INSERT INTO sessions"
                " (repo, pr_number, head_sha, base_sha, diff, question, token, status,"
                " expires_at)"
                " VALUES (%s, 7, %s, %s, 'diff', 'q?', %s, 'pending', now())",
                ("octo/before-v2", "1" * 40, "2" * 40, token),
            )
            conn.commit()
            v1_index = _v1_index_definition(conn)

        assert asyncio.run(run_migrations(fresh_dsn, MIGRATIONS_DIR)) == [
            "V2__pre_commit_sessions.sql"
        ]

        with psycopg.connect(fresh_dsn) as conn:
            source, diff_sha256 = conn.execute(
                "SELECT source, diff_sha256 FROM sessions WHERE token = %s", (token,)
            ).fetchone()
            assert (source, diff_sha256) == ("pr", None)
            assert _v1_index_definition(conn) == v1_index
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )


def test_source_is_limited_to_the_two_session_kinds(vibecheck_env, database_url: str) -> None:
    _migrate()
    # Read from the catalog because no insert can observe this CHECK on its own: any other
    # source already fails sessions_identity_by_source, which names only the two kinds.
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'sessions'::regclass AND conname = 'sessions_source_check'"
        ).fetchone()
    assert row == ("CHECK ((source = ANY (ARRAY['pr'::text, 'pre_commit'::text])))",)
