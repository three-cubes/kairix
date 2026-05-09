"""Step definitions for timeline_cli.feature.

Drives ``kairix.core.temporal.cli.main(argv)`` and captures
stdout/stderr/exit. The success path relies on a populated temporal
index; covered at integration. BDD pins only the surface contracts.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest
from pytest_bdd import parsers, then, when

_DOCUMENTED_FLAGS = ("--since", "--until", "--limit", "--type")


@dataclass
class _TimelineCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def timeline_cli_ctx() -> _TimelineCliCtx:
    return _TimelineCliCtx()


def _run_timeline(timeline_cli_ctx: _TimelineCliCtx, args: list[str]) -> None:
    from kairix.core.temporal.cli import main as timeline_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            timeline_main(args)
        timeline_cli_ctx.exit_code = 0
    except SystemExit as e:
        timeline_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    timeline_cli_ctx.stdout = out.getvalue()
    timeline_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the timeline CLI with `{argv}`"))
def _run_timeline_argv(timeline_cli_ctx: _TimelineCliCtx, argv: str) -> None:
    _run_timeline(timeline_cli_ctx, shlex.split(argv))


@when("the operator runs the timeline CLI with no arguments")
def _run_timeline_no_args(timeline_cli_ctx: _TimelineCliCtx) -> None:
    _run_timeline(timeline_cli_ctx, [])


@then(parsers.parse("the timeline CLI exits with status {code:d}"))
def _assert_timeline_exit(timeline_cli_ctx: _TimelineCliCtx, code: int) -> None:
    assert timeline_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {timeline_cli_ctx.exit_code}; "
        f"stdout={timeline_cli_ctx.stdout[:200]!r} stderr={timeline_cli_ctx.stderr[:200]!r}"
    )


@then("the timeline help output names every documented flag")
def _assert_help_names_flags(timeline_cli_ctx: _TimelineCliCtx) -> None:
    out = timeline_cli_ctx.stdout + timeline_cli_ctx.stderr
    for flag in _DOCUMENTED_FLAGS:
        assert flag in out, f"flag {flag!r} missing from --help output:\n{out}"


@then("stderr names the invalid since date")
def _assert_stderr_names_invalid_since(timeline_cli_ctx: _TimelineCliCtx) -> None:
    err = timeline_cli_ctx.stderr
    assert "not-a-date" in err, f"stderr did not name the bad --since value: {err!r}"
    assert "--since" in err or "since" in err, f"stderr did not flag --since context: {err!r}"


@then("stderr names the bad type choice")
def _assert_stderr_names_bad_type(timeline_cli_ctx: _TimelineCliCtx) -> None:
    assert "bogus" in timeline_cli_ctx.stderr, f"stderr did not name the bad --type value: {timeline_cli_ctx.stderr!r}"
