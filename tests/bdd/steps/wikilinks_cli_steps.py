"""Step definitions for wikilinks_cli.feature.

Drives ``kairix.knowledge.wikilinks.cli.main`` and captures stdout, stderr,
and exit code. The inject + audit subcommands need a populated entity
graph (Neo4j or bootstrap index) — out of scope for BDD; covered by
integration tests. These scenarios cover the surface contract only.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_bdd import parsers, then, when

_SUBCOMMANDS = ("inject", "audit", "status")


@dataclass
class _WikilinksCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def wikilinks_cli_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _WikilinksCliCtx:
    # Point KAIRIX_DATA_DIR at tmp so 'status' doesn't write to ~/.cache;
    # ensure no Neo4j env vars are set so resolver falls back to empty.
    monkeypatch.setenv("KAIRIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(exist_ok=True)
    for ev in ("KAIRIX_NEO4J_URI", "KAIRIX_NEO4J_USER", "KAIRIX_NEO4J_PASSWORD"):
        monkeypatch.delenv(ev, raising=False)
    return _WikilinksCliCtx()


def _run_wikilinks(wikilinks_cli_ctx: _WikilinksCliCtx, args: list[str]) -> None:
    from kairix.knowledge.wikilinks.cli import main as wikilinks_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            wikilinks_main(args)
        wikilinks_cli_ctx.exit_code = 0
    except SystemExit as e:
        wikilinks_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    wikilinks_cli_ctx.stdout = out.getvalue()
    wikilinks_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the wikilinks CLI with `{argv}`"))
def _run_wikilinks_argv(wikilinks_cli_ctx: _WikilinksCliCtx, argv: str) -> None:
    _run_wikilinks(wikilinks_cli_ctx, shlex.split(argv))


@when("the operator runs the wikilinks CLI with no arguments")
def _run_wikilinks_no_args(wikilinks_cli_ctx: _WikilinksCliCtx) -> None:
    _run_wikilinks(wikilinks_cli_ctx, [])


@then(parsers.parse("the wikilinks CLI exits with status {code:d}"))
def _assert_wikilinks_exit(wikilinks_cli_ctx: _WikilinksCliCtx, code: int) -> None:
    assert wikilinks_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {wikilinks_cli_ctx.exit_code}; "
        f"stdout={wikilinks_cli_ctx.stdout[:200]!r} stderr={wikilinks_cli_ctx.stderr[:200]!r}"
    )


@then("the output names every wikilinks subcommand")
def _assert_lists_subcommands(wikilinks_cli_ctx: _WikilinksCliCtx) -> None:
    out = wikilinks_cli_ctx.stdout + wikilinks_cli_ctx.stderr
    for sub in _SUBCOMMANDS:
        assert sub in out, f"subcommand {sub!r} missing from output:\n{out}"


@then("stderr names the unknown wikilinks subcommand")
def _assert_stderr_names_unknown(wikilinks_cli_ctx: _WikilinksCliCtx) -> None:
    assert "not-a-subcommand" in wikilinks_cli_ctx.stderr, (
        f"stderr did not name the bad subcommand: {wikilinks_cli_ctx.stderr!r}"
    )


@then(parsers.parse('the output reports "{label}"'))
def _assert_reports_label(wikilinks_cli_ctx: _WikilinksCliCtx, label: str) -> None:
    out = wikilinks_cli_ctx.stdout
    assert label in out, f"expected {label!r} in output:\n{out}"
