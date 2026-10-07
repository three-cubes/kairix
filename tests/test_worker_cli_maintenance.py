"""Outcome tests for ``kairix worker maintenance`` + ``kairix features status --maintenance``.

KFEAT-021 Phase 1 surfaces two new operator-facing affordances:

  1. ``kairix worker maintenance`` — one-shot
     :class:`MaintenanceScheduler` tick. Always runs (the flag gates
     the worker-loop tick, not this on-demand verb).
  2. ``kairix features status --maintenance`` — reports last-tick time,
     orphan counts, next-scheduled-tick time.

Both are exercised here:

  * In-process via the ``main(argv=...)`` entry points (covers the
    happy-path + arg-routing logic).
  * Via ``subprocess.run([sys.executable, "-m", "kairix.cli", "worker",
    "maintenance", ...])`` to satisfy the F30-style outcome contract
    against the composed production path.

Tests assert on stdout/stderr content per the F30 outcome contract —
not on returncode alone, not on internal fake call-counts.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kairix.core.db.schema import create_schema
from kairix.worker_cli import main as worker_main

pytestmark = pytest.mark.unit


def _bootstrap_with_orphan(tmp_path: Path, *, n_orphans: int = 1) -> Path:
    """Create the production schema; seed N orphan content_vectors rows."""
    import sqlite3

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    try:
        create_schema(db, dims=4)
        for i in range(n_orphans):
            db.execute(
                "INSERT INTO content_vectors (hash, seq, pos) VALUES (?, 0, 0)",
                (f"orphan-cli-{i}",),
            )
        db.commit()
    finally:
        db.close()
    return db_path


# ---------------------------------------------------------------------------
# In-process — exercises the maintenance verb's arg routing + JSON shape
# ---------------------------------------------------------------------------


def test_worker_maintenance_human_output_reports_orphan_pruned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human output names the pruned count, table size, elapsed_ms.

    Sabotage proof: remove the ``pruned=N`` field from the format
    string and the assertion that ``pruned=1`` is in stdout fails.
    """
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    rc = worker_main(["maintenance", "--db-path", str(db_path), "--retention-days", "7"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "maintenance tick:" in captured.out
    assert "pruned=1" in captured.out
    assert "pruned_table_size=1" in captured.out
    assert "elapsed_ms=" in captured.out


def test_worker_maintenance_json_output_carries_all_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json envelope matches the MaintenanceTickResult shape."""
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=2)
    rc = worker_main(["maintenance", "--db-path", str(db_path), "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["orphans_pruned"] == 2
    assert payload["pruned_table_size"] == 2
    assert payload["current_orphan_count"] == 0
    assert "usearch_rebuilt" in payload
    assert "fts_orphans_healed" in payload
    assert "elapsed_ms" in payload


def test_worker_maintenance_second_run_is_idempotent_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Idempotency outcome proof: second run reports pruned=0."""
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)

    # First run prunes the orphan.
    worker_main(["maintenance", "--db-path", str(db_path), "--json"])
    first = json.loads(capsys.readouterr().out)
    assert first["orphans_pruned"] == 1

    # Second run on the same DB — no new orphans, must report 0.
    worker_main(["maintenance", "--db-path", str(db_path), "--json"])
    second = json.loads(capsys.readouterr().out)
    assert second["orphans_pruned"] == 0, f"second maintenance run must be a no-op; got {second['orphans_pruned']}"
    # The soft-delete row from the first run is still in the table.
    assert second["pruned_table_size"] == 1


# ---------------------------------------------------------------------------
# F30-style subprocess outcome test — exercises the composed CLI path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kairix_worker_maintenance_subprocess_prunes_orphan(tmp_path: Path) -> None:
    """Subprocess invocation of ``kairix worker maintenance`` against a tmp DB.

    F30-style: subprocess.run with the subcommand literal, then assert
    against the captured stdout. No reliance on returncode alone.

    Sabotage proof: drop the orphan SELECT predicate (set to
    ``WHERE 1=0``) and the ``pruned=1`` assertion fails when the
    subprocess returns ``pruned=0``.
    """
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    # Point the subprocess at THIS worktree's kairix package so the
    # test sees the in-worktree code, not the primary checkout's
    # editable install (which may lag during dev / cross-worktree work).
    worktree_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "maintenance",
            "--db-path",
            str(db_path),
            "--retention-days",
            "7",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(worktree_root),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(worktree_root)},
        check=False,
    )
    stdout = proc.stdout
    stderr = proc.stderr
    assert proc.returncode == 0, f"subprocess failed: stderr={stderr!r}"
    payload = json.loads(stdout)
    assert payload["orphans_pruned"] == 1, f"expected pruned=1; got payload={payload!r}; stderr={stderr!r}"
    assert payload["pruned_table_size"] == 1
    assert payload["current_orphan_count"] == 0


