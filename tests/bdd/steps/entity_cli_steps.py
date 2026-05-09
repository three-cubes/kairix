"""Step definitions for entity_cli.feature.

Drives ``kairix.knowledge.entities.cli.main`` and captures stdout, stderr,
and exit code. The suggest/validate success paths need spaCy NLP and live
Wikidata; covered at integration. seed --dry-run with no index is a
pure CLI-surface contract — no external dependencies.
"""

from __future__ import annotations

import io
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_bdd import parsers, then, when

_SUBCOMMANDS = ("suggest", "validate", "seed")


@dataclass
class _EntityCliCtx:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def entity_cli_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _EntityCliCtx:
    # Point KAIRIX_DATA_DIR at empty tmp so 'seed' finds no index.
    monkeypatch.setenv("KAIRIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(exist_ok=True)
    for ev in ("KAIRIX_NEO4J_URI", "KAIRIX_NEO4J_USER", "KAIRIX_NEO4J_PASSWORD"):
        monkeypatch.delenv(ev, raising=False)
    return _EntityCliCtx()


def _run_entity(entity_cli_ctx: _EntityCliCtx, args: list[str]) -> None:
    from kairix.knowledge.entities.cli import main as entity_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = entity_main(args)
        entity_cli_ctx.exit_code = rc if rc is not None else 0
    except SystemExit as e:
        entity_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    entity_cli_ctx.stdout = out.getvalue()
    entity_cli_ctx.stderr = err.getvalue()


@when(parsers.parse("the operator runs the entity CLI with `{argv}`"))
def _run_entity_argv(entity_cli_ctx: _EntityCliCtx, argv: str) -> None:
    _run_entity(entity_cli_ctx, shlex.split(argv))


@when("the operator runs the entity CLI with no arguments")
def _run_entity_no_args(entity_cli_ctx: _EntityCliCtx) -> None:
    _run_entity(entity_cli_ctx, [])


@then(parsers.parse("the entity CLI exits with status {code:d}"))
def _assert_entity_exit(entity_cli_ctx: _EntityCliCtx, code: int) -> None:
    assert entity_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {entity_cli_ctx.exit_code}; "
        f"stdout={entity_cli_ctx.stdout[:200]!r} stderr={entity_cli_ctx.stderr[:200]!r}"
    )


@then("the output names every entity subcommand")
def _assert_lists_subcommands(entity_cli_ctx: _EntityCliCtx) -> None:
    out = entity_cli_ctx.stdout + entity_cli_ctx.stderr
    for sub in _SUBCOMMANDS:
        assert sub in out, f"subcommand {sub!r} missing from output:\n{out}"


@then("stderr names the missing index")
def _assert_stderr_names_missing_index(entity_cli_ctx: _EntityCliCtx) -> None:
    err = entity_cli_ctx.stderr
    assert "index not found" in err.lower() or "not found" in err.lower(), (
        f"stderr did not name the missing index: {err!r}"
    )
