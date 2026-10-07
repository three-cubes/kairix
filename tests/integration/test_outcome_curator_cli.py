"""F30 outcome test — ``kairix curator`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the curator CLI's half
of the paydown — the existing unit tests in
``tests/curator/test_cli_unit.py`` continue to cover the function-call
seam (``neo4j_client`` / ``client_factory`` kwarg injection of a
``FakeNeo4jClient``); this test adds the F30-required subprocess
outcome assertion.

The curator CLI's only state is Neo4j — there is no filesystem
``--document-root`` analogue to add. The production health check
gracefully degrades when the neo4j driver is unavailable: the report
sets ``neo4j_available=false``, ``total_entities=0``, ``ok=true``, and
exits 0. That gracefully-degraded path is the F30 happy-path anchor
here — subprocess + JSON envelope assertion against the agent-readable
contract.

Boundary chain exercised (degraded happy-path):

  subprocess([kairix, curator, health, --format, json])
    → kairix/agents/curator/cli.py:main
    → _default_neo4j_client_factory() → import kairix.knowledge.graph.client
    → graph.client returns the "neo4j driver not installed" sentinel
    → run_health_check(client, staleness_days=90)
    → format_report_json(report) → JSON to stdout
    → exit 0

Boundary chain exercised (argparse error-path):

  subprocess([kairix, curator, health, --format, bogus])
    → argparse rejects the --format choice
    → exit 2 with the usage block + "invalid choice" on stderr

Sabotage-proof (both executed):
  (a) Mutated ``kairix/agents/curator/cli.py``'s ``sys.exit(0)`` after
      the health command body to ``sys.exit(1)`` — the JSON envelope
      test's ``returncode == 0`` assertion fails. Restored.
  (b) Mutated the ``--format`` argparse choices from
      ``choices=["text", "json"]`` to ``choices=["text", "json", "bogus"]``
      — the invalid-format test's ``returncode == 2`` assertion fails
      (the parser now accepts ``bogus`` and runs the health command
      against an unrecognised format, which falls through to the
      default text branch and exits 0). Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_curator_cli_subprocess_health_json_envelope_outcome() -> None:
    """``kairix curator health --format json`` exits 0 with a parseable
    JSON envelope on stdout, even when Neo4j is unavailable.

    Asserts on the envelope's required keys + ``neo4j_available=false``
    + ``ok=true`` (no issues to report when there are no entities to
    check). NOT a returncode-only assertion. The envelope shape is
    what cron + agents parse — this is the F30 outcome contract.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "curator", "health", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"curator health exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    for key in (
        "generated_at",
        "total_entities",
        "entities_by_type",
        "synthesis_failures",
        "stale_entities",
        "missing_vault_path",
        "neo4j_available",
        "neo4j_node_counts",
        "staleness_threshold_days",
        "ok",
        "issue_count",
    ):
        assert key in envelope, f"missing {key!r} in envelope: {sorted(envelope.keys())}"

    assert envelope["neo4j_available"] is False, (
        f"expected neo4j_available=False in degraded subprocess env; got {envelope['neo4j_available']!r}"
    )
    assert envelope["total_entities"] == 0, f"expected zero entities when neo4j offline: {envelope}"
    assert envelope["ok"] is True, f"expected ok=True when there are no issues to report: {envelope}"
    assert envelope["issue_count"] == 0, f"expected issue_count=0: {envelope}"


def test_curator_cli_subprocess_invalid_format_exits_two() -> None:
    """``kairix curator health --format <bogus>`` exits 2 (argparse).

    Proves the argparse choices guard reaches the production binary.
    Argparse writes the actionable usage block + "invalid choice" line
    to stderr — assert on that content, not just the exit code.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "curator", "health", "--format", "F30-INVALID"],
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
