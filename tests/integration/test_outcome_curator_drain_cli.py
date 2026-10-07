"""F30-style outcome test — ``kairix curator drain`` subprocess surface (GH #334).

The drain subcommand is the operator-facing surface for the manual
catch-up path. This test invokes the binary via ``subprocess.run``,
points it at a sandboxed SQLite DB seeded with three person signals,
and asserts on the JSON envelope content — NOT on returncode alone,
NOT on internal call-counts.

The graph backend is real-but-degraded (the Neo4j driver isn't
installed in the test sandbox, so :func:`get_client` returns the
"unavailable" sentinel). That gives us a determinate JSON envelope
shape — ``neo4j_available=false, pushed=0`` — to assert on.

The composed production path exercised here:

  subprocess([kairix, curator, drain, --db-path, <tmp>, --format json])
    → kairix/agents/curator/cli.py:main
    → _drain_cmd
    → _default_drain_db_factory(--db-path)  opens the sandboxed DB
    → _default_neo4j_client_factory()       Neo4j driver not installed
    → run_neo4j_drain_tick(db, real-repo)   neo4j_available == False
    → JSON envelope to stdout
    → exit 0

Sabotage proofs documented in the agent report.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kairix.core.db.schema import create_schema

pytestmark = pytest.mark.integration


def _seed_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "drain_cli.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        create_schema(conn)
        # Three person signals — un-pushed, ordered by modified_at.
        for i in range(3):
            conn.execute(
                "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
                "sensitivity, pushed_to_neo4j, push_attempt_count) "
                "VALUES ('person', ?, ?, ?, 0.9, 'internal', 0, 0)",
                (f"person-{i}", f"vault://person-{i}.md", f"2026-05-2{i}T10:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_curator_drain_cli_subprocess_json_envelope_with_unavailable_neo4j(tmp_path: Path) -> None:
    """`kairix curator drain --format json` exits 0 with a parseable
    envelope reporting ``neo4j_available=false`` when the driver is
    not installed in the subprocess environment.

    Asserts on the envelope content — the operator + the worker both
    parse this shape, so the F30 contract is "this envelope is
    structurally stable", not "exit code == 0".
    """
    db_path = _seed_db(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "curator",
            "drain",
            "--db-path",
            str(db_path),
            "--format",
            "json",
            "--batch-size",
            "500",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"drain CLI exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    # Envelope shape is the F30 contract.
    for key in (
        "pushed",
        "failed",
        "skipped_relationships",
        "neo4j_available",
        "elapsed_ms",
        "batches_run",
        "dry_run",
    ):
        assert key in envelope, f"missing {key!r} in envelope: {sorted(envelope.keys())}"

    # Driver is not installed in the test sandbox → neo4j_available=false,
    # pushed=0 (no row touched), staged rows stay un-pushed.
    assert envelope["neo4j_available"] is False, (
        f"expected neo4j_available=False in degraded subprocess env; got {envelope['neo4j_available']!r}"
    )
    assert envelope["pushed"] == 0
    assert envelope["failed"] == 0
    assert envelope["dry_run"] is False
    assert envelope["batches_run"] == 1, f"expected exactly one tick; got {envelope['batches_run']}"

    # Source-of-truth check — the staged rows are unchanged.
    conn = sqlite3.connect(str(db_path))
    try:
        unpushed_count = conn.execute("SELECT COUNT(*) FROM entity_signals WHERE pushed_to_neo4j = 0").fetchone()[0]
    finally:
        conn.close()
    assert unpushed_count == 3, f"expected staged rows untouched, got {unpushed_count} un-pushed"


def test_curator_drain_cli_subprocess_text_format_reports_batches_run(tmp_path: Path) -> None:
    """Default text format prints operator-readable fields with the right counts."""
    db_path = _seed_db(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "curator",
            "drain",
            "--db-path",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"drain CLI exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    # Asserting on stdout content, not on returncode alone.
    assert "neo4j drain complete" in proc.stdout, f"missing summary line in stdout: {proc.stdout!r}"
    assert "batches_run            : 1" in proc.stdout, f"missing batches_run line: {proc.stdout!r}"
    assert "neo4j_available        : False" in proc.stdout, f"missing neo4j_available line: {proc.stdout!r}"


def test_curator_drain_cli_subprocess_invalid_format_exits_two(tmp_path: Path) -> None:
    """``kairix curator drain --format <bogus>`` exits 2 (argparse) with the usage line."""
    db_path = _seed_db(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "curator",
            "drain",
            "--db-path",
            str(db_path),
            "--format",
            "GH334-INVALID",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2, (
        f"expected exit 2 (argparse) for invalid format, got {proc.returncode}.\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "--format" in proc.stderr, f"stderr missing flag name: {proc.stderr!r}"
    assert "invalid choice" in proc.stderr, f"stderr missing argparse error phrase: {proc.stderr!r}"