# ---------------------------------------------------------------------------
# kairix features status --maintenance
# ---------------------------------------------------------------------------


def test_features_status_maintenance_block_renders(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``kairix features status --maintenance`` renders the maintenance block.

    Tested via the in-process ``main`` with an injected diagnostics
    provider so the test stays decoupled from the real config / paths.
    Sabotage proof: drop the ``render_maintenance_human`` call in
    ``main()`` and the "current_orphan_count:" line disappears from
    stdout.
    """
    from kairix.core.features.cli import MaintenanceDiagnostics
    from kairix.core.features.cli import main as features_main
    from kairix.core.features.resolver import FlagStatus

    def _status_provider() -> tuple[FlagStatus, ...]:
        return (
            FlagStatus(
                name="maintenance_loop",
                default=False,
                effective=True,
                source="env",
                stage="introduce",
                introduced_in="v2026.5.24",
                target_retire_in="v2027.5.24",
                owner="connector-framework",
                related_spec=None,
            ),
        )

    def _maintenance_provider(_db_path: str | None) -> MaintenanceDiagnostics:
        return MaintenanceDiagnostics(
            flag_enabled=True,
            last_tick_at_iso="2026-05-24T00:00:00Z",
            last_tick_orphans_pruned=42,
            current_orphan_count=0,
            pruned_table_size=42,
            next_scheduled_tick_at_iso="2026-05-25T00:00:00Z",
            interval_seconds=86400,
        )

    rc = features_main(
        ["status", "--maintenance"],
        status_provider=_status_provider,
        read_maintenance=_maintenance_provider,
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "KFEAT-021 maintenance loop:" in captured
    assert "enabled:                  true" in captured
    assert "last_tick_at:             2026-05-24T00:00:00Z" in captured
    assert "orphans_pruned_last_tick: 42" in captured
    assert "current_orphan_count:     0" in captured
    assert "next_scheduled_tick_at:   2026-05-25T00:00:00Z" in captured


def test_features_status_maintenance_json_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """JSON envelope includes the maintenance block when --maintenance is set."""
    from kairix.core.features.cli import MaintenanceDiagnostics
    from kairix.core.features.cli import main as features_main
    from kairix.core.features.resolver import FlagStatus

    def _status_provider() -> tuple[FlagStatus, ...]:
        return (
            FlagStatus(
                name="maintenance_loop",
                default=False,
                effective=False,
                source="default",
                stage="introduce",
                introduced_in="v2026.5.24",
                target_retire_in="v2027.5.24",
                owner="connector-framework",
                related_spec=None,
            ),
        )

    def _maintenance_provider(_db_path: str | None) -> MaintenanceDiagnostics:
        return MaintenanceDiagnostics(
            flag_enabled=False,
            last_tick_at_iso="",
            last_tick_orphans_pruned=0,
            current_orphan_count=4370,
            pruned_table_size=0,
            next_scheduled_tick_at_iso="",
            interval_seconds=86400,
        )

    rc = features_main(
        ["status", "--maintenance", "--json"],
        status_provider=_status_provider,
        read_maintenance=_maintenance_provider,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "maintenance" in payload
    assert payload["maintenance"] == {
        "enabled": False,
        "last_tick_at": "",
        "orphans_pruned_last_tick": 0,
        "current_orphan_count": 4370,
        "pruned_table_size": 0,
        "next_scheduled_tick_at": "",
        "interval_seconds": 86400,
    }


def test_features_status_without_maintenance_flag_omits_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Backward-compat: omitting --maintenance leaves the legacy output unchanged."""
    from kairix.core.features.cli import main as features_main
    from kairix.core.features.resolver import FlagStatus

    def _status_provider() -> tuple[FlagStatus, ...]:
        return ()

    rc = features_main(["status"], status_provider=_status_provider)
    assert rc == 0
    out = capsys.readouterr().out
    assert "KFEAT-021" not in out, "--maintenance was not supplied; block must not render"


# ---------------------------------------------------------------------------
# kairix worker preflight --json — maintenance block embedded
# ---------------------------------------------------------------------------


def test_preflight_json_envelope_carries_maintenance_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The preflight --json envelope embeds the KFEAT-021 maintenance block."""
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    rc = worker_main(["preflight", "--db-path", str(db_path), "--json"])
    # Orphans surface as a "warn" gap; healthy stays True so rc is 0.
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "maintenance" in payload, f"preflight envelope must carry maintenance block; got {payload!r}"
    block = payload["maintenance"]
    assert block["current_orphan_count"] == 1
    assert block["pruned_table_size"] == 0
    assert "enabled" in block
    assert "last_tick_at" in block
    assert "orphans_pruned_last_tick" in block


def test_preflight_auto_heal_prunes_orphan_vectors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--auto-heal now extends to orphan-vector pruning (KFEAT-021 Phase 1).

    Sabotage proof: remove the ``content-vectors-without-documents``
    branch from ``_auto_heal_gaps`` and the auto-heal stage leaves the
    orphan in place — current_orphan_count remains 1 after the heal.
    """
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    rc = worker_main(["preflight", "--db-path", str(db_path), "--json", "--auto-heal"])
    assert rc == 0
    out_text = capsys.readouterr().out
    # auto-heal echoes a status line above the JSON envelope; the
    # envelope is the final brace-block. Parse it out.
    json_start = out_text.find("{")
    assert json_start >= 0, f"no JSON envelope in stdout: {out_text!r}"
    payload = json.loads(out_text[json_start:])
    # Post-heal: orphan gone from content_vectors, moved to the
    # soft-delete table.
    assert payload["maintenance"]["current_orphan_count"] == 0, f"auto-heal should clear orphans; got {payload!r}"
    assert payload["maintenance"]["pruned_table_size"] == 1
    # The auto-heal status line names what was pruned.
    assert "auto-heal: pruning 1 orphan" in out_text


def test_in_process_io_seam_captures_stderr_on_explicit_sink(tmp_path: Path) -> None:
    """The in-process out/err sinks work — needed for the test suite's coverage."""
    from kairix.worker_cli import maintenance

    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    out = io.StringIO()
    err = io.StringIO()
    rc = maintenance(db_path=db_path, out=out, err=err, as_json=True)
    assert rc == 0
    assert json.loads(out.getvalue())["orphans_pruned"] == 1
    assert err.getvalue() == ""


# ---------------------------------------------------------------------------
# Clock interaction — elapsed_ms envelope shape
# ---------------------------------------------------------------------------


def test_maintenance_tick_reports_integer_elapsed_ms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The tick envelope reports ``elapsed_ms`` as a non-negative integer.

    Asserts the deterministic contract (the field is a clock-deltaed
    integer alongside the completed tick's outcome) rather than a
    wall-clock ceiling, which measures the host scheduler (F82).
    """
    db_path = _bootstrap_with_orphan(tmp_path, n_orphans=1)
    rc = worker_main(["maintenance", "--db-path", str(db_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["orphans_pruned"] == 1, f"tick should prune the seeded orphan; got {payload!r}"
    assert isinstance(payload["elapsed_ms"], int), f"elapsed_ms must be an int; got {payload['elapsed_ms']!r}"
    assert payload["elapsed_ms"] >= 0
