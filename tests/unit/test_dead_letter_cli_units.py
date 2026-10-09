"""Unit-layer coverage lift for :mod:`kairix.dead_letter_cli`.

The F30 outcome tests
(``tests/integration/test_outcome_cli_dead_letter_status.py``) drive
the CLI via subprocess, which is the right contract but pytest-cov
doesn't see the subprocess body. This file exercises every branch of
``kairix.dead_letter_cli.main`` (and ``status``) in-process through
the public DI seams (``db_provider`` + ``db_path`` kwargs) so F7
(per-file ≥90% unit coverage) is satisfied.

F1-clean / F2-clean: no @patch, no env-var manipulation; every test
uses the public ``db_provider`` seam.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from kairix.core.db.schema import create_schema
from kairix.dead_letter_cli import default_db_provider
from kairix.dead_letter_cli import drain as dl_drain
from kairix.dead_letter_cli import main as dl_main
from kairix.dead_letter_cli import status as dl_status

pytestmark = pytest.mark.unit

# A permanently-unprocessable last_error string + an unsupported MIME so
# the drain core's narrow eligibility (corrupt_zip OR known-unsupported
# MIME) fires deterministically.
_ERR_DRAINABLE = "some other connector error"
_MIME_UNSUPPORTED = "application/msword"
_ORPHAN_SOURCE = "removed-sharepoint"


def _seed_db(tmp_path: Path, *, rows: bool = False) -> Path:
    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    if rows:
        db.execute(
            "INSERT INTO connector_deadletter "
            "(source_name, item_id, failure_count, last_error, last_attempt) "
            "VALUES (?, ?, ?, ?, ?)",
            ("connector-alpha", "i1", 3, "MissingDependencyException", "2026-05-26T05:58:00Z"),
        )
    db.commit()
    db.close()
    return db_path


def _seed_drainable(tmp_path: Path, *, source_name: str = _ORPHAN_SOURCE) -> Path:
    """A DB with one permanently-unprocessable (drainable) row for ``source_name``."""
    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    db.execute(
        "INSERT INTO connector_deadletter "
        "(source_name, item_id, failure_count, last_error, last_attempt) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_name, "poison-1", 3, _ERR_DRAINABLE, "2026-05-26T05:58:00Z"),
    )
    db.execute(
        "INSERT INTO bronze_records "
        "(source_name, item_id, raw_path, mime, fetched_at, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_name, "poison-1", "/bronze/poison-1", _MIME_UNSUPPORTED, "2026-05-26T05:50:00Z", "h-poison"),
    )
    db.commit()
    db.close()
    return db_path


def _deadletter_rows(db_path: Path, source_name: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM connector_deadletter WHERE source_name = ?",
                (source_name,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _provider(db_path: Path) -> Any:
    """Build a fixed in-process provider that always opens ``db_path``."""

    def _open(_explicit: Path | None) -> sqlite3.Connection:
        return sqlite3.connect(str(db_path))

    return _open


def test_default_db_provider_with_explicit_path_opens_file(tmp_path: Path) -> None:
    """The explicit-path branch opens the supplied file directly."""
    db_path = _seed_db(tmp_path, rows=False)
    conn = default_db_provider(db_path)
    try:
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert any(n[0] == "connector_deadletter" for n in names)
    finally:
        conn.close()


def test_status_with_provider_writes_human_output(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, rows=True)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = dl_status(out=out_buf, err=err_buf, db_provider=_provider(db_path))
    assert rc == 0
    assert "connector-alpha" in out_buf.getvalue()
    assert err_buf.getvalue() == ""


def test_status_with_provider_writes_json_output(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, rows=True)
    out_buf = io.StringIO()
    rc = dl_status(as_json=True, out=out_buf, db_provider=_provider(db_path))
    assert rc == 0
    parsed = json.loads(out_buf.getvalue())
    assert parsed["total"] == 1
    assert parsed["per_source"][0]["source_name"] == "connector-alpha"


def test_status_source_filter_returns_only_one(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, rows=True)
    out_buf = io.StringIO()
    rc = dl_status(
        source_name="connector-alpha",
        as_json=True,
        out=out_buf,
        db_provider=_provider(db_path),
    )
    assert rc == 0
    parsed = json.loads(out_buf.getvalue())
    assert len(parsed["per_source"]) == 1


def test_status_connect_failure_writes_affordance(tmp_path: Path) -> None:
    """Provider raising sqlite3.Error → exit 1 + F21-shaped affordance."""

    def _bad_provider(_db_path: Path | None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated connect failure")

    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = dl_status(out=out_buf, err=err_buf, db_provider=_bad_provider)
    assert rc == 1
    err = err_buf.getvalue()
    assert "fix:" in err
    assert "next:" in err
    assert "run:" in err


def test_status_select_failure_writes_affordance(tmp_path: Path) -> None:
    """SELECT against a db with no schema → exit 1 + affordance line."""
    bad_db = tmp_path / "empty.sqlite"
    sqlite3.connect(str(bad_db)).close()  # empty file, no schema
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = dl_status(out=out_buf, err=err_buf, db_provider=_provider(bad_db))
    assert rc == 1
    err = err_buf.getvalue()
    assert "connector_deadletter" in err
    assert "fix:" in err


def test_main_default_subcommand_is_status(tmp_path: Path) -> None:
    """``kairix dead-letter`` with no subcommand routes to status."""
    db_path = _seed_db(tmp_path, rows=False)
    rc = dl_main([], db_provider=_provider(db_path))
    assert rc == 0


def test_main_explicit_status_subcommand(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, rows=False)
    rc = dl_main(["status"], db_provider=_provider(db_path))
    assert rc == 0


def test_main_status_with_json_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = _seed_db(tmp_path, rows=True)
    rc = dl_main(["status", "--json"], db_provider=_provider(db_path))
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["total"] == 1


def test_main_status_with_source_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = _seed_db(tmp_path, rows=True)
    rc = dl_main(
        ["status", "--source-name", "connector-alpha"],
        db_provider=_provider(db_path),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "connector-alpha" in captured.out


def test_main_status_with_db_path_arg(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The ``--db-path`` arg is the subprocess seam; verify the binding path."""
    db_path = _seed_db(tmp_path, rows=False)
    rc = dl_main(["status", "--db-path", str(db_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no dead-letter state" in captured.out


def test_main_in_process_db_path_kwarg_wins_over_arg(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The in-process ``db_path=`` kwarg trumps the ``--db-path`` flag.

    Sabotage: invert the precedence in :func:`_resolve_db_path_arg` and
    this test fails because the ``--db-path`` arg would win against the
    in-process tmp DB.
    """
    seeded_db = _seed_db(tmp_path, rows=True)
    rc = dl_main(
        ["status", "--db-path", str(tmp_path / "ignored.sqlite")],
        db_path=seeded_db,
        db_provider=_provider(seeded_db),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "connector-alpha" in captured.out


# ---------------------------------------------------------------------------
# drain verb (PR-5) — single source, all sources, dry-run, orphaned source
# ---------------------------------------------------------------------------


def test_drain_named_source_clears_orphaned_backlog(tmp_path: Path) -> None:
    """``drain SOURCE`` clears an ORPHANED source's drainable row + reports it.

    The source has no active connector — drain works on the source_name
    string alone. Sabotage: skip the ``source_name`` branch in ``_run_drain``
    → nothing drains and the row-count / 'drained 1' assertions fail.
    """
    db_path = _seed_drainable(tmp_path)
    out_buf = io.StringIO()
    rc = dl_drain(source_name=_ORPHAN_SOURCE, out=out_buf, db_provider=_provider(db_path))
    assert rc == 0
    assert _deadletter_rows(db_path, _ORPHAN_SOURCE) == 0
    out = out_buf.getvalue()
    assert _ORPHAN_SOURCE in out
    assert "drained 1" in out
    assert "total:" in out


def test_drain_all_sources_clears_every_distinct_source(tmp_path: Path) -> None:
    """``drain`` with NO source drains every distinct source_name."""
    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    for src in ("removed-a", "removed-b"):
        db.execute(
            "INSERT INTO connector_deadletter "
            "(source_name, item_id, failure_count, last_error, last_attempt) "
            "VALUES (?, ?, ?, ?, ?)",
            (src, "p", 3, _ERR_DRAINABLE, "2026-05-26T05:58:00Z"),
        )
        db.execute(
            "INSERT INTO bronze_records "
            "(source_name, item_id, raw_path, mime, fetched_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src, "p", "/b/p", _MIME_UNSUPPORTED, "2026-05-26T05:50:00Z", f"h-{src}"),
        )
    db.commit()
    db.close()

    out_buf = io.StringIO()
    rc = dl_drain(out=out_buf, db_provider=_provider(db_path))
    assert rc == 0
    assert _deadletter_rows(db_path, "removed-a") == 0
    assert _deadletter_rows(db_path, "removed-b") == 0
    out = out_buf.getvalue()
    assert "removed-a" in out
    assert "removed-b" in out
    assert "across 2 source(s)" in out


def test_drain_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """``--dry-run`` reports what WOULD drain but leaves the row in place.

    Sabotage: ignore ``dry_run`` in ``_run_drain`` → the row clears and the
    'row still there' / 'dry-run' header assertions fail.
    """
    db_path = _seed_drainable(tmp_path)
    out_buf = io.StringIO()
    rc = dl_drain(source_name=_ORPHAN_SOURCE, dry_run=True, out=out_buf, db_provider=_provider(db_path))
    assert rc == 0
    # row survives — nothing mutated.
    assert _deadletter_rows(db_path, _ORPHAN_SOURCE) == 1
    out = out_buf.getvalue()
    assert "dry-run" in out
    assert "would drain 1" in out


def test_drain_empty_table_reports_friendly_line(tmp_path: Path) -> None:
    """A clean table drains nothing and prints the friendly empty line."""
    db_path = _seed_db(tmp_path, rows=False)
    out_buf = io.StringIO()
    rc = dl_drain(out=out_buf, db_provider=_provider(db_path))
    assert rc == 0
    assert "no drainable dead-letter state" in out_buf.getvalue()


def test_drain_connect_failure_writes_affordance(tmp_path: Path) -> None:
    """A provider raising sqlite3.Error → exit 1 + an F21-shaped affordance."""

    def _bad_provider(_db_path: Path | None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated connect failure")

    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = dl_drain(out=out_buf, err=err_buf, db_provider=_bad_provider)
    assert rc == 1
    err = err_buf.getvalue()
    assert "fix:" in err
    assert "next:" in err
    assert "run:" in err


def test_main_routes_drain_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``kairix dead-letter drain SOURCE`` routes through ``main`` to ``drain``.

    Sabotage: remove the ``elif cmd == 'drain'`` route in ``main`` → argv
    parses but no drain runs; the row survives and this fails.
    """
    db_path = _seed_drainable(tmp_path)
    rc = dl_main(["drain", _ORPHAN_SOURCE], db_provider=_provider(db_path))
    assert rc == 0
    assert _deadletter_rows(db_path, _ORPHAN_SOURCE) == 0
    assert "drained 1" in capsys.readouterr().out


def test_main_routes_drain_dry_run_and_max_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``main`` threads ``--dry-run`` + ``--max`` through to ``drain``."""
    db_path = _seed_drainable(tmp_path)
    rc = dl_main(["drain", _ORPHAN_SOURCE, "--dry-run", "--max", "10"], db_provider=_provider(db_path))
    assert rc == 0
    assert _deadletter_rows(db_path, _ORPHAN_SOURCE) == 1  # dry-run: untouched
    assert "dry-run" in capsys.readouterr().out


def test_main_drain_default_out_streams_to_stdio(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``drain`` with no ``out`` defaults to ``sys.stdout``."""
    db_path = _seed_db(tmp_path, rows=False)
    rc = dl_drain(db_provider=_provider(db_path))
    assert rc == 0
    assert "no drainable dead-letter state" in capsys.readouterr().out


def test_main_runs_via_module_main_guard(tmp_path: Path) -> None:
    """The ``if __name__ == '__main__'`` guard executes :func:`main`.

    Runs ``python -m kairix.dead_letter_cli`` in a subprocess — a fresh
    interpreter, so no kairix module body re-executes in the test process
    (F1). Sabotage: drop ``main(...)`` from the guard → no status output and
    the assert on stdout fails.
    """
    import subprocess

    db_path = _seed_db(tmp_path, rows=False)
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.dead_letter_cli", "status", "--db-path", str(db_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "no dead-letter state" in proc.stdout


def test_status_default_out_err_streams_route_to_stdio(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``out`` / ``err`` are None they default to ``sys.stdout`` / ``sys.stderr``."""
    db_path = _seed_db(tmp_path, rows=False)
    rc = dl_status(db_provider=_provider(db_path))
    assert rc == 0
    captured = capsys.readouterr()
    assert "no dead-letter state" in captured.out
