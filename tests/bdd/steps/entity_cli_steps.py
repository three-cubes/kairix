"""Step definitions for entity_cli.feature.

Drives ``kairix.knowledge.entities.cli.main`` with an explicit ``db_path``
pointing at a non-existent file (so the missing-index branch is exercised
deterministically) instead of monkeypatching ``KAIRIX_DB_PATH``. The
suggest/validate success paths need spaCy NLP and live Wikidata; covered
at integration.
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
    db_path: Path
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def entity_cli_ctx(tmp_path: Path) -> _EntityCliCtx:
    # tmp_path is empty: no index file at this path → exercises the
    # 'index not found' branch of cmd_seed without env-var fiddling.
    return _EntityCliCtx(db_path=tmp_path / "absent.sqlite")


def _run_entity(entity_cli_ctx: _EntityCliCtx, args: list[str]) -> None:
    from kairix.knowledge.entities.cli import main as entity_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = entity_main(args, db_path=entity_cli_ctx.db_path)
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
