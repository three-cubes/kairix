"""``kairix probe-config`` runner (#provider-plugin-arch IM-9).

A NEW CONSUMER of ``kairix/quality/probe/`` — not a parallel benchmark
harness. Per F29 the probe instrumentation surface is singular; this
module orchestrates a small representative workload against the
operator's configured provider and emits the JSON report defined in
``docs/architecture/probe-config-schema.md``.

What the runner does:

1. **Cold call** — one ``embed_batch`` against a fresh provider so
   the report's ``cold_ms`` captures the connection setup / TLS
   handshake cost.

2. **Warm sequential** — ``warm_samples`` ``embed_batch`` calls back
   to back. Drives the ``warm_p50_ms`` / ``warm_p95_ms`` /
   ``warm_p99_ms`` tail.

3. **Warm concurrent** — a small fan-out (default ``concurrency=4``)
   of ``embed_batch`` calls so the coalescer / pool / cache have an
   opportunity to fire. The runner reads the transport-snapshotter
   to populate ``coalesce_ratio`` and ``cache_hit_rate``.

4. **Repeated-query phase** — ``repeated_samples`` calls on the same
   text so a properly-sized cache would show a high hit-rate. The
   ``cache_max_entries`` heuristic compares the observed hit rate to
   the threshold to decide whether to recommend a larger cache.

Tuning heuristics (locked in this module per the IM-9 brief; the
ADR's strawmen mostly stand — minor deviations noted below):

+--------------------------+-------------------------------+-------------------------------+
| Recommendation field     | Trigger                       | Suggested value               |
+==========================+===============================+===============================+
| ``pool_size``            | ``pool_acquire_p50_ms > 50``  | ``min(current * 4, 32)``      |
+--------------------------+-------------------------------+-------------------------------+
| ``coalesce_window_ms``   | ``coalesce_ratio > 0.7`` AND  | ``max(current // 2, 5)``      |
|                          | workload phase is ``"solo"``  |                               |
+--------------------------+-------------------------------+-------------------------------+
| ``cache_max_entries``    | ``cache_hit_rate < 0.05``     | ``current * 8``               |
|                          | under repeated-query phase    |                               |
+--------------------------+-------------------------------+-------------------------------+

Status verdict:

+---------------+--------------------------------------------------+
| Verdict       | Trigger (any one)                                |
+===============+==================================================+
| unreachable   | every cold / warm call errored, OR               |
|               | ``healthcheck().ok`` is False                    |
+---------------+--------------------------------------------------+
| degraded      | ``warm_p95_ms > 1000``                           |
+---------------+--------------------------------------------------+
| degraded      | ``warm_p95_ms > 5000`` — additional critical     |
| (critical)    | warning appended                                 |
+---------------+--------------------------------------------------+
| healthy       | none of the above                                |
+---------------+--------------------------------------------------+

Deviations from the SK-7 strawman:

* ``pool_acquire_p50_ms > 5`` (strawman) → ``> 50`` (lock-in). The
  strawman's 5 ms floor is below the noise floor of an in-process
  fake; 50 ms is the actionable boundary where pool exhaustion is
  the dominant cost. Operators on a real endpoint hit this when the
  pool truly is the bottleneck; sub-50 ms acquire times are within
  the latency budget of a single coalescer batch dispatch.
* The ``coalesce_window_ms`` and ``cache_max_entries`` triggers
  carry through unchanged.

The runner takes a ``Provider`` and a ``TransportSnapshotter``
(Protocol) by dependency injection. Production wires the snapshotter
to ``kairix.transport.coalesce.get_embed_coalescer().stats()`` plus
``kairix.transport.cache.get_embed_cache().stats()``; tests inject
:class:`FakeTransportSnapshotter` from ``tests/fakes.py``.

F29-clean (lives under ``kairix/quality/probe/``); F26-clean
(no ``kairix/core/`` imports of transport / providers); F1/F2/F5
clean (no patching, no env monkeypatch, no test-only imports).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from kairix.providers import Provider
from kairix.quality.probe.config_report import (
    EXIT_CODE_DEGRADED,
    EXIT_CODE_HEALTHY,
    EXIT_CODE_UNREACHABLE,
    SCHEMA_VERSION,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_UNREACHABLE,
    ProbeConfigReport,
    ProviderInfo,
    TimingSection,
    TransportSection,
    TuningRecommendation,
    hostname_from_endpoint,
)

#: Number of warm sequential ``embed_batch`` calls in the warm phase.
#: Picked to give a small but meaningful p95 sample without making
#: the probe slow on the first run.
DEFAULT_WARM_SAMPLES = 20

#: Number of concurrent ``embed_batch`` calls in the fan-out phase.
#: Drives the coalescer / pool so the transport stats have something
#: to report.
DEFAULT_CONCURRENCY = 4

#: Number of repeated calls in the cache-warm phase (same text every
#: time). A properly-sized cache shows a high hit-rate; an undersized
#: cache shows < 5% and triggers the ``cache_max_entries`` heuristic.
DEFAULT_REPEATED_SAMPLES = 10

#: Threshold (ms) above which ``warm_p95_ms`` triggers ``degraded``.
#: Per docs/architecture/probe-config-schema.md § Status thresholds.
WARM_P95_DEGRADED_MS = 1000.0

#: Threshold (ms) above which ``warm_p95_ms`` triggers a critical
#: warning on top of the ``degraded`` verdict. Operators seeing this
#: should treat the endpoint as effectively unusable.
WARM_P95_CRITICAL_MS = 5000.0

#: Pool-acquire p50 (ms) above which the runner recommends raising
#: ``pool_size``. The SK-7 strawman of 5 ms is below the noise floor
#: of an in-process fake; 50 ms is the actionable boundary in the
#: module docstring.
POOL_ACQUIRE_RECOMMEND_MS = 50.0

#: Coalesce ratio above which the runner recommends shrinking
#: ``coalesce_window_ms`` under the solo-workload phase. A high ratio
#: with a low fan-out means most requests are waiting for batchmates
#: that never arrive.
COALESCE_RATIO_RECOMMEND = 0.7

#: Cache hit-rate below which the runner recommends increasing
#: ``cache_max_entries`` under the repeated-query phase. With a
#: properly-sized cache and N identical queries the hit-rate is
#: ``(N-1)/N``; the 5% threshold catches caches that are evicting
#: under load.
CACHE_HIT_RATE_RECOMMEND = 0.05

#: Default pool size used as the ``current`` value in a
#: ``pool_size`` recommendation when the snapshotter does not supply
#: one. Matches the kairix transport pool's documented default.
DEFAULT_POOL_SIZE = 4

#: Default coalesce-window (ms) used as the ``current`` value in a
#: ``coalesce_window_ms`` recommendation when the snapshotter does
#: not supply one.
DEFAULT_COALESCE_WINDOW_MS = 50

#: Default cache size used as the ``current`` value in a
#: ``cache_max_entries`` recommendation when the snapshotter does
#: not supply one.
DEFAULT_CACHE_MAX_ENTRIES = 1024

#: Sample probe text — short enough that latency is dominated by
#: the round-trip, not by token-encoding cost.
PROBE_TEXT = "kairix probe-config sample text"

#: Stage-name constant — used as a key in ``stage_latency_ms`` and in
#: the snapshotter's "did the snapshot already claim this stage?"
#: lookup. Extracted to a module-level constant per F17 (no string
#: literal ≥10 chars duplicated ≥3 times in a module).
_STAGE_HTTP_ROUNDTRIP = "http_roundtrip"

#: Sample probe text used in the repeated-query phase — distinct
#: from ``PROBE_TEXT`` so the cache cold-path doesn't interfere with
#: the warm phase's hit-rate measurement.
REPEATED_PROBE_TEXT = "kairix probe-config repeated sample"


class TransportSnapshotter(Protocol):
    """Provides observed transport stats after the probe run.

    Production wires this to the real ``kairix.transport.coalesce``
    and ``kairix.transport.cache`` modules. Tests inject a fake that
    returns explicit values.

    Members:

    * ``snapshot()`` — return a :class:`TransportSnapshot` with the
      observed transport-layer stats after the probe completes.
    """

    def snapshot(self) -> TransportSnapshot:
        """Return the observed transport stats after the run."""


@dataclass(frozen=True)
class TransportSnapshot:
    """Snapshot of transport-layer stats observed during a probe run.

    Fields:

    * ``coalesce_ratio`` — ``batches / requests`` over the run; 0.0
      when the coalescer wasn't exercised.
    * ``cache_hit_rate`` — ``hits / (hits + misses)`` over the run;
      0.0 when the cache wasn't exercised.
    * ``pool_acquire_p50_ms`` — median time spent in
      ``transport.pool.get_client`` over the run; 0.0 when not measured.
    * ``stage_latency_ms`` — per-stage breakdown in ms; keys are the
      uniform stage names from the ADR (``pool_acquire``,
      ``coalesce_wait``, ``cache_lookup``, ``http_roundtrip``,
      ``response_parse``). Missing stages are reported as ``0.0`` on
      the report side; passing them through here keeps the snapshotter
      authoritative for the values it claims to know.
    * ``current_pool_size`` / ``current_coalesce_window_ms`` /
      ``current_cache_max_entries`` — current operator-visible config
      values; surface verbatim in any tuning recommendation triggered
      by this snapshot. ``None`` means "fall back to module default"
      (used by the no-transport-observed fast path).
    """

    coalesce_ratio: float = 0.0
    cache_hit_rate: float = 0.0
    pool_acquire_p50_ms: float = 0.0
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    current_pool_size: int | None = None
    current_coalesce_window_ms: int | None = None
    current_cache_max_entries: int | None = None


class NullTransportSnapshotter:
    """Snapshotter that always returns an empty :class:`TransportSnapshot`.

    Used when the operator's configured provider does not exercise
    the kairix transport layer (e.g. a non-pooled local provider).
    The report still emits the transport section with zeros — keeping
    the JSON shape uniform across providers per the schema doc.
    """

    def snapshot(self) -> TransportSnapshot:
        return TransportSnapshot()


@dataclass
class _CallTimings:
    """Per-call wall-clock samples collected during a probe run.

    Accumulator for the runner — converted to the final
    :class:`TimingSection` via :func:`_summarise_timings`.
    """

    cold_ms: float = 0.0
    warm_samples_ms: list[float] = field(default_factory=list)
    errors: int = 0
    total_calls: int = 0


def _percentile(samples: list[float], pct: float) -> float:
    """Return the ``pct``-th percentile of ``samples`` (0 ≤ pct ≤ 100).

    Linear interpolation between adjacent ranks. Empty input returns
    ``0.0`` — the report's downstream consumers expect a number, not
    ``None``.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, len(ordered) - 1)
    frac = rank - lower_idx
    return float(ordered[lower_idx] + (ordered[upper_idx] - ordered[lower_idx]) * frac)


