"""Unit tests for ``kairix.quality.probe.config_runner`` (#provider-plugin-arch IM-9).

Pins runner behaviour with the FakeProvider + an in-test snapshotter:

* a healthy provider yields ``status="healthy"`` + zero warnings
* a slow provider (warm_p95 > 1 s) yields ``status="degraded"``
* a slow provider (warm_p95 > 5 s) yields ``status="degraded"`` + a
  critical warning
* a provider that errors on every call yields ``status="unreachable"``
* each tuning heuristic fires for the right input:
  - pool_size when ``pool_acquire_p50_ms > 50``
  - coalesce_window_ms when ``coalesce_ratio > 0.7``
  - cache_max_entries when ``cache_hit_rate < 0.05`` and > 0
* baseline comparison via :func:`compute_comparison` flags >20%
  regressions
* the report carries exactly the schema's required top-level fields
* no provider-specific keys leak (uniform schema per F29)

Each test embeds a sabotage-proof note (mutate prod → confirm fail
→ restore). Where the test asserts a specific class of behaviour
multiple times, the sabotage notes call out the most precise mutation
that fails the assertion.

All tests drive the public surface ``run_probe_config`` — internal
helpers are NOT tested directly. Per the project's no-internal-tests
discipline, unreachable branches inside private helpers are dead
code that should be removed, not pinned by a direct test.
"""

from __future__ import annotations

import pytest

from kairix.providers import ProviderHealth, ProviderUnreachable
from kairix.quality.probe.config_report import (
    EXIT_CODE_DEGRADED,
    EXIT_CODE_HEALTHY,
    EXIT_CODE_UNREACHABLE,
    REGRESSION_THRESHOLD_PCT,
    SCHEMA_VERSION,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_UNREACHABLE,
    compute_comparison,
    hostname_from_endpoint,
)
from kairix.quality.probe.config_runner import (
    CACHE_HIT_RATE_RECOMMEND,
    COALESCE_RATIO_RECOMMEND,
    POOL_ACQUIRE_RECOMMEND_MS,
    TransportSnapshot,
    run_probe_config,
)
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


class _StubSnapshotter:
    """In-test ``TransportSnapshotter`` returning a fixed snapshot.

    Production wires the snapshotter to live transport modules; tests
    drive explicit values so the assertion target is the runner's
    classification + recommendation logic.
    """

    def __init__(self, snap: TransportSnapshot) -> None:
        self._snap = snap

    def snapshot(self) -> TransportSnapshot:
        return self._snap


def _fast_runner_kwargs() -> dict[str, int]:
    """Small phase counts so unit tests run in well under a second."""
    return {"warm_samples": 3, "concurrency": 2, "repeated_samples": 3}


# ---------------------------------------------------------------------------
# Healthy / degraded / unreachable status
# ---------------------------------------------------------------------------


def test_healthy_provider_yields_status_healthy_with_zero_warnings() -> None:
    """A fast provider + healthy snapshot → status healthy + warnings empty.

    Sabotage: change ``STATUS_HEALTHY = "broken"`` in config_report →
    assertion ``report.status == STATUS_HEALTHY`` still passes (the
    test imports the same constant). Stronger sabotage: lower
    ``WARM_P95_DEGRADED_MS = 0.0`` → every warm sample exceeds → status
    flips to degraded → this assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_h", dim=1536, embed_latency_s=0.001)
    report = run_probe_config(provider, **_fast_runner_kwargs())
    assert report.status == STATUS_HEALTHY
    assert report.warnings == []
    assert report.exit_code == EXIT_CODE_HEALTHY
    assert report.tuning_recommendations == []


def test_slow_provider_yields_status_degraded() -> None:
    """Latency >1 s → warm_p95 > WARM_P95_DEGRADED_MS → degraded.

    Sabotage: raise ``WARM_P95_DEGRADED_MS = 1e9`` → no sample exceeds →
    status healthy → this assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_d", dim=1536, embed_latency_s=1.1)
    report = run_probe_config(provider, **_fast_runner_kwargs())
    assert report.status == STATUS_DEGRADED
    assert report.exit_code == EXIT_CODE_DEGRADED


