"""F30 outcome test — ``kairix worker`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the worker CLI's half
of the paydown — the existing unit tests in
``tests/test_worker_cli.py`` continue to cover the function-call seam
(in-process injection via ``state_path=`` / ``flag_path=`` kwargs on
``main()``); this test adds the F30-required subprocess outcome
assertion.

F2-clean by construction: subprocess is driven via ``--state-path``
(mirrors the canonical ``--document-root`` pattern from
``kairix store crawl``). No ``KAIRIX_*`` env vars are set in the
subprocess invocation — the test runs against the production binary's
actual CLI surface, with the tmp state file path passed as an explicit
argument.

Boundary chain exercised (happy path):

  subprocess([kairix, worker, status, --state-path <tmp/file.json>, --json])
    → kairix/worker_cli.py:main
    → status(state_path=<tmp>, as_json=True)
    → kairix.worker_state.read_state(<tmp>) → WorkerState
    → WorkerState.to_dict() → JSON envelope
    → stdout

Sabotage-proof anchor (executed, not mentally walked):
deleting the ``state_path=resolved_state`` plumb in ``main`` (i.e.
ignoring the ``--state-path`` arg and falling back to the default
``worker_state_path()``) makes ``status`` read a non-existent file in
the production data dir → returns 1 with "no state file" on stderr →
the happy-path assertion on ``returncode == 0`` fails. Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kairix.worker_state import WorkerPhase, WorkerState, write_state

pytestmark = pytest.mark.integration


def _seed_worker_state(state_path: Path) -> WorkerState:
    """Write a representative WorkerState to ``state_path``.

    Mirrors the shape the running worker persists each phase transition:
    INGEST phase, non-zero counters, recent embed timestamps. Returns the
    WorkerState so callers can assert against the round-tripped fields.
    """
    state = WorkerState(
        current_phase=WorkerPhase.INGEST,
        embedded_total=17,
        failed_chunks_total=2,
        recall_alerts_total=1,
        restart_count=4,
        consecutive_embed_noops=0,
        last_embed_run_at=1716_000_000.0,
        last_embed_did_work=True,
        started_at=1715_999_000.0,
    )
    write_state(state, state_path)
    return state


def test_worker_status_subprocess_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix worker status --json`` binary against a tmp state file.

    Asserts on the JSON envelope content an ops dashboard consumes —
    NOT on returncode alone, NOT on internal fake call-counts. The F30
    contract: subprocess + stdout/stderr/envelope assertion.
    """
    state_path = tmp_path / "worker-state.json"
    seeded = _seed_worker_state(state_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "status",
            "--state-path",
            str(state_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"worker status exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    # Phase string round-trips through the WorkerPhase enum
    assert envelope["current_phase"] == seeded.current_phase.value, f"phase mismatch: {envelope}"
    # Counters round-trip losslessly
    assert envelope["embedded_total"] == 17, f"embedded_total: {envelope}"
    assert envelope["failed_chunks_total"] == 2, f"failed_chunks_total: {envelope}"
    assert envelope["recall_alerts_total"] == 1, f"recall_alerts_total: {envelope}"
    assert envelope["restart_count"] == 4, f"restart_count: {envelope}"
    # Timestamp fields land in the envelope (operator dashboards key off these)
    assert envelope["last_embed_run_at"] == 1716_000_000.0, f"last_embed_run_at: {envelope}"
    assert envelope["last_embed_did_work"] is True, f"last_embed_did_work: {envelope}"
    assert envelope["started_at"] == 1715_999_000.0, f"started_at: {envelope}"


def test_worker_status_subprocess_exits_non_zero_on_missing_state(tmp_path: Path) -> None:
    """Pointing at a non-existent state file must surface a non-zero exit +
    a parseable error message on stderr. Closes the binary-surface error
    path the unit tests cover only in-process.

    The missing-state branch is the production canary an operator-shell
    monitor (or Docker healthcheck) relies on: exit 1 means the worker
    has never started or has been removed; exit 0 means a state file is
    present and parseable.
    """
    bogus = tmp_path / "does-not-exist.json"
    assert not bogus.exists()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "status",
            "--state-path",
            str(bogus),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}. stderr={proc.stderr!r}"
    assert "kairix worker:" in proc.stderr, f"stderr missing error prefix: {proc.stderr!r}"
    assert "no state file" in proc.stderr.lower(), f"stderr missing 'no state file': {proc.stderr!r}"
    assert proc.stdout == "", f"stdout should be empty on missing state, got: {proc.stdout!r}"


def test_worker_reextract_subprocess_envelope_outcome(tmp_path: Path) -> None:
    """``kairix worker reextract --source-name <src> --db-path <db> --json``
    drives the real binary against a tmp DB and asserts on the JSON envelope.

    Seeds a fresh DB with no dead_letter rows for the named source — the
    envelope should report all-zero counts (the no-op shape) with
    skipped_no_connector matching the empty config. F30: subprocess +
    stdout/JSON-envelope assertion, not internal call-counts.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "reextract.sqlite"
    db = sqlite3.connect(str(db_path))
    try:
        create_schema(db)
    finally:
        db.close()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "reextract",
            "--source-name",
            "no-such-connector",
            "--db-path",
            str(db_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"reextract exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    envelope = json.loads(proc.stdout)
    assert envelope["source_name"] == "no-such-connector"
    assert envelope["recovered"] == 0
    assert envelope["still_failing"] == 0
    assert envelope["skipped_no_bronze"] == 0
    # Source name has no connector and no dead_letter rows → all-zero envelope.
    assert envelope["skipped_no_connector"] == 0
    assert envelope["dry_run"] is False


def test_worker_reextract_dry_run_does_not_commit(tmp_path: Path) -> None:
    """``--dry-run`` walks the same logic but commits nothing. With an
    empty DB the envelope still parses + reports the dry_run=True flag."""
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "reextract-dry.sqlite"
    db = sqlite3.connect(str(db_path))
    try:
        create_schema(db)
    finally:
        db.close()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "reextract",
            "--source-name",
            "no-such-connector",
            "--db-path",
            str(db_path),
            "--dry-run",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, f"dry-run exited {proc.returncode}; stderr={proc.stderr!r}"
    envelope = json.loads(proc.stdout)
    assert envelope["dry_run"] is True


def test_worker_reextract_envelope_includes_skipped_source_unavailable_counter(tmp_path: Path) -> None:
    """Phase 6 of streaming-bronze (#27, F30 outcome test): the JSON envelope
    surfaces the ``skipped_source_unavailable`` counter introduced by Phase 5
    re-extract dual-mode handling.

    Even on an empty DB the field must be present (zero), so operators can
    write monitoring against the field without it sometimes being absent.

    Sabotage proof: remove ``skipped_source_unavailable`` from the json.dumps
    dict in worker_cli.reextract; this test fails with KeyError.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "reextract-sourceunav.sqlite"
    db = sqlite3.connect(str(db_path))
    try:
        create_schema(db)
    finally:
        db.close()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "reextract",
            "--source-name",
            "no-such-connector",
            "--db-path",
            str(db_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}; stderr={proc.stderr!r}"
    envelope = json.loads(proc.stdout)
    assert "skipped_source_unavailable" in envelope, (
        f"Phase 6 contract: envelope must include skipped_source_unavailable; got keys: {sorted(envelope.keys())}"
    )
    assert envelope["skipped_source_unavailable"] == 0


def test_worker_reextract_human_output_includes_skipped_source_unavailable(tmp_path: Path) -> None:
    """Human-readable (non-JSON) output also surfaces the new counter.
    Operators reading the terminal output for a recovery run see all
    five counters consistently.

    Sabotage proof: remove the skipped_source_unavailable= from the
    f-string in worker_cli.reextract's else branch; this test fails
    because the substring isn't in stdout.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "reextract-human.sqlite"
    db = sqlite3.connect(str(db_path))
    try:
        create_schema(db)
    finally:
        db.close()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "worker",
            "reextract",
            "--source-name",
            "no-such-connector",
            "--db-path",
            str(db_path),
            # no --json — human output
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "skipped_source_unavailable=0" in proc.stdout, (
        f"human output should include the new counter; got: {proc.stdout!r}"
    )