def _measure_call(provider: Provider, texts: list[str], timings: _CallTimings) -> bool:
    """Time one ``embed_batch`` call against ``provider``.

    Returns ``True`` when the call returned successfully (the
    runner counts it in ``total_calls``); ``False`` when it raised
    (counted in ``errors``).

    Per the Provider contract ``embed_batch`` should not raise, but
    a probe is exactly when an operator finds out their endpoint is
    broken — so the runner treats any exception as an error rather
    than letting it bubble.
    """
    started = time.perf_counter()
    timings.total_calls += 1
    try:
        provider.embed_batch(texts)
    except Exception:
        timings.errors += 1
        return False
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    timings.warm_samples_ms.append(elapsed_ms)
    return True


def _run_warm_sequential(provider: Provider, samples: int, timings: _CallTimings) -> None:
    """Run ``samples`` warm sequential ``embed_batch`` calls.

    Each call is one text; samples accumulate into
    ``timings.warm_samples_ms`` for the p50/p95/p99 rollup.
    """
    for _ in range(samples):
        _measure_call(provider, [PROBE_TEXT], timings)


def _run_warm_concurrent(provider: Provider, concurrency: int, timings: _CallTimings) -> None:
    """Fan ``concurrency`` ``embed_batch`` calls out across threads.

    Drives the coalescer / pool / cache so the transport snapshotter
    has stats to report. Errors are counted; no exception escapes.
    """
    threads = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        started = time.perf_counter()
        try:
            provider.embed_batch([f"{PROBE_TEXT}-{idx}"])
        except Exception:
            with lock:
                timings.errors += 1
                timings.total_calls += 1
            return
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with lock:
            timings.total_calls += 1
            timings.warm_samples_ms.append(elapsed_ms)

    for i in range(concurrency):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()