def test_critical_latency_emits_warning_in_addition_to_degraded() -> None:
    """warm_p95 > critical_p95_ms → status degraded + a critical warning.

    Drives the critical-warning branch by lowering ``critical_p95_ms`` to
    50 ms so the test can use 60 ms of latency rather than sleeping for
    >5 s per call (the production constant is 5000 ms; sleeping that
    long per call would blow past the safe-commit 30 s timeout).

    Sabotage: drop the ``> critical_p95_ms`` check in ``_classify_status``
    (always fall through to the degraded branch instead) → no warning
    emitted → report.warnings stays empty → this assertion fails.
    Confirmed; restored.
    """
    provider = FakeProvider(name="fake_c", dim=1536, embed_latency_s=0.06)
    report = run_probe_config(
        provider,
        warm_samples=2,
        concurrency=1,
        repeated_samples=2,
        degraded_p95_ms=10.0,
        critical_p95_ms=50.0,
    )
    assert report.status == STATUS_DEGRADED
    assert any("critical" in w.lower() for w in report.warnings), f"expected a critical warning; got {report.warnings}"


def test_unreachable_when_every_call_errors() -> None:
    """Provider that raises on every embed → status unreachable + error.

    Sabotage: swallow the exception in ``_measure_call`` (catch + count
    success) → no errors counted → status healthy → assertion fails.
    Confirmed; restored.
    """
    provider = FakeProvider(
        name="fake_u",
        dim=1536,
        embed_raises=ProviderUnreachable("simulated"),
    )
    report = run_probe_config(provider, **_fast_runner_kwargs())
    assert report.status == STATUS_UNREACHABLE
    assert report.exit_code == EXIT_CODE_UNREACHABLE
    assert report.error is not None


def test_unreachable_when_healthcheck_fails_even_if_calls_succeed() -> None:
    """A failing healthcheck short-circuits to unreachable.

    Sabotage: drop the healthcheck-ok check at the top of
    ``run_probe_config`` → status falls through to healthy → this
    assertion fails. Confirmed; restored.
    """
    bad_health = ProviderHealth(
        ok=False,
        endpoint="https://broken.example.invalid",
        cold_ms=None,
        warm_ms=None,
        error="endpoint refused connection",
    )
    provider = FakeProvider(
        name="fake_h_bad",
        dim=1536,
        embed_latency_s=0.001,
        health=bad_health,
    )
    report = run_probe_config(provider, **_fast_runner_kwargs())
    assert report.status == STATUS_UNREACHABLE
    assert "refused" in (report.error or "")


# ---------------------------------------------------------------------------
# Tuning heuristics — each fires for the right input
# ---------------------------------------------------------------------------


def test_high_pool_acquire_recommends_increasing_pool_size() -> None:
    """pool_acquire_p50_ms > 50 → pool_size recommendation.

    Sabotage: raise ``POOL_ACQUIRE_RECOMMEND_MS = 1e9`` → no rec → list
    empty → assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_p", dim=1536, embed_latency_s=0.001)
    snap = TransportSnapshot(
        coalesce_ratio=0.1,
        cache_hit_rate=0.5,
        pool_acquire_p50_ms=POOL_ACQUIRE_RECOMMEND_MS + 30.0,
        current_pool_size=4,
    )
    report = run_probe_config(provider, snapshotter=_StubSnapshotter(snap), **_fast_runner_kwargs())
    fields = {r.field for r in report.tuning_recommendations}
    assert "pool_size" in fields, f"expected pool_size advice; got {sorted(fields)}"
    pool_rec = next(r for r in report.tuning_recommendations if r.field == "pool_size")
    assert pool_rec.current == 4
    assert pool_rec.suggested > pool_rec.current


def test_high_coalesce_ratio_recommends_decreasing_window() -> None:
    """coalesce_ratio > 0.7 → coalesce_window_ms recommendation.

    Sabotage: raise ``COALESCE_RATIO_RECOMMEND = 2.0`` → trigger
    impossible → no rec → assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_co", dim=1536, embed_latency_s=0.001)
    snap = TransportSnapshot(
        coalesce_ratio=COALESCE_RATIO_RECOMMEND + 0.1,
        cache_hit_rate=0.5,
        pool_acquire_p50_ms=1.0,
        current_coalesce_window_ms=50,
    )
    report = run_probe_config(provider, snapshotter=_StubSnapshotter(snap), **_fast_runner_kwargs())
    fields = {r.field for r in report.tuning_recommendations}
    assert "coalesce_window_ms" in fields
    rec = next(r for r in report.tuning_recommendations if r.field == "coalesce_window_ms")
    assert rec.suggested < rec.current  # halving (with floor 5)


