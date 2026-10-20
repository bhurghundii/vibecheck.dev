-- V2__pre_commit_sessions.sql
-- A second kind of session, opened by the git pre-commit hook on staged
-- changes that have no PR and no commit yet. Such a session is identified by
-- (repo, sha256 of the staged diff) instead of (repo, PR, head SHA).

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'pr'
        CHECK (source IN ('pr', 'pre_commit')),
    ADD COLUMN IF NOT EXISTS diff_sha256 TEXT;

ALTER TABLE sessions
    ALTER COLUMN pr_number DROP NOT NULL,
    ALTER COLUMN head_sha DROP NOT NULL,
    ALTER COLUMN base_sha DROP NOT NULL;

-- The NOT NULLs dropped above move here, scoped to the kind of session that
-- needs them. Every existing row is a 'pr' row with all three set, so this
-- validates cleanly.
ALTER TABLE sessions
    ADD CONSTRAINT sessions_identity_by_source CHECK (
        (source = 'pr' AND pr_number IS NOT NULL AND head_sha IS NOT NULL AND base_sha IS NOT NULL)
        OR (source = 'pre_commit' AND diff_sha256 IS NOT NULL)
    );

-- Idempotent pre-commit session creation relies on this:
-- INSERT ... ON CONFLICT (repo, diff_sha256) WHERE source = 'pre_commit'.
-- The V1 index on (repo, pr_number, head_sha) is left alone: pre-commit rows
-- have NULLs there, and NULLs never collide in a unique index, so PR
-- idempotency is unchanged.
CREATE UNIQUE INDEX IF NOT EXISTS sessions_pre_commit_repo_diff_sha256_key
    ON sessions (repo, diff_sha256) WHERE source = 'pre_commit';
