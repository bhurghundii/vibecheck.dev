"""scripts/pre_commit_hook.py: what the hook sends, and which exit code each
server answer turns into.

The hook is driven in-process through main() against a stub HTTP server on
localhost, not as a subprocess, so coverage sees it and no vibecheck server or
database is needed. The staged-diff tests use a real throwaway git repo,
because "sends the staged diff, not the working tree" is a property of the git
invocation and a fake would only test itself.

Fail-open is the default: anything that isn't the server's own verdict lets
the commit through with a warning. Only pending, failed and a 422 rejection
block.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import pre_commit_hook as hook

_DIFF = "diff --git a/x b/x\n+x\n"

# The warning from the request's own except clause. Tests of a specific failure
# mode assert this rather than any warning: main()'s catch-all also warns and
# exits 0, so "a warning appeared" can't tell which clause handled it.
_UNREACHABLE = "could not reach the vibecheck server"


@dataclass
class _Stub:
    code: int = 200
    body: dict = field(default_factory=lambda: {"status": "passed", "session_url": "u"})
    delay: float = 0.0
    requests: list[dict] = field(default_factory=list)
    url: str = ""


@pytest.fixture()
def stub() -> Iterator[_Stub]:
    state = _Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            state.requests.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            time.sleep(state.delay)
            payload = json.dumps(state.body).encode()
            try:
                self.send_response(state.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up first: the timeout test.

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _env(stub: _Stub, **extra) -> dict:
    return {
        "VIBECHECK_BASE_URL": stub.url,
        "VIBECHECK_TOKEN": "t0ken",
        "VIBECHECK_REPO": "octo/repo",
        **extra,
    }


def _run(stub: _Stub, *, env: dict | None = None, timeout: float = 5.0, diff: str = _DIFF) -> int:
    return hook.main(env=env if env is not None else _env(stub), timeout=timeout, diff=diff)


# --- exit codes ---------------------------------------------------------------


def test_passed_session_lets_the_commit_through(stub, capsys) -> None:
    stub.body = {"status": "passed", "session_url": "https://vc/s/abc"}

    assert _run(stub) == 0
    err = capsys.readouterr().err
    # Every fail-open path also exits 0, so the exit code alone can't show that
    # the passed branch is the one that let the commit through.
    assert "passed — committing." in err
    assert "warning" not in err.lower()


def test_posts_repo_and_diff_with_the_bearer_token(stub) -> None:
    _run(stub)

    assert stub.requests == [
        {
            "path": "/pre-commit/sessions",
            "auth": "Bearer t0ken",
            "body": {"repo": "octo/repo", "diff": _DIFF},
        }
    ]


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_undecided_or_failed_session_blocks_and_prints_the_link(stub, capsys, status) -> None:
    stub.code = 201
    stub.body = {"status": status, "session_url": "https://vc/s/abc"}

    assert _run(stub) == 1
    assert "https://vc/s/abc" in capsys.readouterr().err


def test_rejection_blocks_with_the_servers_reason(stub, capsys) -> None:
    stub.code = 422
    stub.body = {"rejection": "diff touches 1,001 lines"}

    assert _run(stub) == 1
    assert "diff touches 1,001 lines" in capsys.readouterr().err


@pytest.mark.parametrize("code", [401, 403, 413, 500, 502])
def test_other_server_errors_warn_and_let_the_commit_through(stub, capsys, code) -> None:
    stub.code = code
    stub.body = {"detail": "nope"}

    assert _run(stub) == 0
    assert "the vibecheck server answered HTTP" in capsys.readouterr().err


def test_a_422_without_a_rejection_fails_open(stub, capsys) -> None:
    stub.code = 422
    stub.body = {"detail": [{"loc": ["body", "repo"]}]}

    assert _run(stub) == 0
    assert "the vibecheck server answered HTTP" in capsys.readouterr().err


def test_unreachable_server_warns_and_lets_the_commit_through(stub, capsys) -> None:
    env = _env(stub, VIBECHECK_BASE_URL="http://127.0.0.1:1")

    assert _run(stub, env=env) == 0
    assert _UNREACHABLE in capsys.readouterr().err


def test_slow_server_times_out_and_lets_the_commit_through(stub, capsys) -> None:
    stub.delay = 1.0
    started = time.monotonic()

    assert _run(stub, timeout=0.2) == 0
    assert time.monotonic() - started < 0.9
    assert _UNREACHABLE in capsys.readouterr().err


def test_unparseable_response_fails_open(stub, capsys) -> None:
    stub.body = ["not", "an", "object"]

    assert _run(stub) == 0
    assert "unexpected response from the vibecheck server" in capsys.readouterr().err


@pytest.fixture()
def raw_server() -> Iterator[list]:
    """A TCP server that answers every connection with the bytes in reply[0],
    for responses http.server can't be made to send: not HTTP at all, or cut off."""
    reply: list = [b""]
    sock = socket.create_server(("127.0.0.1", 0))
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                conn.recv(65536)
                conn.sendall(reply[0])
                # Half-close and drain the rest of the request before closing:
                # closing with unread bytes sends a RST, which the client sees
                # as a connection reset instead of the malformed reply.
                conn.shutdown(socket.SHUT_WR)
                try:
                    while conn.recv(65536):
                        pass
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    reply.append(f"http://127.0.0.1:{sock.getsockname()[1]}")
    try:
        yield reply
    finally:
        stop.set()
        thread.join()
        sock.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"this is not http\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"sta",
    ],
    ids=["not-http", "truncated-body"],
)
def test_a_malformed_response_fails_open(stub, raw_server, capsys, payload) -> None:
    raw_server[0] = payload
    env = _env(stub, VIBECHECK_BASE_URL=raw_server[1])

    assert _run(stub, env=env) == 0
    assert _UNREACHABLE in capsys.readouterr().err


