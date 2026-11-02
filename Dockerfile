# syntax=docker/dockerfile:1

# ---- builder ----
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# WORKDIR matches the runtime stage exactly. uv bakes an absolute path into
# the venv's console-script shebangs (#!/app/.venv/bin/python); as long as
# both stages agree on WORKDIR the shebang stays valid after the COPY below.
# We still invoke via `python -m uvicorn` at the end (never the console
# script) as a second, independent guard against this class of bug.
WORKDIR /app

COPY pyproject.toml uv.lock ./
# No BuildKit cache mount here: Railway's build farm requires cache mounts
# to carry an id scoped to its own internal service ID
# (--mount=type=cache,id=s/<service-id>-/...), which would hardcode a
# Railway-specific, regenerable value into a Dockerfile that also needs to
# build with plain `docker build`/`docker compose` elsewhere. Losing the
# cache just means uv re-populates its package cache each build — slower,
# not broken.
# --no-install-project: the source isn't copied yet, and the server runs it
# straight from /app anyway. The project itself is only installed as a package
# for the local `vibecheck` CLI. --no-dev keeps the default `server` group.
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY assets ./assets

# ---- runtime ----
FROM python:3.14-slim AS runtime

RUN groupadd --system vibecheck && useradd --system --gid vibecheck --home /app vibecheck

WORKDIR /app

COPY --from=builder --chown=vibecheck:vibecheck /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER vibecheck

EXPOSE 8000

# No curl in the slim image — a stdlib one-liner avoids adding a package
# just for the healthcheck.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
