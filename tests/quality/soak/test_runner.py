"""Unit tests for `kairix.quality.soak.run_soak`.

Each assertion in the runner gets a sabotage-proof test: the test passes when
the runner's check fires, and would silently pass (false-positive) if the
check were removed. The test injects a fake workload whose returned envelope
controls what the iteration looks like.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from kairix.quality.soak import run_soak

pytestmark = pytest.mark.unit


def _fake_workload(payload: dict[str, Any]) -> Any:
    """Return a workload runner that always returns the same payload.

    Two calls produce identical envelopes → identical signatures → no drift.
    """

    def runner(_suite: str) -> dict[str, Any]:
        return payload

    return runner


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_run_two_iterations_passes() -> None:
    result = run_soak(
        suite="fake",
        repeat=2,
        workload_runner=_fake_workload({"summary": {"weighted_total": 0.9}, "case_count": 5}),
    )
    assert result.passed, f"expected pass; got failures={[(f.kind, f.detail) for f in result.failures]}"
    assert result.error == ""
    assert len(result.iterations) == 2
    assert result.iterations[0].signature == result.iterations[1].signature


def test_envelope_shape_matches_design_spec() -> None:
    result = run_soak(
        suite="fake",
        repeat=2,
        workload_runner=_fake_workload({"summary": {"weighted_total": 0.9}, "case_count": 1}),
    )
    env = result.to_envelope()
    # Fields from the design's "JSON envelope" example.
    assert env["suite"] == "fake"
    assert env["repeat"] == 2
    assert env["passed"] is True
    assert env["failures"] == []
    assert len(env["iterations"]) == 2
    # Each iteration carries every field the design spec promises.
    for it in env["iterations"]:
        for field_name in ("index", "duration_s", "memory_mb", "stderr_bytes", "fd_count", "signature"):
            assert field_name in it, f"iteration missing field {field_name!r}; got {sorted(it.keys())}"


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_repeat_lt_2_fails_with_structured_reason() -> None:
    """A single-iteration soak can't compare anything; reject early with affordance.

    Sabotage: remove the `if repeat < 2:` guard and this test breaks because
    we get an empty `iterations` list with `passed=True`.
    """
    result = run_soak(suite="fake", repeat=1, workload_runner=_fake_workload({"summary": {}, "case_count": 0}))
    assert result.passed is False
    assert len(result.failures) == 1
    assert result.failures[0].kind == "invalid_argument"
    assert "repeat must be >= 2" in result.failures[0].detail


# ---------------------------------------------------------------------------
# Signature drift — the cross-iteration determinism property
# ---------------------------------------------------------------------------


def test_signature_drift_fails_when_workload_nondeterministic() -> None:
    """A workload returning different envelopes across calls fails the soak.

    Sabotage: remove the signature check and this test breaks because two
    non-matching iterations would pass with no recorded failure.
    """
    call_count = [0]

    def nondeterministic(_suite: str) -> dict[str, Any]:
        call_count[0] += 1
        # Different content on each call → different signatures.
        return {"summary": {"weighted_total": 0.9 + call_count[0] * 0.001}, "case_count": 1}

    result = run_soak(suite="fake", repeat=3, workload_runner=nondeterministic)

    drift_failures = [f for f in result.failures if f.kind == "signature_mismatch"]
    assert len(drift_failures) == 2, (
        f"expected 2 signature-mismatch failures (iter 1 + iter 2 vs iter 0); "
        f"got {len(drift_failures)}: {[f.detail for f in drift_failures]}"
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# Log-volume — the #275 regression class
# ---------------------------------------------------------------------------


def test_log_volume_fails_when_workload_spews_stderr() -> None:
    """A workload that prints to stderr beyond the cap fails the soak.

    This is the #275 property: 1400 warning lines on a 200-case suite =
    ~140 KB / iter. With repeat=3 + cap=5MB/repeat = 15MB cap total, an
    unbounded warning workload that emits 7MB/iter triggers the failure.

    Sabotage: remove _check_log_volume and this test breaks because the
    LARGE_STDERR workload produces 21MB stderr but failures stays empty.
    """
    large_chunk = "x" * (7 * 1024 * 1024)  # 7 MB per iter

    def noisy(_suite: str) -> dict[str, Any]:
        # Write directly to stderr so redirect_stderr captures it.
        sys.stderr.write(large_chunk)
        return {"summary": {"weighted_total": 0.9}, "case_count": 1}

    result = run_soak(
        suite="fake",
        repeat=3,
        max_log_volume_mb=5.0,  # 5 MB/iter x 3 = 15 MB cap total
        workload_runner=noisy,
    )
    log_failures = [f for f in result.failures if f.kind == "log_volume"]
    assert len(log_failures) == 1, f"expected 1 log_volume failure; got {[f.detail for f in result.failures]}"
    assert result.passed is False
    # Detail should name the actual MB so the operator sees the gap.
    assert "MB" in log_failures[0].detail


def test_log_volume_quiet_workload_passes() -> None:
    """Sanity: a workload that emits nothing on stderr clears the gate."""
    result = run_soak(
        suite="fake",
        repeat=3,
        max_log_volume_mb=0.001,  # 1 KB/repeat — very tight
        workload_runner=_fake_workload({"summary": {"weighted_total": 0.9}, "case_count": 1}),
    )
    log_failures = [f for f in result.failures if f.kind == "log_volume"]
    assert log_failures == [], f"silent workload should not fail log_volume; got {[f.detail for f in log_failures]}"


# ---------------------------------------------------------------------------
# Top-level error handling
# ---------------------------------------------------------------------------


def test_workload_exception_populates_error_envelope() -> None:
    """A raising workload doesn't crash run_soak — it surfaces in `error`."""

    def boom(_suite: str) -> dict[str, Any]:
        raise RuntimeError("workload exploded")

    result = run_soak(suite="fake", repeat=3, workload_runner=boom)

    assert result.passed is False
    assert result.error.startswith("RuntimeError:")
    assert "workload exploded" in result.error
    # No iterations completed before the explosion.
    assert result.iterations == []


def test_workload_exception_mid_run_preserves_completed_iterations() -> None:
    """When the workload raises on iter-2, iter-0 + iter-1 should still be recorded."""
    state = {"calls": 0}

    def flaky(_suite: str) -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 3:
            raise RuntimeError("third call breaks")
        return {"summary": {"weighted_total": 0.9}, "case_count": 1}

    result = run_soak(suite="fake", repeat=4, workload_runner=flaky)

    assert result.passed is False
    assert result.error.startswith("RuntimeError:")
    # First two iterations completed.
    assert len(result.iterations) == 2
    assert [it.index for it in result.iterations] == [0, 1]
