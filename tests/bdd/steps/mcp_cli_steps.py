"""Step definitions for mcp_cli.feature.

Drives ``kairix.agents.mcp.cli.main`` and captures stdout / stderr / exit
code. ``serve`` actually starts a server (blocks), so the BDD covers only
the surface contracts: --help, no-subcommand, argparse rejection of bad
transport. The serve runtime path is exercised by integration tests.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest
from pytest_bdd import parsers, then, when

_TRANSPORTS = ("stdio", "http", "sse")


@dataclass
class _McpCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def mcp_cli_ctx() -> _McpCliCtx:
    return _McpCliCtx()


def _run_mcp(mcp_cli_ctx: _McpCliCtx, args: list[str]) -> None:
    from kairix.agents.mcp.cli import main as mcp_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            mcp_main(args)
        mcp_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
        mcp_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    mcp_cli_ctx.stdout = out.getvalue()
    mcp_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the mcp CLI with `{argv}`"))
def _run_mcp_argv(mcp_cli_ctx: _McpCliCtx, argv: str) -> None:
    _run_mcp(mcp_cli_ctx, shlex.split(argv))


@when("the operator runs the mcp CLI with no arguments")
def _run_mcp_no_args(mcp_cli_ctx: _McpCliCtx) -> None:
    _run_mcp(mcp_cli_ctx, [])


@then(parsers.parse("the mcp CLI exits with status {code:d}"))
def _assert_mcp_exit(mcp_cli_ctx: _McpCliCtx, code: int) -> None:
    assert mcp_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {mcp_cli_ctx.exit_code}; "
        f"stdout={mcp_cli_ctx.stdout[:200]!r} stderr={mcp_cli_ctx.stderr[:200]!r}"
    )


@then("the help output names the serve subcommand")
def _assert_help_names_serve(mcp_cli_ctx: _McpCliCtx) -> None:
    out = mcp_cli_ctx.stdout + mcp_cli_ctx.stderr
    assert "serve" in out, f"missing 'serve' in output:\n{out}"


@then("the help output names every transport choice")
def _assert_help_names_transports(mcp_cli_ctx: _McpCliCtx) -> None:
    out = mcp_cli_ctx.stdout + mcp_cli_ctx.stderr
    for transport in _TRANSPORTS:
        assert transport in out, f"transport {transport!r} missing from --help output:\n{out}"


@then("the output names the serve subcommand")
def _assert_output_names_serve(mcp_cli_ctx: _McpCliCtx) -> None:
    out = mcp_cli_ctx.stdout + mcp_cli_ctx.stderr
    assert "serve" in out, f"missing 'serve' in output:\n{out}"


@then("stderr names the bad transport")
def _assert_stderr_names_bad_transport(mcp_cli_ctx: _McpCliCtx) -> None:
    assert "not-a-transport" in mcp_cli_ctx.stderr, f"stderr did not name the bad transport: {mcp_cli_ctx.stderr!r}"
