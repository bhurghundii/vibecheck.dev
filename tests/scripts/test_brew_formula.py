"""scripts/brew_formula.py: the Homebrew formula for the local CLI. Run over
the real uv.lock, because the point is that the formula carries the CLI's
locked dependencies and none of the server's."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.brew_formula import render, runtime_closure

LOCK = tomllib.loads((Path(__file__).resolve().parents[2] / "uv.lock").read_text())


def test_closure_follows_httpx_and_leaves_out_the_server() -> None:
    names = {p["name"] for p in runtime_closure(LOCK)}
    assert {"httpx", "httpcore", "h11", "anyio", "idna", "certifi"} <= names
    assert not names & {"fastapi", "psycopg", "uvicorn", "pydantic", "pytest", "vibecheck-dev"}


def test_formula_pins_the_tag_tarball_and_locked_sdists() -> None:
    formula = render(LOCK, version="1.2.3", sha256="abc123")
    assert 'url "https://github.com/bhurghundii/vibecheck.dev/archive/refs/tags/v1.2.3.tar.gz"' in formula
    assert 'sha256 "abc123"' in formula
    httpx = next(p for p in LOCK["package"] if p["name"] == "httpx")
    assert f'url "{httpx["sdist"]["url"]}"' in formula
    assert f'sha256 "{httpx["sdist"]["hash"].removeprefix("sha256:")}"' in formula
