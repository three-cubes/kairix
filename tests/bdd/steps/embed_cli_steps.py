"""Step definitions for embed_cli.feature.

Drives ``kairix.core.embed.cli.main(argv)`` and captures stdout/stderr/exit.
The actual embed pipeline (Azure API, lockfile, recall gate) is out of
scope for BDD — only the CLI argparse surface is pinned here. Pipeline
mechanics are exercised by integration tests.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest
from pytest_bdd import parsers, then, when

_SUBCOMMANDS = ("embed", "recall-check", "status")
_EMBED_FLAGS = ("--force", "--limit", "--batch-size", "--skip-recall-check", "--skip-summarise")


@dataclass
class _EmbedCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def embed_cli_ctx() -> _EmbedCliCtx:
    return _EmbedCliCtx()


def _run_embed(embed_cli_ctx: _EmbedCliCtx, args: list[str]) -> None:
    from kairix.core.embed.cli import main as embed_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            embed_main(args)
        embed_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
        embed_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    embed_cli_ctx.stdout = out.getvalue()
    embed_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the embed CLI with `{argv}`"))
def _run_embed_argv(embed_cli_ctx: _EmbedCliCtx, argv: str) -> None:
    _run_embed(embed_cli_ctx, shlex.split(argv))


@then(parsers.parse("the embed CLI exits with status {code:d}"))
def _assert_embed_exit(embed_cli_ctx: _EmbedCliCtx, code: int) -> None:
    assert embed_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {embed_cli_ctx.exit_code}; "
        f"stdout={embed_cli_ctx.stdout[:200]!r} stderr={embed_cli_ctx.stderr[:200]!r}"
    )


@then("the embed help output names every subcommand")
def _assert_help_lists_subcommands(embed_cli_ctx: _EmbedCliCtx) -> None:
    out = embed_cli_ctx.stdout + embed_cli_ctx.stderr
    for sub in _SUBCOMMANDS:
        assert sub in out, f"subcommand {sub!r} missing from --help output:\n{out}"


@then("the embed help output names every embed flag")
def _assert_help_lists_flags(embed_cli_ctx: _EmbedCliCtx) -> None:
    out = embed_cli_ctx.stdout + embed_cli_ctx.stderr
    for flag in _EMBED_FLAGS:
        assert flag in out, f"flag {flag!r} missing from `embed --help` output:\n{out}"