def test_deeply_nested_json_fails_open(stub, raw_server, capsys) -> None:
    # Valid JSON that json.loads can't parse without blowing the recursion limit.
    body = b"[" * 200_000 + b"]" * 200_000
    raw_server[0] = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    env = _env(stub, VIBECHECK_BASE_URL=raw_server[1])

    assert _run(stub, env=env) == 0
    assert _UNREACHABLE in capsys.readouterr().err


def test_a_200_that_is_not_json_fails_open(stub, raw_server, capsys) -> None:
    raw_server[0] = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5\r\n\r\nhello"
    env = _env(stub, VIBECHECK_BASE_URL=raw_server[1])

    assert _run(stub, env=env) == 0
    assert _UNREACHABLE in capsys.readouterr().err


def test_the_default_timeout_is_bounded() -> None:
    assert 0 < hook.DEFAULT_TIMEOUT_SECONDS <= 300


def test_the_default_timeout_is_120_seconds() -> None:
    # Every other test passes timeout= explicitly, so main()'s own default is
    # only observed here.
    assert hook.DEFAULT_TIMEOUT_SECONDS == 120
    assert inspect.signature(hook.main).parameters["timeout"].default == 120


# --- configuration --------------------------------------------------------------


@pytest.mark.parametrize("missing", ["VIBECHECK_BASE_URL", "VIBECHECK_TOKEN"])
def test_missing_config_warns_and_sends_nothing(stub, capsys, missing) -> None:
    env = _env(stub)
    del env[missing]

    assert _run(stub, env=env) == 0
    assert f"{missing} is not set, so this commit was not checked" in capsys.readouterr().err
    assert stub.requests == []


def test_empty_staged_diff_sends_nothing(stub) -> None:
    assert _run(stub, diff="") == 0
    assert stub.requests == []


def test_no_resolvable_repo_warns_and_sends_nothing(stub, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = _env(stub)
    del env["VIBECHECK_REPO"]

    assert _run(stub, env=env) == 0
    assert "origin is not a GitHub remote" in capsys.readouterr().err
    assert stub.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/repo",
        "https://github.com/octo/repo.git",
        "https://github.com/octo/repo/",
        "git@github.com:octo/repo.git",
        "ssh://git@github.com/octo/repo.git",
    ],
)
def test_github_remotes_parse_to_owner_and_name(url) -> None:
    assert hook.parse_github_repo(url) == "octo/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/octo/repo.git",
        "git@bitbucket.org:octo/repo.git",
        "https://github.com/octo",
        "https://github.com/octo/repo/tree/main",
        "",
    ],
)
def test_non_github_or_malformed_remotes_do_not_parse(url) -> None:
    assert hook.parse_github_repo(url) is None


