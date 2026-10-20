# Local development

```sh
make dev    # starts Postgres via compose, runs the app locally with uv --reload
make test   # uv run pytest (spins up its own Postgres via Testcontainers)
make seed   # inserts a realistic session directly into Postgres, prints its URL
make eval   # runs app/ai/evals/cases/ against the real grader (needs MODEL_API_KEY the first time)
make down   # docker compose down
```

!!! note
    Postgres runs on host port **5433**, not 5432, to avoid colliding with a local Postgres you might already have running. Inside Docker Compose's own network, the app still talks to `db` on the normal 5432 — this only affects connecting from the host (e.g. `psql localhost:5433`).

Migrations (`migrations/*.sql`) apply automatically on startup via a small built-in runner — no Alembic, no manual step.

## Tech stack

Python, FastAPI, `uv`, psycopg3 (async, raw SQL, no ORM), Postgres 16, server-rendered Jinja2 templates. The only client-side JavaScript is a small paste guard on the answer box (`app/web/static/nopaste.js`); every page works without it. Grading calls the Anthropic Messages API, or Amazon Bedrock's Converse API (signed with `botocore`), directly over async httpx. See `pyproject.toml` for exact versions.