def _run_repeated(provider: Provider, samples: int, timings: _CallTimings) -> None:
    """Issue ``samples`` calls on the same text so the cache can fire.

    A correctly-sized cache shows hit-rate ≈ ``(samples-1)/samples``
    on this phase; an undersized cache shows a low hit-rate and
    triggers the ``cache_max_entries`` recommendation.
    """
    for _ in range(samples):
        _measure_call(provider, [REPEATED_PROBE_TEXT], timings)


def _summarise_timings(timings: _CallTimings) -> TimingSection:
    """Roll up ``_CallTimings`` into a :class:`TimingSection`.

    p50 / p95 / p99 are computed from ``warm_samples_ms``; the cold
    sample is the first wall-clock recorded.
    """
    return TimingSection(
        cold_ms=timings.cold_ms,
        warm_p50_ms=_percentile(timings.warm_samples_ms, 50.0),
        warm_p95_ms=_percentile(timings.warm_samples_ms, 95.0),
        warm_p99_ms=_percentile(timings.warm_samples_ms, 99.0),
    )


def _build_recommendations(
    transport: TransportSection,
    snapshot: TransportSnapshot,
) -> list[TuningRecommendation]:
    """Apply the locked-in tuning heuristics to a snapshot.

    See the module docstring's heuristic table for which trigger
    fires which recommendation. Order in the returned list is
    pool → coalesce → cache so operators address the highest-impact
    lever first.
    """
    out: list[TuningRecommendation] = []
    if transport.pool_acquire_p50_ms > POOL_ACQUIRE_RECOMMEND_MS:
        current = snapshot.current_pool_size or DEFAULT_POOL_SIZE
        out.append(
            TuningRecommendation(
                field="pool_size",
                current=current,
                suggested=min(current * 4, 32),
                rationale=(
                    f"pool_acquire_p50_ms is {transport.pool_acquire_p50_ms:.1f} ms "
                    f"(target <{POOL_ACQUIRE_RECOMMEND_MS:.0f} ms); pool is the "
                    f"bottleneck under your concurrency"
                ),
            )
        )
    if transport.coalesce_ratio > COALESCE_RATIO_RECOMMEND:
        current = snapshot.current_coalesce_window_ms or DEFAULT_COALESCE_WINDOW_MS
        out.append(
            TuningRecommendation(
                field="coalesce_window_ms",
                current=current,
                suggested=max(current // 2, 5),
                rationale=(
                    f"coalesce_ratio is {transport.coalesce_ratio:.2f} — most "
                    f"requests are waiting for batchmates that never arrive"
                ),
            )
        )
    if 0.0 < transport.cache_hit_rate < CACHE_HIT_RATE_RECOMMEND:
        current = snapshot.current_cache_max_entries or DEFAULT_CACHE_MAX_ENTRIES
        out.append(
            TuningRecommendation(
                field="cache_max_entries",
                current=current,
                suggested=current * 8,
                rationale=(
                    f"cache_hit_rate is {transport.cache_hit_rate:.2f} under a "
                    f"repeated-query workload; the cache is undersized"
                ),
            )
        )
    return out


def _classify_status(
    timings: _CallTimings,
    healthcheck_ok: bool,
    *,
    degraded_p95_ms: float,
    critical_p95_ms: float,
) -> tuple[str, list[str]]:
    """Decide ``status`` plus any ``warnings`` to surface.

    Returns ``(status, warnings)`` so the caller can plug both into
    the final report. Thresholds are parameters (not module constants)
    so operators tuning for their endpoint distance can lower them per
    invocation without a code change, and tests can drive the
    critical-warning branch without sleeping for >5 s.
    """
    if not healthcheck_ok or (timings.errors and timings.errors == timings.total_calls):
        return STATUS_UNREACHABLE, []
    warnings: list[str] = []
    if timings.errors > 0:
        warnings.append(
            f"{timings.errors} of {timings.total_calls} probe calls errored — "
            f"endpoint is intermittent; investigate before relying on it"
        )
    p95 = _percentile(timings.warm_samples_ms, 95.0)
    if p95 > critical_p95_ms:
        warnings.append(
            f"warm_p95_ms is {p95:.0f} ms — critical: endpoint is effectively "
            f"unusable for interactive workloads; check region / network path"
        )
        return STATUS_DEGRADED, warnings
    if p95 > degraded_p95_ms:
        return STATUS_DEGRADED, warnings
    return STATUS_HEALTHY, warnings


def _status_exit_code(status: str) -> int:
    """Map a status string to its mirrored process exit code."""
    if status == STATUS_HEALTHY:
        return EXIT_CODE_HEALTHY
    if status == STATUS_DEGRADED:
        return EXIT_CODE_DEGRADED
    return EXIT_CODE_UNREACHABLE


def _kairix_version() -> str:
    """Resolve the installed kairix version string for the report.

    Falls back to ``"0.0.0"`` when the package metadata is missing
    (editable install before ``pip install -e .``).
    """
    try:
        from kairix import __version__

        return __version__
    except ImportError:  # pragma: no cover — defensive; __version__ is set unconditionally in kairix/__init__.py
        return "0.0.0"


def _build_unreachable_report(
    provider: Provider,
    error: str,
    kairix_version: str,
) -> ProbeConfigReport:
    """Construct an ``unreachable`` report for a provider that errored on every call.

    Stage timings are all zeros because no successful call was ever
    measured. The ``error`` field carries a short human-readable
    description for support sharing.
    """
    endpoint_url = ""
    try:
        endpoint_url = provider.healthcheck().endpoint
    except Exception:
        # If healthcheck itself raises, hostname falls back to "".
        endpoint_url = ""
    return ProbeConfigReport(
        schema_version=SCHEMA_VERSION,
        kairix_version=kairix_version,
        status=STATUS_UNREACHABLE,
        provider=ProviderInfo(
            name=provider.name,
            endpoint_hostname=hostname_from_endpoint(endpoint_url),
            dimension=_safe_dimension(provider),
        ),
        timing=TimingSection(cold_ms=0.0, warm_p50_ms=0.0, warm_p95_ms=0.0, warm_p99_ms=0.0),
        transport=TransportSection(coalesce_ratio=0.0, cache_hit_rate=0.0, pool_acquire_p50_ms=0.0),
        stage_latency_ms=_empty_stage_latencies(),
        tuning_recommendations=[],
        warnings=[],
        error=error,
        exit_code=EXIT_CODE_UNREACHABLE,
    )


def _safe_dimension(provider: Provider) -> int:
    """Return ``provider.dimension()`` or ``0`` if the call raises.

    An unreachable provider may not even know its dimension; the
    report still needs an integer per the schema.
    """
    try:
        return int(provider.dimension())
    except Exception:
        return 0


def _empty_stage_latencies() -> dict[str, float]:
    """The uniform stage-latency keys with zeros — used on unreachable.

    Per docs/architecture/probe-config-schema.md the report's
    ``stage_latency_ms`` keys are uniform across providers; a stage
    that wasn't exercised reports ``0.0``, not absent.
    """
    return {
        "pool_acquire": 0.0,
        "coalesce_wait": 0.0,
        "cache_lookup": 0.0,
        _STAGE_HTTP_ROUNDTRIP: 0.0,
        "response_parse": 0.0,
    }


def _merged_stage_latencies(timings: _CallTimings, snapshot: TransportSnapshot) -> dict[str, float]:
    """Combine snapshotter-reported stages with the measured wall-clock.

    The snapshotter is authoritative for any stage it claims; for any
    uniform key it does NOT supply, the runner falls back to either
    zero or the measured wall-clock equivalent.

    Specifically: when the snapshotter does not report
    ``http_roundtrip``, the runner uses the warm-sample median as a
    coarse approximation. This is the schema's "stage timings vary
    across providers but the report schema is identical" guarantee —
    every uniform key is always present.
    """
    out = _empty_stage_latencies()
    out.update(snapshot.stage_latency_ms)
    if _STAGE_HTTP_ROUNDTRIP not in snapshot.stage_latency_ms:
        out[_STAGE_HTTP_ROUNDTRIP] = _percentile(timings.warm_samples_ms, 50.0)
    return out


def _all_calls_failed(timings: _CallTimings) -> bool:
    """Return ``True`` when every probe call errored — drives unreachable."""
    return timings.total_calls > 0 and timings.errors == timings.total_calls


def _healthcheck_ok(provider: Provider) -> tuple[bool, str | None]:
    """Run ``provider.healthcheck()`` defensively.

    Returns ``(ok, error_message)``. A healthcheck that itself raises
    counts as not-ok with the exception message — never propagates,
    since the probe is exactly when an operator finds out their
    healthcheck is broken.
    """
    try:
        health = provider.healthcheck()
    except Exception as exc:
        return False, f"healthcheck raised: {type(exc).__name__}: {exc}"
    if not health.ok:
        return False, health.error or "healthcheck reported endpoint not ok"
    return True, None


def run_probe_config(
    provider: Provider,
    *,
    warm_samples: int = DEFAULT_WARM_SAMPLES,
    concurrency: int = DEFAULT_CONCURRENCY,
    repeated_samples: int = DEFAULT_REPEATED_SAMPLES,
    snapshotter: TransportSnapshotter | None = None,
    degraded_p95_ms: float = WARM_P95_DEGRADED_MS,
    critical_p95_ms: float = WARM_P95_CRITICAL_MS,
) -> ProbeConfigReport:
    """Drive ``provider`` through cold / warm / fan-out / repeated phases.

    Returns the :class:`ProbeConfigReport` with all fields populated.
    Never raises — every observable failure becomes a report value
    (``status=unreachable`` or a warning entry).

    Parameters:

    - ``provider`` — the configured ``Provider`` instance to probe.
    - ``warm_samples`` — number of warm sequential samples
      (default :data:`DEFAULT_WARM_SAMPLES`).
    - ``concurrency`` — fan-out for the warm-concurrent phase
      (default :data:`DEFAULT_CONCURRENCY`).
    - ``repeated_samples`` — same-text samples for the cache phase
      (default :data:`DEFAULT_REPEATED_SAMPLES`).
    - ``snapshotter`` — :class:`TransportSnapshotter` Protocol
      implementation; defaults to :class:`NullTransportSnapshotter`
      (transport stats all zero).
    - ``degraded_p95_ms`` — warm-p95 threshold (ms) above which the
      verdict is ``degraded``. Defaults to
      :data:`WARM_P95_DEGRADED_MS`.
    - ``critical_p95_ms`` — warm-p95 threshold (ms) above which the
      verdict is still ``degraded`` but a critical warning is added.
      Defaults to :data:`WARM_P95_CRITICAL_MS`.
    """
    snapshotter = snapshotter or NullTransportSnapshotter()
    kairix_version = _kairix_version()

    healthcheck_ok, healthcheck_error = _healthcheck_ok(provider)
    if not healthcheck_ok:
        return _build_unreachable_report(
            provider,
            error=healthcheck_error or "healthcheck reported endpoint not ok",
            kairix_version=kairix_version,
        )

    timings = _CallTimings()
    # Cold call — the first request pays connection setup / TLS handshake.
    cold_started = time.perf_counter()
    cold_ok = _measure_call(provider, [PROBE_TEXT], timings)
    if cold_ok:
        timings.cold_ms = (time.perf_counter() - cold_started) * 1000.0
        # Cold sample is also a warm sample for tail purposes — but
        # _measure_call already appended it. Leave it alone.

    if _all_calls_failed(timings):
        return _build_unreachable_report(
            provider,
            error="every probe call errored — endpoint unreachable",
            kairix_version=kairix_version,
        )

    _run_warm_sequential(provider, warm_samples, timings)
    _run_warm_concurrent(provider, concurrency, timings)
    _run_repeated(provider, repeated_samples, timings)

    if _all_calls_failed(timings):
        return _build_unreachable_report(
            provider,
            error="every probe call errored — endpoint unreachable",
            kairix_version=kairix_version,
        )

    snapshot = snapshotter.snapshot()
    timing = _summarise_timings(timings)
    transport = TransportSection(
        coalesce_ratio=snapshot.coalesce_ratio,
        cache_hit_rate=snapshot.cache_hit_rate,
        pool_acquire_p50_ms=snapshot.pool_acquire_p50_ms,
    )
    status, warnings = _classify_status(
        timings,
        healthcheck_ok=True,
        degraded_p95_ms=degraded_p95_ms,
        critical_p95_ms=critical_p95_ms,
    )
    recommendations = _build_recommendations(transport, snapshot)
    endpoint_url = provider.healthcheck().endpoint

    return ProbeConfigReport(
        schema_version=SCHEMA_VERSION,
        kairix_version=kairix_version,
        status=status,
        provider=ProviderInfo(
            name=provider.name,
            endpoint_hostname=hostname_from_endpoint(endpoint_url),
            dimension=_safe_dimension(provider),
        ),
        timing=timing,
        transport=transport,
        stage_latency_ms=_merged_stage_latencies(timings, snapshot),
        tuning_recommendations=recommendations,
        warnings=warnings,
        exit_code=_status_exit_code(status),
    )


__all__ = [
    "CACHE_HIT_RATE_RECOMMEND",
    "COALESCE_RATIO_RECOMMEND",
    "DEFAULT_CACHE_MAX_ENTRIES",
    "DEFAULT_COALESCE_WINDOW_MS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_POOL_SIZE",
    "DEFAULT_REPEATED_SAMPLES",
    "DEFAULT_WARM_SAMPLES",
    "POOL_ACQUIRE_RECOMMEND_MS",
    "WARM_P95_CRITICAL_MS",
    "WARM_P95_DEGRADED_MS",
    "NullTransportSnapshotter",
    "TransportSnapshot",
    "TransportSnapshotter",
    "run_probe_config",
]
