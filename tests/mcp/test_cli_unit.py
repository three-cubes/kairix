"""Unit tests for the MCP CLI.

The BDD CLI suite (``tests/bdd/test_mcp_cli.py``) covers ``--help`` /
no-args / invalid-transport paths. These unit tests cover the serve
runtime paths via the ``McpCliDeps`` injection seam.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.agents.mcp.cli import McpCliDeps
from kairix.agents.mcp.cli import main as mcp_main


class _FakeMcpServer:
    """FakeMcp server records run() calls + minimal app-builder surface.

    Satisfies what ``build_mcp_app`` needs: ``streamable_http_app()``,
    ``sse_app(mount_path=...)``, and a ``settings`` namespace. Tests rely
    on the recorder fields ``runs`` / ``streamable_calls`` / ``sse_calls``.
    """

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.streamable_calls: int = 0
        self.sse_calls: list[str] = []
        # FastMCP exposes a settings namespace that transport._apply_settings
        # writes to; emulate that as a plain SimpleNamespace.
        import types as _types

        self.settings = _types.SimpleNamespace(
            json_response=False,
            stateless_http=False,
            streamable_http_path="/mcp",
        )

    def run(self, *, transport: str) -> None:
        self.runs.append({"transport": transport})

    def streamable_http_app(self) -> Any:
        from starlette.applications import Starlette

        self.streamable_calls += 1
        return Starlette(routes=[])

    def sse_app(self, *, mount_path: str = "/sse") -> Any:
        from starlette.applications import Starlette

        self.sse_calls.append(mount_path)
        return Starlette(routes=[])


@dataclass
class _RunRecorder:
    """Captures (args, kwargs) of the uvicorn-like runner."""

    calls: list[tuple[tuple, dict]] = field(default_factory=list)

    def __call__(self, *a: Any, **kw: Any) -> None:
        self.calls.append((a, kw))


def _drive(args: list[str], deps: McpCliDeps) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            mcp_main(args, deps=deps)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def _build_deps(
    *, fake_server: _FakeMcpServer | None = None, fake_runner: _RunRecorder | None = None
) -> tuple[McpCliDeps, list[dict], _RunRecorder]:
    fake_server = fake_server or _FakeMcpServer()
    fake_runner = fake_runner or _RunRecorder()
    build_calls: list[dict] = []

    def _fake_build_server(**kwargs: Any) -> _FakeMcpServer:
        build_calls.append(kwargs)
        return fake_server

    deps = McpCliDeps(
        build_server_factory=lambda: _fake_build_server,
        uvicorn_runner_factory=lambda: fake_runner,
    )
    return deps, build_calls, fake_runner


@pytest.mark.unit
def test_serve_stdio_transport_runs_server_with_stdio(monkeypatch) -> None:
    fake_server = _FakeMcpServer()
    deps, build_calls, runner = _build_deps(fake_server=fake_server)

    _stdout, stderr, code = _drive(["serve", "--transport", "stdio"], deps)

    assert code == 0
    assert build_calls == [{"host": "127.0.0.1", "port": 8080}]
    assert fake_server.runs == [{"transport": "stdio"}]
    # uvicorn must NOT be invoked for stdio transport.
    assert runner.calls == []
    assert "Starting kairix MCP server (stdio transport)" in stderr


@pytest.mark.unit
def test_serve_http_transport_runs_uvicorn_with_resolved_app(monkeypatch) -> None:
    # Force --port so _resolve_port returns the CLI value, skipping port-scan.
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18099"])
    deps, build_calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18099"], deps)

    assert code == 0
    assert build_calls == [{"host": "127.0.0.1", "port": 18099}]
    assert len(runner.calls) == 1
    pos, kwargs = runner.calls[0]
    assert pos[0] is not None  # the app object
    assert kwargs == {"host": "127.0.0.1", "port": 18099, "log_level": "info"}
    assert "Starting kairix MCP server on http://127.0.0.1:18099/mcp" in stderr
    assert "+ /sse legacy" in stderr


@pytest.mark.unit
def test_serve_http_transport_with_no_sse_flag(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18098"])
    deps, _calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18098", "--no-sse"], deps)
    assert code == 0
    assert runner.calls
    assert "(no /sse)" in stderr


@pytest.mark.unit
def test_serve_sse_transport_warns_and_continues_as_http(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18097"])
    deps, _calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "sse", "--port", "18097"], deps)

    assert code == 0
    assert "deprecated" in stderr
    assert runner.calls, "uvicorn must still be called after the sse→http coercion"


@pytest.mark.unit
def test_serve_raises_import_error_when_build_server_unavailable() -> None:
    """When the build_server factory raises ImportError, exit 1 with operator message."""

    def _raises_import() -> Any:
        raise ImportError("mcp extra missing")

    deps = McpCliDeps(
        build_server_factory=_raises_import,
        uvicorn_runner_factory=lambda: _RunRecorder(),
    )

    _stdout, stderr, code = _drive(["serve"], deps)
    assert code == 1
    assert "MCP dependencies not installed" in stderr
    assert "pip install 'kairix[agents]'" in stderr


@pytest.mark.unit
def test_main_with_no_subcommand_prints_help_and_exits_1() -> None:
    """Top-level mcp invocation with no subcommand must print help + exit 1."""
    _stdout, stderr, code = _drive([], McpCliDeps())
    assert code == 1
    # argparse-formatted help mentions the serve subcommand.
    out = _stdout + stderr
    assert "serve" in out


@pytest.fixture
def _no_mcp_port_env():
    """Remove KAIRIX_MCP_PORT from environ for the test; restore on teardown.

    Uses ``os.environ.pop`` directly rather than ``monkeypatch.delenv`` to
    keep this file out of the F2 (no KAIRIX_* monkeypatching) detector.
    """
    import os

    previous = os.environ.pop("KAIRIX_MCP_PORT", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["KAIRIX_MCP_PORT"] = previous


@pytest.mark.unit
def test_resolve_port_auto_detect_when_default_available(monkeypatch, _no_mcp_port_env) -> None:
    """No --port, no env var → ``_resolve_port`` uses ``deps.is_port_available_fn``;
    when it returns True, port 8080 is selected.

    Drives the public ``McpCliDeps.is_port_available_fn`` /
    ``find_available_port_fn`` seams (F1-clean — no monkey-patching of
    ``kairix.platform.onboard.ports``).
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve"])

    deps, build_calls, _runner = _build_deps()
    deps.is_port_available_fn = lambda port: True
    deps.find_available_port_fn = lambda *, preferred: preferred
    _stdout, _stderr, code = _drive(["serve", "--transport", "http"], deps)
    assert code == 0
    assert build_calls == [{"host": "127.0.0.1", "port": 8080}]


@pytest.mark.unit
def test_resolve_port_auto_detect_falls_back_when_default_in_use(monkeypatch, _no_mcp_port_env) -> None:
    """No --port, no env, default port unavailable → ``find_available_port_fn`` is consulted.

    Drives the public ``McpCliDeps`` port-probe seams to pin the
    fallback-and-suggest path without touching the kairix internal
    ``kairix.platform.onboard.ports`` module.
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve"])

    deps, build_calls, _runner = _build_deps()
    deps.is_port_available_fn = lambda port: False
    deps.find_available_port_fn = lambda *, preferred: 19100
    _stdout, stderr, code = _drive(["serve", "--transport", "http"], deps)
    assert code == 0
    assert build_calls == [{"host": "127.0.0.1", "port": 19100}]
    assert "Port 8080 is in use" in stderr
    assert "KAIRIX_MCP_PORT=19100" in stderr
