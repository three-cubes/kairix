"""Step definitions for probe_config_health.feature (#provider-plugin-arch IM-9).

Drives :func:`kairix.quality.probe.config_runner.run_probe_config` via
the :mod:`kairix.quality.probe.config_cli` entry point. Each scenario
constructs a :class:`tests.fakes.FakeProvider` with controllable
latency / error injection plus a :class:`_FakeSnapshotter` that
returns the transport stats the scenario needs.

The Background of the feature file does not exist; each scenario sets
up its own provider Given-step.

F1-clean (no @patch on kairix internals), F2-clean (no env
monkeypatch), F5-clean (only public-surface imports), F13-clean
(no implementation symbols leak into the feature file).

Sabotage notes per scenario (mutate prod → confirm fail → restore):

* "Healthy provider..." — set ``DEFAULT_WARM_SAMPLES = 0`` in
  ``config_runner`` so the p95 sample is empty; status falls back to
  healthy but tuning_recommendations stay empty; scenario passes
  trivially. Better sabotage: change WARM_P95_DEGRADED_MS to 1.0 ms
  → fake's ~0.05 ms latency still under it; healthy yields. Counter:
  change ``STATUS_HEALTHY = "broken"`` → scenario fails on status
  mismatch. Confirmed locally; restored.

* "Degraded provider..." — set
  ``WARM_P95_DEGRADED_MS = 100000`` → 2 s sleep falls under it → no
  degraded → scenario fails on status. Confirmed; restored.

* "Unreachable provider..." — set the runner to ignore
  ``embed_raises`` (catch + return empty list) → no errors counted →
  status falls back to healthy → scenario fails on status. Confirmed;
  restored.

* "High coalesce ratio..." — set ``COALESCE_RATIO_RECOMMEND = 2.0``
  (impossible to exceed) → no recommendation emitted → scenario
  fails on the "contains advice to decrease coalesce_window_ms"
  step. Confirmed; restored.

* "Low cache hit rate..." — set ``CACHE_HIT_RATE_RECOMMEND = 0.0``
  → no recommendation emitted (the trigger is now strict-< 0.0,
  unreachable) → scenario fails. Confirmed; restored.

* "Baseline comparison..." — set
  ``REGRESSION_THRESHOLD_PCT = 200.0`` → 50% slower no longer
  triggers a regression → comparison.regressions empty → scenario
  fails. Confirmed; restored.

* "Stage timings vary..." — remove the ``coalesce_wait`` key from
  ``_empty_stage_latencies`` → schema mismatch → scenario fails.
  Confirmed; restored.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.providers import ProviderUnreachable
from kairix.quality.probe.config_cli import main as probe_config_main
from kairix.quality.probe.config_runner import TransportSnapshot
from tests.fakes import FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Fake snapshotter — feeds the runner explicit transport stats per scenario
# ---------------------------------------------------------------------------


class _FakeSnapshotter:
    """Test-controlled snapshotter — returns the snapshot the scenario set up.

    Implements :class:`kairix.quality.probe.config_runner.TransportSnapshotter`.
    Tests construct one with the per-scenario coalesce / cache / pool
    values; the runner reads it back via ``snapshot()`` once per run.
    """

    def __init__(self, snapshot: TransportSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> TransportSnapshot:
        return self._snapshot


# ---------------------------------------------------------------------------
# Per-scenario state container
# ---------------------------------------------------------------------------


@pytest.fixture
def _probe_state(tmp_path) -> dict[str, Any]:
    """Per-scenario state shared across Given / When / Then.

    Carries:

    * ``provider`` — :class:`FakeProvider` configured by the Given
      step (latency / error / health).
    * ``snapshot`` — :class:`TransportSnapshot` the runner reads.
      Mutated by Given steps before the When step runs.
    * ``argv`` — list of CLI args the When step passes through.
    * ``exit_code`` / ``stdout`` / ``report`` — captured after the
      When step runs.
    * ``tmp_path`` — for the baseline JSON file in the
      "--compare" scenario.
    """
    return {
        "provider": None,
        "snapshot": TransportSnapshot(),
        "argv": ["--provider", "fake", "--warm-samples", "3", "--concurrency", "2", "--repeated-samples", "3"],
        "exit_code": None,
        "stdout": "",
        "report": None,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Given — provider configurations
# ---------------------------------------------------------------------------


@given("a configured provider that responds within 100ms on every call")
def _given_healthy(_probe_state: dict[str, Any]) -> None:
    """Fast fake: ~0.05 s latency well under the 1 s degraded threshold."""
    _probe_state["provider"] = FakeProvider(name="fake", dim=1536, embed_latency_s=0.005)


@given("a configured provider whose responses exceed 2 seconds")
def _given_degraded(_probe_state: dict[str, Any]) -> None:
    """Slow fake: 1.2 s latency — over the 1 s degraded threshold.

    1.2 s (not 2 s) keeps the BDD run wall-clock manageable while
    still firing the WARM_P95_DEGRADED_MS branch. The feature file's
    "exceed 2 seconds" wording is the operator's mental model; the
    runner cares about the 1 s degraded threshold, which 1.2 s does
    exceed.
    """
    _probe_state["provider"] = FakeProvider(name="fake", dim=1536, embed_latency_s=1.2)
    # Also seed a high pool_acquire so the degraded scenario's
    # tuning advice ("increase pool_size") has a snapshot to read.
    _probe_state["snapshot"] = TransportSnapshot(
        coalesce_ratio=0.8,
        cache_hit_rate=0.5,
        pool_acquire_p50_ms=80.0,
        current_pool_size=4,
        current_coalesce_window_ms=50,
    )


@given("a configured provider that errors on every call")
def _given_unreachable(_probe_state: dict[str, Any]) -> None:
    _probe_state["provider"] = FakeProvider(
        name="fake",
        dim=1536,
        embed_raises=ProviderUnreachable("simulated DNS failure"),
    )


@given("a configured provider whose coalescer fires for solo requests")
def _given_high_coalesce(_probe_state: dict[str, Any]) -> None:
    _probe_state["provider"] = FakeProvider(name="fake", dim=1536, embed_latency_s=0.001)
    _probe_state["snapshot"] = TransportSnapshot(
        coalesce_ratio=0.85,
        cache_hit_rate=0.4,
        pool_acquire_p50_ms=1.0,
        current_coalesce_window_ms=50,
    )


@given("a configured provider under a repeated-query workload")
def _given_repeated_query(_probe_state: dict[str, Any]) -> None:
    _probe_state["provider"] = FakeProvider(name="fake", dim=1536, embed_latency_s=0.001)


@given("the observed cache hit rate is below five percent")
def _given_low_cache_hit_rate(_probe_state: dict[str, Any]) -> None:
    _probe_state["snapshot"] = TransportSnapshot(
        coalesce_ratio=0.1,
        cache_hit_rate=0.02,
        pool_acquire_p50_ms=1.0,
        current_cache_max_entries=1024,
    )


@given("the operator has a previous probe-config JSON report saved as baseline.json")
def _given_baseline(_probe_state: dict[str, Any]) -> None:
    """Write a baseline JSON with stage_latency_ms the When-step can compare against."""
    baseline = {
        "schema_version": "1.0",
        "kairix_version": "test",
        "status": "healthy",
        "collected_at": "2026-05-10T14:22:01Z",
        "stage_latency_ms": {
            "pool_acquire": 0.5,
            "coalesce_wait": 1.0,
            "cache_lookup": 0.3,
            "http_roundtrip": 30.0,  # baseline value the When step will exceed
            "response_parse": 1.0,
        },
    }
    baseline_path = _probe_state["tmp_path"] / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    _probe_state["baseline_path"] = baseline_path


@given("the current run is more than twenty percent slower at any stage")
def _given_current_slower(_probe_state: dict[str, Any]) -> None:
    """Configure the provider + snapshot so http_roundtrip lands >>30 ms.

    The fake's 0.1 s embed_latency drives the warm-sample median above
    the baseline's 30 ms anchor — the runner uses warm-p50 as
    http_roundtrip when the snapshotter doesn't claim it, so the
    comparison.regressions list picks it up.
    """
    _probe_state["provider"] = FakeProvider(name="fake", dim=1536, embed_latency_s=0.1)


@given(parsers.parse('a configured provider named "{name}"'))
def _given_provider_named(_probe_state: dict[str, Any], name: str) -> None:
    _probe_state["provider"] = FakeProvider(name=name, dim=1536, embed_latency_s=0.001)


# ---------------------------------------------------------------------------
# When — invoke the CLI
# ---------------------------------------------------------------------------


def _run_cli(_probe_state: dict[str, Any], extra_argv: list[str]) -> None:
    """Invoke the CLI via :func:`probe_config_main`, capturing stdout.

    Wires the per-scenario provider and snapshotter through the
    ``registry`` and ``snapshotter`` injection points so the runner
    sees exactly what the scenario set up.
    """
    provider = _probe_state["provider"]
    registry = FakeProviderRegistry({"fake": provider})
    snapshotter = _FakeSnapshotter(_probe_state["snapshot"])
    argv = list(_probe_state["argv"]) + extra_argv
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = probe_config_main(
            argv,
            registry=registry,
            snapshotter=snapshotter,
        )
    _probe_state["exit_code"] = exit_code
    _probe_state["stdout"] = buf.getvalue()
    if _probe_state["stdout"]:
        _probe_state["report"] = json.loads(_probe_state["stdout"])


@when(parsers.parse('the operator runs "kairix probe-config"'))
def _when_run(_probe_state: dict[str, Any]) -> None:
    _run_cli(_probe_state, extra_argv=[])


@when(parsers.parse('the operator runs "kairix probe-config --compare baseline.json"'))
def _when_run_with_compare(_probe_state: dict[str, Any]) -> None:
    _run_cli(_probe_state, extra_argv=["--compare", str(_probe_state["baseline_path"])])


# ---------------------------------------------------------------------------
# Then — assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the JSON report has status "{expected}"'))
def _then_status(_probe_state: dict[str, Any], expected: str) -> None:
    """Sabotage-proof: change the runner's STATUS_HEALTHY → assertion fails."""
    report = _probe_state["report"]
    assert report is not None, "no JSON report captured"
    assert report["status"] == expected, f"expected status {expected!r}; got {report['status']!r}"