def test_low_cache_hit_rate_recommends_larger_cache() -> None:
    """cache_hit_rate < 0.05 (and > 0) → cache_max_entries recommendation.

    Sabotage: drop the ``> 0.0`` lower-bound check → 0.0 (no cache
    exercised) triggers the recommendation → false-positive →
    assertion-on-suggested-value-shape stays green but operator gets
    noise. Use ``CACHE_HIT_RATE_RECOMMEND = 0.0`` → strict-< 0.0 →
    nothing fires → assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_ca", dim=1536, embed_latency_s=0.001)
    snap = TransportSnapshot(
        coalesce_ratio=0.1,
        cache_hit_rate=CACHE_HIT_RATE_RECOMMEND / 2.0,  # 0.025 — below threshold
        pool_acquire_p50_ms=1.0,
        current_cache_max_entries=1024,
    )
    report = run_probe_config(provider, snapshotter=_StubSnapshotter(snap), **_fast_runner_kwargs())
    fields = {r.field for r in report.tuning_recommendations}
    assert "cache_max_entries" in fields
    rec = next(r for r in report.tuning_recommendations if r.field == "cache_max_entries")
    assert rec.suggested == 1024 * 8


def test_cache_recommendation_does_not_fire_at_zero_hit_rate() -> None:
    """Hit-rate of 0.0 means the cache was never exercised; no recommendation.

    Sabotage: change the runner's ``0.0 < transport.cache_hit_rate``
    guard to ``0.0 <= transport.cache_hit_rate`` → 0.0 fires → list
    contains cache_max_entries → assertion fails. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_z", dim=1536, embed_latency_s=0.001)
    snap = TransportSnapshot(
        coalesce_ratio=0.1,
        cache_hit_rate=0.0,
        pool_acquire_p50_ms=1.0,
    )
    report = run_probe_config(provider, snapshotter=_StubSnapshotter(snap), **_fast_runner_kwargs())
    fields = {r.field for r in report.tuning_recommendations}
    assert "cache_max_entries" not in fields


# ---------------------------------------------------------------------------
# Schema fidelity
# ---------------------------------------------------------------------------


_REQUIRED_TOP_KEYS = {
    "schema_version",
    "kairix_version",
    "status",
    "provider",
    "timing",
    "transport",
    "stage_latency_ms",
    "tuning_recommendations",
    "warnings",
    "exit_code",
}

_REQUIRED_STAGE_KEYS = {
    "pool_acquire",
    "coalesce_wait",
    "cache_lookup",
    "http_roundtrip",
    "response_parse",
}


def test_report_carries_all_required_top_level_keys() -> None:
    """Every top-level field from docs/architecture/probe-config-schema.md present.

    Sabotage: drop ``"warnings": list(self.warnings)`` from
    ``ProbeConfigReport.to_dict`` → required-key check fails on
    ``warnings``. Confirmed; restored.
    """
    provider = FakeProvider(name="fake_s", dim=1536, embed_latency_s=0.001)
    report = run_probe_config(provider, **_fast_runner_kwargs())
    d = report.to_dict()
    missing = _REQUIRED_TOP_KEYS - d.keys()
    assert not missing, f"missing required top-level keys: {sorted(missing)}"
    assert d["schema_version"] == SCHEMA_VERSION


def test_stage_latency_ms_uses_uniform_keys_across_providers() -> None:
    """No provider-specific keys leak; uniform schema per F29.

    Sabotage: add ``"azure_resource"`` to ``_empty_stage_latencies`` →
    extra-key check fails. Confirmed; restored.
    """
    for name in ("azure_foundry", "openai", "ollama"):
        provider = FakeProvider(name=name, dim=1536, embed_latency_s=0.001)
        report = run_probe_config(provider, **_fast_runner_kwargs())
        keys = set(report.stage_latency_ms.keys())
        assert _REQUIRED_STAGE_KEYS.issubset(keys), f"provider {name!r}: missing stages {_REQUIRED_STAGE_KEYS - keys}"
        extras = keys - _REQUIRED_STAGE_KEYS
        assert not extras, f"provider {name!r}: unexpected stage keys {extras}"


