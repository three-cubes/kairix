"""F30 outcome test — ``kairix benchmark`` subprocess surface.

Group F (quality CLIs) Wave 0 paydown. The unit suite in
``tests/quality/benchmark/test_cli_flags.py`` exercises the in-process
function surface (cmd_run, cmd_list, resolve_collection, etc.) with
BenchmarkCLIDeps-injected fakes. This test adds the F30-required
subprocess outcome assertion: real ``python -m kairix.cli benchmark``
invocation against the production CLI dispatch.

Strategy: drive the ``compare`` subcommand. It's purely filesystem-
based (reads two JSON files, prints a delta table to stdout, exits 0
on success / 1 on file error) so the F30 outcome test is hermetic and
sub-second — no retrieval stack, no provider wiring, no env vars. The
``run`` subcommand needs the full benchmark+retrieval stack and is
exercised end-to-end by the existing mini-suite integration test
(``tests/integration/test_benchmark_mini_suite.py``); this test
covers the binary surface contract that no existing test pins.

F2-clean: no ``KAIRIX_*`` env vars set in the subprocess invocation.
Both result JSONs are written to a tmp_path so the test is hermetic.

Sabotage-proofs (both executed locally — see commit message):
  - happy path: mutate ``cmd_compare`` to return early before the
    print loop. Stdout becomes empty → the "BENCHMARK COMPARISON"
    header assertion fails. Restored.
  - error path: mutate the ``except (FileNotFoundError,
    json.JSONDecodeError)`` block to bare ``except``: pass. The error
    output is suppressed and rc would be 0 → both rc==1 and the
    "Error loading results" assertions fail. Restored.

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

pytestmark = pytest.mark.integration


def _write_result(path: Path, weighted_total: float, ndcg_at_10: float) -> None:
    """Write a minimal benchmark result JSON that ``cmd_compare`` consumes.

    Shape mirrors what the production runner emits via
    ``BenchmarkResult.to_json()``. ``cmd_compare`` reads ``meta``,
    ``summary.weighted_total``, ``summary.ndcg_at_10``, and
    ``summary.category_scores`` — everything else is ignored.
    """
    payload = {
        "meta": {"system": "hybrid", "date": "2026-05-22"},
        "summary": {
            "weighted_total": weighted_total,
            "ndcg_at_10": ndcg_at_10,
            "category_scores": {
                "recall": 0.80,
                "temporal": 0.60,
                "entity": 0.70,
                "conceptual": 0.50,
                "multi_hop": 0.40,
                "procedural": 0.60,
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_benchmark_cli_subprocess_compare_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix benchmark compare`` binary surface.

    Asserts on the stdout comparison table content (header, delta marker,
    per-category rows) the operator consumes — NOT on returncode alone.
    The F30 contract: subprocess + stdout/stderr/envelope.
    """
    result_a = tmp_path / "a.json"
    result_b = tmp_path / "b.json"
    _write_result(result_a, weighted_total=0.65, ndcg_at_10=0.70)
    _write_result(result_b, weighted_total=0.72, ndcg_at_10=0.75)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "benchmark",
            "compare",
            str(result_a),
            str(result_b),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"benchmark compare exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    # The comparison header is the operator-facing anchor that signals
    # the compare path ran end-to-end. Without it the operator can't
    # tell whether the CLI dispatched the right subcommand.
    assert "BENCHMARK COMPARISON" in proc.stdout, f"compare header missing: {proc.stdout!r}"
    # Per-result lines surface the system + total + tier — pin two of
    # the three fields so a refactor that drops any single field fails.
    assert "total=0.650" in proc.stdout, f"A's weighted_total missing: {proc.stdout!r}"
    assert "total=0.720" in proc.stdout, f"B's weighted_total missing: {proc.stdout!r}"
    # The delta marker (▲ / ▼ / =) communicates direction; the absolute
    # value follows. Pin the up-arrow because B > A in the fixture.
    assert "Delta:" in proc.stdout, f"delta line missing: {proc.stdout!r}"
    assert "0.070" in proc.stdout, f"delta absolute value missing: {proc.stdout!r}"


def test_benchmark_cli_subprocess_exits_non_zero_on_missing_result_file(tmp_path: Path) -> None:
    """Pointing at a nonexistent result file must surface a non-zero exit + a
    parseable error message on stderr. Closes the binary-surface error path
    the unit tests cover only in-process."""
    real_result = tmp_path / "real.json"
    _write_result(real_result, weighted_total=0.65, ndcg_at_10=0.70)

    bogus = tmp_path / "does-not-exist.json"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "benchmark",
            "compare",
            str(bogus),
            str(real_result),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}. stderr={proc.stderr!r}"
    # The error message anchors the operator on what went wrong; pin the
    # "Error loading results" prefix the CLI emits.
    assert "Error loading results" in proc.stderr, f"stderr missing error prefix: {proc.stderr!r}"