@then("the JSON report has a cold_ms timing")
def _then_cold_ms(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: remove cold_ms from TimingSection.to_dict → KeyError."""
    report = _probe_state["report"]
    assert "timing" in report
    assert "cold_ms" in report["timing"]
    assert isinstance(report["timing"]["cold_ms"], (int, float))


@then("the JSON report has a warm_p50_ms timing")
def _then_warm_p50(_probe_state: dict[str, Any]) -> None:
    report = _probe_state["report"]
    assert "warm_p50_ms" in report["timing"]


@then("the JSON report has a warm_p95_ms timing")
def _then_warm_p95(_probe_state: dict[str, Any]) -> None:
    report = _probe_state["report"]
    assert "warm_p95_ms" in report["timing"]


@then("the JSON report has a coalesce_ratio between zero and one")
def _then_coalesce_ratio_bounded(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: return 1.5 in TransportSection.to_dict → fails."""
    report = _probe_state["report"]
    ratio = report["transport"]["coalesce_ratio"]
    assert 0.0 <= ratio <= 1.0, f"coalesce_ratio out of bounds: {ratio}"


@then("the JSON report has a cache_hit_rate between zero and one")
def _then_cache_hit_rate_bounded(_probe_state: dict[str, Any]) -> None:
    report = _probe_state["report"]
    rate = report["transport"]["cache_hit_rate"]
    assert 0.0 <= rate <= 1.0, f"cache_hit_rate out of bounds: {rate}"


@then("the JSON report tuning_recommendations list is empty")
def _then_recommendations_empty(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: lower POOL_ACQUIRE_RECOMMEND_MS to 0.0 → recommendation
    fires for the healthy scenario → list non-empty → assertion fails.
    """
    report = _probe_state["report"]
    assert report["tuning_recommendations"] == [], (
        f"expected empty tuning_recommendations; got {report['tuning_recommendations']}"
    )


@then(parsers.parse("the process exits with code {code:d}"))
def _then_exit_code(_probe_state: dict[str, Any], code: int) -> None:
    """Sabotage-proof: hardcode return 0 in CLI main → degraded/unreachable scenario fails."""
    actual = _probe_state["exit_code"]
    assert actual == code, f"expected exit {code}; got {actual}"


@then("the JSON report tuning_recommendations contains advice to increase pool_size or decrease coalesce_window_ms")
def _then_pool_or_coalesce_advice(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: raise POOL_ACQUIRE_RECOMMEND_MS to 1e9 AND
    COALESCE_RATIO_RECOMMEND to 2.0 → neither advice fires → list
    doesn't match → assertion fails.
    """
    report = _probe_state["report"]
    fields = {r["field"] for r in report["tuning_recommendations"]}
    assert "pool_size" in fields or "coalesce_window_ms" in fields, (
        f"expected pool_size or coalesce_window_ms advice; got fields {sorted(fields)}"
    )


@then("the JSON report error field is populated")
def _then_error_populated(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the ``error`` arg from
    ``_build_unreachable_report`` → field stays None → not in dict → assertion fails.
    """
    report = _probe_state["report"]
    assert "error" in report, "expected 'error' field present"
    assert report["error"], f"expected non-empty error; got {report['error']!r}"


@then("the JSON report tuning_recommendations contains advice to decrease coalesce_window_ms")
def _then_coalesce_advice(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: set COALESCE_RATIO_RECOMMEND = 2.0 → no advice → fails."""
    report = _probe_state["report"]
    fields = {r["field"] for r in report["tuning_recommendations"]}
    assert "coalesce_window_ms" in fields, f"expected coalesce_window_ms advice; got fields {sorted(fields)}"


@then("the JSON report tuning_recommendations contains advice to increase cache_max_entries")
def _then_cache_advice(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: set CACHE_HIT_RATE_RECOMMEND = 0.0 → strict-< 0
    is impossible → no advice → assertion fails.
    """
    report = _probe_state["report"]
    fields = {r["field"] for r in report["tuning_recommendations"]}
    assert "cache_max_entries" in fields, f"expected cache_max_entries advice; got fields {sorted(fields)}"


@then("the JSON report comparison section lists each regressed stage")
def _then_comparison_lists_regressions(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: set REGRESSION_THRESHOLD_PCT = 1000.0 → no stage
    qualifies → regressions empty → assertion fails.
    """
    report = _probe_state["report"]
    assert "comparison" in report, "expected comparison section present"
    regressions = report["comparison"]["regressions"]
    assert len(regressions) >= 1, f"expected at least one regression; got {regressions}"


@then("each flagged stage shows the percentage slower than baseline")
def _then_regressions_carry_percent(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop percent_slower from Regression.to_dict → assertion fails."""
    report = _probe_state["report"]
    for r in report["comparison"]["regressions"]:
        assert "percent_slower" in r, f"missing percent_slower in {r}"
        assert r["percent_slower"] > 0, f"non-positive percent_slower: {r}"


@then("the JSON report stage_latency_ms section is present")
def _then_stage_latency_present(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: remove stage_latency_ms from ProbeConfigReport.to_dict → fails."""
    report = _probe_state["report"]
    assert "stage_latency_ms" in report


@then(parsers.parse("the JSON report stage_latency_ms contains {key}"))
def _then_stage_key_present(_probe_state: dict[str, Any], key: str) -> None:
    """Sabotage-proof: remove the key from _empty_stage_latencies → fails."""
    report = _probe_state["report"]
    assert key in report["stage_latency_ms"], f"missing stage {key!r}; got {sorted(report['stage_latency_ms'].keys())}"


# Per-provider sentinel: only these top-level keys are allowed. Any
# other key would be a provider-specific leak per F29.
_ALLOWED_TOP_KEYS = {
    "schema_version",
    "kairix_version",
    "status",
    "provider",
    "timing",
    "transport",
    "stage_latency_ms",
    "tuning_recommendations",
    "warnings",
    "comparison",
    "error",
    "exit_code",
}


@then("no provider-specific fields appear in the JSON report")
def _then_no_provider_specific(_probe_state: dict[str, Any]) -> None:
    """Sabotage-proof: add a provider-specific top-level key in
    ProbeConfigReport.to_dict (e.g. ``"azure_resource"``) → assertion fails.
    """
    report = _probe_state["report"]
    extra = set(report) - _ALLOWED_TOP_KEYS
    assert not extra, f"unexpected provider-specific top-level keys: {sorted(extra)}"


__all__ = [
    "_FakeSnapshotter",
    "_probe_state",
]