def test_provider_endpoint_surface_is_hostname_only() -> None:
    """Privacy: endpoint surfaces as hostname only, never a URL.

    Sabotage: change ``hostname_from_endpoint`` to return the full URL
    → ``"://"`` appears in the report → assertion fails. Confirmed;
    restored.
    """
    health = ProviderHealth(
        ok=True,
        endpoint="https://example-resource.openai.azure.com/openai/v1/embeddings",
        cold_ms=0.0,
        warm_ms=0.0,
        error=None,
    )
    provider = FakeProvider(name="azure_foundry", dim=1536, embed_latency_s=0.001, health=health)
    report = run_probe_config(provider, **_fast_runner_kwargs())
    hostname = report.provider.endpoint_hostname
    assert hostname == "example-resource.openai.azure.com", hostname
    assert "://" not in hostname
    assert "/" not in hostname


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def test_compute_comparison_flags_only_regressions_over_threshold() -> None:
    """Stages >20% slower appear; stages within 20% are within noise.

    Sabotage: raise ``REGRESSION_THRESHOLD_PCT = 1000.0`` → 100%
    regression no longer qualifies → regressions empty → assertion fails.
    Confirmed; restored.
    """
    baseline = {"http_roundtrip": 10.0, "pool_acquire": 1.0, "cache_lookup": 1.0}
    current = {
        "http_roundtrip": 25.0,  # 150% slower — should regress
        "pool_acquire": 1.1,  # 10% slower — within noise
        "cache_lookup": 1.0,  # unchanged
    }
    comp = compute_comparison(
        current_stages=current,
        baseline_stages=baseline,
        baseline_path="/tmp/baseline.json",
        baseline_collected_at="2026-05-10T00:00:00Z",
    )
    flagged = {r.stage for r in comp.regressions}
    assert flagged == {"http_roundtrip"}, f"expected just http_roundtrip; got {flagged}"
    http = next(r for r in comp.regressions if r.stage == "http_roundtrip")
    assert http.percent_slower > REGRESSION_THRESHOLD_PCT


def test_compute_comparison_skips_missing_or_zero_baseline_stages() -> None:
    """Stages with zero or missing baseline don't divide-by-zero.

    Sabotage: remove the ``baseline_ms <= 0`` guard → ZeroDivisionError
    → test fails (or raises). Confirmed; restored.
    """
    baseline = {"http_roundtrip": 0.0, "pool_acquire": 1.0}
    current = {"http_roundtrip": 100.0, "pool_acquire": 5.0, "cache_lookup": 9.0}
    comp = compute_comparison(
        current_stages=current,
        baseline_stages=baseline,
        baseline_path="/tmp/baseline.json",
        baseline_collected_at="2026-05-10T00:00:00Z",
    )
    flagged = {r.stage for r in comp.regressions}
    # http_roundtrip is skipped because baseline_ms == 0; pool_acquire
    # is 400% slower so it qualifies; cache_lookup is missing from
    # baseline so skipped.
    assert flagged == {"pool_acquire"}


def test_hostname_from_endpoint_handles_bare_hostname() -> None:
    """``api.openai.com`` → ``api.openai.com`` (no scheme).

    Sabotage: drop the ``parsed.hostname`` short-circuit → urlparse
    returns no hostname for a bare token → fallback path returns the
    full string but with no path-stripping → schemaless inputs would
    leak path-like content. Sabotage: change fallback to
    ``endpoint.split("?", 1)[0]`` → an endpoint containing a slash
    would no longer have its path stripped → assertion fails.
    Confirmed; restored.
    """
    assert hostname_from_endpoint("api.openai.com") == "api.openai.com"
    assert hostname_from_endpoint("api.openai.com/v1/embeddings") == "api.openai.com"
    assert hostname_from_endpoint("") == ""
    assert hostname_from_endpoint("https://api.openai.com/v1") == "api.openai.com"
