"""Step definitions for brief_cli.feature.

Drives ``kairix.agents.briefing.cli.main`` and captures stdout, stderr,
and exit code. The full briefing pipeline (LLM synthesis + memory
fetch) is out of scope for BDD — these scenarios only cover the
operator-visible CLI surface (argument validation, --help, error paths).
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest
from pytest_bdd import parsers, then, when

_VALID_AGENTS = ("builder", "shape", "growth", "consultant")


@dataclass
class _BriefCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def brief_cli_ctx() -> _BriefCliCtx:
    return _BriefCliCtx()


def _run_brief(brief_cli_ctx: _BriefCliCtx, args: list[str]) -> None:
    from kairix.agents.briefing.cli import main as brief_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            brief_main(args)
        brief_cli_ctx.exit_code = 0
    except SystemExit as e:
        brief_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    brief_cli_ctx.stdout = out.getvalue()
    brief_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the brief CLI with `{argv}`"))
def _run_brief_argv(brief_cli_ctx: _BriefCliCtx, argv: str) -> None:
    _run_brief(brief_cli_ctx, shlex.split(argv))


@when("the operator runs the brief CLI with no arguments")
def _run_brief_no_args(brief_cli_ctx: _BriefCliCtx) -> None:
    _run_brief(brief_cli_ctx, [])


@then(parsers.parse("the brief CLI exits with status {code:d}"))
def _assert_brief_exit(brief_cli_ctx: _BriefCliCtx, code: int) -> None:
    assert brief_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {brief_cli_ctx.exit_code}; "
        f"stdout={brief_cli_ctx.stdout[:200]!r} stderr={brief_cli_ctx.stderr[:200]!r}"
    )


@then("stderr names the invalid agent")
def _assert_stderr_names_invalid(brief_cli_ctx: _BriefCliCtx) -> None:
    assert "not-an-agent" in brief_cli_ctx.stderr, f"stderr did not name the bad agent: {brief_cli_ctx.stderr!r}"


@then("stderr lists the valid agent names")
def _assert_stderr_lists_valid_agents(brief_cli_ctx: _BriefCliCtx) -> None:
    err = brief_cli_ctx.stderr
    hits = sum(1 for a in _VALID_AGENTS if a in err)
    assert hits >= 3, (
        f"stderr lists too few valid agents ({hits}/{len(_VALID_AGENTS)}); "
        f"operators won't know what to type. stderr={err!r}"
    )


@then("the output names every valid agent")
def _assert_help_lists_agents(brief_cli_ctx: _BriefCliCtx) -> None:
    out = brief_cli_ctx.stdout + brief_cli_ctx.stderr
    for agent in _VALID_AGENTS:
        assert agent in out, f"agent {agent!r} missing from --help output:\n{out}"
