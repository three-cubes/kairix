"""Step definitions for kairix_cli_top_level.feature.

Drives ``kairix.cli.main`` directly with sys.argv mutation (the entry point
reads sys.argv directly — that's the contract). Captures stdout, stderr,
and exit code.
"""

from __future__ import annotations

import io
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest
from pytest_bdd import parsers, then, when

from kairix.cli import COMMANDS as _KAIRIX_SUBCOMMANDS
from kairix.cli import main as kairix_main


@dataclass
class _TopLevelCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def top_level_cli_ctx(monkeypatch: pytest.MonkeyPatch) -> _TopLevelCliCtx:
    # Each scenario gets a clean argv slate. monkeypatch.setattr on sys.argv
    # is a pytest-builtin convenience for the sys-module — the production
    # CLI reads sys.argv as its documented input, so this is the right
    # pyramid level to drive it from.
    monkeypatch.setattr(sys, "argv", ["kairix"])
    return _TopLevelCliCtx()


@when(parsers.parse("the operator invokes the kairix entry point with `{argv}`"))
def _run_top_level(top_level_cli_ctx: _TopLevelCliCtx, argv: str, monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["kairix", *shlex.split(argv)]
    monkeypatch.setattr(sys, "argv", args)
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            kairix_main()
        top_level_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
        top_level_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    top_level_cli_ctx.stdout = out.getvalue()
    top_level_cli_ctx.stderr = err.getvalue()


@then(parsers.parse("the kairix CLI exits with status {code:d}"))
def _assert_top_level_exit(top_level_cli_ctx: _TopLevelCliCtx, code: int) -> None:
    assert top_level_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {top_level_cli_ctx.exit_code}; "
        f"stdout={top_level_cli_ctx.stdout[:200]!r} stderr={top_level_cli_ctx.stderr[:200]!r}"
    )


# Aliases that the help intentionally collapses into their canonical name.
# Operators discover them from the canonical command's help text or release
# notes, not from the top-level docstring.
_KAIRIX_HIDDEN_ALIASES = {"vault"}  # alias for "store"


@then("the output names every documented subcommand")
def _assert_help_lists_subcommands(top_level_cli_ctx: _TopLevelCliCtx) -> None:
    out = top_level_cli_ctx.stdout + top_level_cli_ctx.stderr
    for cmd_name in _KAIRIX_SUBCOMMANDS:
        if cmd_name in _KAIRIX_HIDDEN_ALIASES:
            continue
        assert cmd_name in out, f"subcommand {cmd_name!r} missing from --help output:\n{out}"


@then(parsers.parse('the output starts with "{prefix}"'))
def _assert_starts_with(top_level_cli_ctx: _TopLevelCliCtx, prefix: str) -> None:
    assert top_level_cli_ctx.stdout.startswith(prefix), (
        f"stdout did not start with {prefix!r}; got {top_level_cli_ctx.stdout[:100]!r}"
    )


@then("stderr names the unknown command")
def _assert_stderr_names_unknown(top_level_cli_ctx: _TopLevelCliCtx) -> None:
    assert "Unknown command" in top_level_cli_ctx.stderr, (
        f"stderr missing 'Unknown command': {top_level_cli_ctx.stderr!r}"
    )
    assert "not-a-command" in top_level_cli_ctx.stderr, (
        f"stderr did not name the bad command: {top_level_cli_ctx.stderr!r}"
    )


@then("stderr lists the documented subcommands")
def _assert_stderr_lists_subcommands(top_level_cli_ctx: _TopLevelCliCtx) -> None:
    err = top_level_cli_ctx.stderr
    # At least 5 of the documented subcommands must appear in the error help —
    # being lenient here on the exact count, strict on the operator-actionable
    # property (the help text is present, not absent).
    hits = sum(1 for cmd_name in _KAIRIX_SUBCOMMANDS if cmd_name in err)
    assert hits >= 5, (
        f"stderr lists too few subcommands ({hits}); operators won't know what's available. stderr={err!r}"
    )