# --- against a real git repo --------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "remote", "add", "origin", "git@github.com:octo/from-remote.git")
    (tmp_path / "staged.txt").write_text("old\n")
    (tmp_path / "unstaged.txt").write_text("old\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_sends_the_staged_diff_not_the_working_tree(stub, repo: Path) -> None:
    (repo / "staged.txt").write_text("staged change\n")
    _git(repo, "add", "staged.txt")
    (repo / "unstaged.txt").write_text("unstaged change\n")

    hook.main(env=_env(stub), timeout=5.0)

    sent = stub.requests[0]["body"]["diff"]
    assert "+staged change" in sent
    assert "unstaged change" not in sent
    assert sent.startswith("diff --git a/staged.txt b/staged.txt")


def test_a_coloured_diff_config_does_not_reach_the_server(stub, repo: Path) -> None:
    _git(repo, "config", "color.ui", "always")
    _git(repo, "config", "color.diff", "always")
    (repo / "staged.txt").write_text("staged change\n")
    _git(repo, "add", "staged.txt")

    hook.main(env=_env(stub), timeout=5.0)

    sent = stub.requests[0]["body"]["diff"]
    assert "\x1b[" not in sent
    assert sent.startswith("diff --git")


def test_an_external_diff_driver_does_not_reach_the_server(
    stub, repo: Path, tmp_path_factory
) -> None:
    # The driver lives outside the test repo so it never shows up as a change.
    driver = tmp_path_factory.mktemp("driver") / "external-diff"
    driver.write_text("#!/bin/sh\necho EXTERNAL-DIFF\n")
    driver.chmod(0o755)
    _git(repo, "config", "diff.external", str(driver))
    (repo / "staged.txt").write_text("staged change\n")
    _git(repo, "add", "staged.txt")

    hook.main(env=_env(stub), timeout=5.0)

    sent = stub.requests[0]["body"]["diff"]
    assert "EXTERNAL-DIFF" not in sent
    assert sent.startswith("diff --git")


def test_nothing_staged_sends_nothing(stub, repo: Path) -> None:
    (repo / "unstaged.txt").write_text("unstaged change\n")

    assert hook.main(env=_env(stub), timeout=5.0) == 0
    assert stub.requests == []


def test_repo_comes_from_origin_when_not_set(stub, repo: Path) -> None:
    (repo / "staged.txt").write_text("staged change\n")
    _git(repo, "add", "staged.txt")
    env = _env(stub)
    del env["VIBECHECK_REPO"]

    hook.main(env=env, timeout=5.0)

    assert stub.requests[0]["body"]["repo"] == "octo/from-remote"


def test_non_utf8_staged_content_fails_open_when_the_server_is_down(stub, repo, capsys) -> None:
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    _git(repo, "add", "latin1.txt")

    assert hook.main(env=_env(stub, VIBECHECK_BASE_URL="http://127.0.0.1:1"), timeout=5.0) == 0
    assert _UNREACHABLE in capsys.readouterr().err


def test_non_utf8_staged_content_is_still_sent_for_checking(stub, repo) -> None:
    # Fail-open alone would pass even if the diff were never read; the point of
    # the lenient decode is that the change still gets quizzed.
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    _git(repo, "add", "latin1.txt")

    code = hook.main(env=_env(stub), timeout=5.0)

    assert len(stub.requests) == 1
    assert "+caf�" in stub.requests[0]["body"]["diff"]
    assert code == 0


def test_failing_git_diff_warns_and_sends_nothing(stub, capsys, tmp_path, monkeypatch) -> None:
    # Outside any repository `git diff --cached` exits non-zero.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert hook.main(env=_env(stub), timeout=5.0) == 0
    assert "`git diff --cached` failed" in capsys.readouterr().err
    assert stub.requests == []


# --- anything unexpected --------------------------------------------------------------


def test_an_unexpected_error_warns_and_lets_the_commit_through(stub, capsys, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(hook, "_post", explode)

    assert _run(stub) == 0
    assert "the hook failed unexpectedly" in capsys.readouterr().err


def test_ctrl_c_still_aborts_the_commit(stub, monkeypatch) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(hook, "_post", interrupt)

    with pytest.raises(KeyboardInterrupt):
        _run(stub)


def test_an_unknown_status_on_200_warns_and_lets_the_commit_through(stub, capsys) -> None:
    stub.body = {"status": "mystery", "session_url": "https://vc/s/abc"}

    assert _run(stub) == 0
    assert "the vibecheck server answered HTTP" in capsys.readouterr().err


def test_the_script_is_executable() -> None:
    path = Path(hook.__file__)
    assert path.stat().st_mode & 0o111
    assert path.read_text().startswith("#!/usr/bin/env python3\n")


def test_the_hook_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(hook.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    assert names
    assert all(name in sys.stdlib_module_names or name == "__future__" for name in names), names


def test_the_pre_commit_manifest_declares_the_hook_as_the_ticket_requires() -> None:
    # Read with the standard library: yaml is only importable here transitively
    # and isn't a declared dependency, and the manifest is one flat mapping.
    manifest = Path(hook.__file__).parents[1] / ".pre-commit-hooks.yaml"
    fields = {}
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("- ")
        key, _, value = line.partition(": ")
        fields[key] = value

    assert fields["id"] == "vibecheck"
    assert fields["entry"] == "scripts/pre_commit_hook.py"
    assert fields["language"] == "script"
    assert fields["minimum_pre_commit_version"] == "'3.2.0'"
    assert fields["stages"] == "[pre-commit]"
    assert fields["verbose"] == "true"
    assert fields["pass_filenames"] == "false"
    assert fields["always_run"] == "true"
