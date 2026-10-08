"""Single-shot mode — every case runs once, sequentially, in suite order.

Matches the historical ``kairix benchmark run`` behaviour: every case
in the gold suite gets scored exactly once. No thread pool, no
sampling, no warm-up. Intentionally the smallest of the three mode
dispatchers — ~50 LoC, no probe/soak dependency. Ships first per
C2 §4.

Latency phase convention. The first query reports
``latency_phase="cold"`` (no warm-up); every subsequent query reports
``latency_phase="warm"``. The P2 perf-lens scorers consume these
labels to apply the cold + warm gate thresholds independently.
"""

from __future__ import annotations

import time
from dataclasses import replace

from kairix.quality.benchmark.modes.types import (
    ModeRunRequest,
    ModeRunResult,
    QueryRunResult,
)
from kairix.quality.probe.runner import SampledQuery
from kairix.quality.redaction import describe_exception


def _to_sampled_query(case: object) -> SampledQuery:
    """Project a ``BenchmarkCase`` onto the ``SampledQuery`` shape.

    Same conversion the probe runner does internally — both surfaces
    consume the same case fields (``id``, ``category``, ``query``,
    optional ``agent``). Kept as a private helper so the dispatcher
    body stays declarative.
    """
    return SampledQuery(
        case_id=case.id,
        category=case.category,
        query=case.query,
        agent=getattr(case, "agent", None),
    )


def _label_phase(result: QueryRunResult, *, is_first: bool) -> QueryRunResult:
    """Apply the cold/warm label per the single-shot phase convention.

    The executor produces a ``QueryRunResult`` without phase context —
    only the dispatcher knows which call is the first. Re-emits a new
    frozen record with the phase tag set; the original is discarded.
    """
    phase = "cold" if is_first else "warm"
    return replace(result, latency_phase=phase)


def _run_one(
    case: object,
    request: ModeRunRequest,
    *,
    is_first: bool,
) -> tuple[QueryRunResult, str]:
    """Execute one case end-to-end and capture latency + phase tag.

    The executor's contract is "MUST NOT raise"; defence-in-depth here
    captures any escaped exception as a ``QueryRunResult`` with
    populated ``error``. Success is implied when ``error is None``.
    Returns ``(query_result, error_string_or_empty)`` so the caller can
    build the aggregate error tuple without re-scanning the result list.
    """
    sampled = _to_sampled_query(case)
    t_start = time.perf_counter()
    try:
        outcome = request.query_executor(sampled)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        err_text = f"query {describe_exception(exc)}"
        labelled = QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=elapsed_ms,
            error=err_text,
            latency_phase=("cold" if is_first else "warm"),
        )
        return labelled, err_text
    labelled = _label_phase(outcome, is_first=is_first)
    return labelled, ("" if labelled.error is None else labelled.error)


def run_single_shot(request: ModeRunRequest) -> ModeRunResult:
    """Run every case in ``request.suite.cases`` once, in suite order.

    Per C2 §4, single-shot composes nothing — pure orchestration:
    iterate, time, label, accumulate. Returns a ``ModeRunResult`` whose
    ``mode_metrics`` carry only the aggregate-time-and-error counters
    that single-shot can compute without sampling or thread-pool stats.

    Empty suite yields an empty ``per_query_runs`` tuple and zeroed
    metrics — no error.
    """
    wall_start = time.perf_counter()
    per_query: list[QueryRunResult] = []
    errors: list[str] = []
    for idx, case in enumerate(request.suite.cases):
        outcome, err = _run_one(case, request, is_first=(idx == 0))
        per_query.append(outcome)
        if err:
            errors.append(f"[{outcome.query_id}] {err}")
    wallclock_s = time.perf_counter() - wall_start
    n = len(per_query)
    mean_latency_ms = (sum(r.latency_ms for r in per_query) / n) if n else 0.0
    return ModeRunResult(
        per_query_runs=tuple(per_query),
        mode_metrics={
            "wallclock_s": round(wallclock_s, 4),
            "mean_latency_ms": round(mean_latency_ms, 3),
            "errors": float(len(errors)),
            "n": float(n),
        },
        errors=tuple(errors),
        raw={},
    )
