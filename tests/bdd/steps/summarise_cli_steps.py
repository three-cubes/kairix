"""Step definitions for summarise_cli.feature.

Drives ``kairix.knowledge.summaries.cli.main`` with explicit ``document_root``
and ``db_path`` kw-arguments — no env-var monkeypatch, no private cache
clears. Captures stdout + exit code so the assertions pin operator-visible
CLI behaviour.
"""

from __future__ import annotations

import io
import shlex
import sqlite3
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when


@dataclass
class _SummariseCtx:
    document_root: Path
    db_path: Path
    exit_code: int = 0
    stdout: str = ""


@pytest.fixture
def summarise_ctx(tmp_path: Path) -> _SummariseCtx:
    document_root = tmp_path / "vault"
    document_root.mkdir()
    db_path = tmp_path / "data" / "summaries.db"
    return _SummariseCtx(document_root=document_root, db_path=db_path)


def _ensure_db(ctx: _SummariseCtx) -> Path:
    """Initialise an empty summaries DB at ctx.db_path."""
    from kairix.knowledge.summaries.staleness import init_summaries_db

    ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        init_summaries_db(conn)
    finally:
        conn.close()
    return ctx.db_path


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("an empty summaries database")
def _given_empty_db(summarise_ctx: _SummariseCtx) -> None:
    _ensure_db(summarise_ctx)


@given(parsers.parse("a summaries database populated with:"))
def _given_populated_db(summarise_ctx: _SummariseCtx, datatable: list[list[str]]) -> None:
    _ensure_db(summarise_ctx)
    headers = datatable[0]
    rows = datatable[1:]
    conn = sqlite3.connect(str(summarise_ctx.db_path))
    try:
        for row in rows:
            payload = dict(zip(headers, row, strict=True))
            conn.execute(
                "INSERT OR REPLACE INTO summaries (path, l0, l1) VALUES (?, ?, ?)",
                (
                    payload["path"],
                    payload.get("l0") or None,
                    payload.get("l1") or None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs `kairix summarise {argv}`"))
def _run_summarise_cli(summarise_ctx: _SummariseCtx, argv: str) -> None:
    from kairix.knowledge.summaries.cli import main as summarise_main

    args = shlex.split(argv)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            summarise_main(
                args,
                document_root=summarise_ctx.document_root,
                db_path=summarise_ctx.db_path,
            )
        summarise_ctx.exit_code = 0
    except SystemExit as e:
        summarise_ctx.exit_code = int(e.code) if e.code is not None else 0
    summarise_ctx.stdout = buf.getvalue()


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("the summarise CLI exits with status {code:d}"))
def _assert_summarise_exit(summarise_ctx: _SummariseCtx, code: int) -> None:
    assert summarise_ctx.exit_code == code, (
        f"expected exit {code}, got {summarise_ctx.exit_code}; stdout={summarise_ctx.stdout[:300]!r}"
    )


@then(parsers.parse("the output reports {n:d} documents with L0 summaries"))
def _assert_l0_count(summarise_ctx: _SummariseCtx, n: int) -> None:
    line = next((line for line in summarise_ctx.stdout.splitlines() if line.startswith("With L0:")), None)
    assert line is not None, f"missing 'With L0:' line in output: {summarise_ctx.stdout!r}"
    # Format: "With L0:        2 / 2 stored"
    count = int(line.split(":", 1)[1].strip().split()[0])
    assert count == n, f"expected {n} docs with L0, got {count}; line={line!r}"


@then(parsers.parse("the output reports {n:d} document with an L1 overview"))
def _assert_l1_count(summarise_ctx: _SummariseCtx, n: int) -> None:
    line = next((line for line in summarise_ctx.stdout.splitlines() if line.startswith("With L1:")), None)
    assert line is not None, f"missing 'With L1:' line in output: {summarise_ctx.stdout!r}"
    count = int(line.split(":", 1)[1].strip().split()[0])
    assert count == n, f"expected {n} docs with L1, got {count}; line={line!r}"
