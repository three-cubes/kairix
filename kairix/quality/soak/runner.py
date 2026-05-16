"""Soak runner — repeat a benchmark workload N times and check for degradation.

The capability described in `docs/architecture/operational-tests-design.md`.

Assertions:
    - memory growth per iteration < max_memory_growth_mb
    - per-iteration wall time within max_time_drift_pct of first iteration
    - total stderr bytes < max_log_volume_mb * repeat
    - byte-identical SoakIteration.signature across repeats (no state leakage)

Never raises — failures populate SoakResult.failures with structured reasons,
so the CLI can format them and the MCP stub can surface them in an envelope.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Conservative defaults — operator can tighten via CLI flags or kairix.config.yaml.
DEFAULT_MAX_MEMORY_GROWTH_MB = 50.0
DEFAULT_MAX_LOG_VOLUME_MB_PER_REPEAT = 5.0
DEFAULT_MAX_TIME_DRIFT_PCT = 20.0


@dataclass(frozen=True)
class SoakIteration:
    """Per-iteration measurements captured during a soak run."""

    index: int
    duration_s: float
    memory_mb: float
    stderr_bytes: int
    fd_count: int | None  # None on non-Linux where /proc/self/fd is unavailable
    signature: str  # hash of the workload's reported result envelope


@dataclass(frozen=True)
class SoakFailure:
    """One assertion failure with a structured reason."""

    kind: str  # "memory_growth" | "time_drift" | "log_volume" | "fd_leak" | "signature_mismatch"
    detail: str  # human-readable diagnostic
    iteration: int | None = None  # which iteration triggered it, when applicable


@dataclass(frozen=True)
class SoakResult:
    """Outcome of one `run_soak` invocation.

    Attributes:
        suite: workload identifier (forwarded as-is to the benchmark runner).
        repeat: number of iterations requested.
        iterations: per-iteration measurements (one entry per completed run).
        failures: empty when passed=True; each entry is a structured assertion failure.
        passed: True only when failures is empty AND all iterations completed.
        error: empty on normal completion; structured `"<Class>: <msg>"` on
            top-level failure (e.g., workload raised, fd-count couldn't be
            sampled, etc.).
    """

    suite: str
    repeat: int
    iterations: list[SoakIteration] = field(default_factory=list)
    failures: list[SoakFailure] = field(default_factory=list)
    passed: bool = True
    error: str = ""

    def to_envelope(self) -> dict[str, Any]:
        """Project to the JSON envelope CLI --json + MCP would emit."""
        return {
            "suite": self.suite,
            "repeat": self.repeat,
            "iterations": [
                {
                    "index": it.index,
                    "duration_s": it.duration_s,
                    "memory_mb": it.memory_mb,
                    "stderr_bytes": it.stderr_bytes,
                    "fd_count": it.fd_count,
                    "signature": it.signature,
                }
                for it in self.iterations
            ],
            "failures": [{"kind": f.kind, "detail": f.detail, "iteration": f.iteration} for f in self.failures],
            "passed": self.passed,
            "error": self.error,
        }


def _default_workload_runner(suite: str) -> dict[str, Any]:
    """Default workload — runs the benchmark suite and returns its envelope.

    ``suite`` may be a bundle name (e.g. ``reflib``) or an explicit file path.
    Mirrors the resolution logic ``kairix benchmark run --suite SUITE`` uses
    so the operator gets the same name-shortcut UX (#222) here.

    Lazy-imports so a soak invocation with an injected runner doesn't pull
    the whole benchmark stack into module load.
    """
    from kairix.quality.benchmark.runner import run_benchmark
    from kairix.quality.benchmark.suite import load_suite, resolve_suite_path

    suite_path = resolve_suite_path(suite)
    suite_obj = load_suite(str(suite_path))
    result = run_benchmark(suite=suite_obj)
    return {"summary": result.summary, "case_count": len(result.cases)}


def _sample_memory_mb() -> float:
    """Resident memory in MB. Linux returns kilobytes; macOS returns bytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024  # Linux: KB → MB


def _sample_fd_count() -> int | None:
    """Open file descriptor count, or None if unsampleable on this platform."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    try:
        return len(os.listdir(fd_dir))
    except OSError:
        return None


def _signature(envelope: dict[str, Any]) -> str:
    """Stable hash of the workload result envelope.

    Two iterations with the same hash prove the workload is reproducible
    (no random ordering, no clock leakage, no per-call state contamination).
    """
    serialised = json.dumps(envelope, sort_keys=True, default=str).encode()
    return hashlib.sha256(serialised).hexdigest()[:16]


def _run_one_iteration(
    index: int,
    suite: str,
    workload_runner: Callable[[str], dict[str, Any]],
) -> SoakIteration:
    """Execute one iteration of the workload and return measurements.

    stderr is captured via redirect_stderr into a BytesIO so the soak run
    can assert on total stderr volume.
    """
    mem_before = _sample_memory_mb()
    fd_before = _sample_fd_count()

    captured_stderr = io.StringIO()
    t_start = time.perf_counter()
    with contextlib.redirect_stderr(captured_stderr):
        envelope = workload_runner(suite)
    duration = time.perf_counter() - t_start

    mem_after = _sample_memory_mb()
    fd_after = _sample_fd_count()
    stderr_bytes = len(captured_stderr.getvalue().encode("utf-8", errors="replace"))

    return SoakIteration(
        index=index,
        duration_s=round(duration, 3),
        memory_mb=round(mem_after - mem_before, 2),
        stderr_bytes=stderr_bytes,
        fd_count=fd_after if fd_before is not None else None,
        signature=_signature(envelope),
    )


# Workloads with sub-100ms baselines have too much measurement noise for
# percent-drift to mean anything. Above this floor the comparison is signal.
_TIME_DRIFT_BASELINE_FLOOR_S = 0.1


def _per_iter_failure(kind: str, iteration: int, body: str) -> SoakFailure:
    """Construct a per-iteration assertion failure with the canonical prefix."""
    return SoakFailure(kind=kind, iteration=iteration, detail=f"iteration {iteration} {body}")


def _check_memory_growth(its: list[SoakIteration], max_mb: float) -> list[SoakFailure]:
    """Flag iterations past iter-0 whose memory delta exceeds the cap.

    Iteration 0 is the warm-up — model loading, SQLite cache hydration,
    spaCy NLP-pipeline lazy-init, FAISS / usearch index open all happen
    on the first benchmark case and produce a large legitimate delta
    (~1 GB on the reflib suite). The signal worth catching is compounding
    growth in iter-1 onwards, where the warm-up cost is amortised and
    any further growth points at a leak.
    """
    return [
        _per_iter_failure(
            kind="memory_growth",
            iteration=it.index,
            body=f"grew RSS by {it.memory_mb:.1f} MB (cap {max_mb:.1f} MB)",
        )
        for it in its
        if it.index >= 1 and it.memory_mb > max_mb
    ]


def _check_time_drift(its: list[SoakIteration], max_pct: float) -> list[SoakFailure]:
    """Flag iterations whose duration drifts beyond max_pct of iteration 0.

    Skipped when the baseline iteration is sub-100ms — measurement noise
    dominates % drift below that floor and the gate would fire false positives
    on unit-test-shaped fake workloads.
    """
    if not its or its[0].duration_s < _TIME_DRIFT_BASELINE_FLOOR_S:
        return []
    baseline = its[0].duration_s
    failures = []
    for it in its[1:]:
        drift_pct = abs(it.duration_s - baseline) / baseline * 100
        if drift_pct > max_pct:
            failures.append(
                _per_iter_failure(
                    kind="time_drift",
                    iteration=it.index,
                    body=(
                        f"duration {it.duration_s:.2f}s drifted {drift_pct:.1f}% from "
                        f"baseline {baseline:.2f}s (cap {max_pct:.1f}%)"
                    ),
                )
            )
    return failures


def _check_log_volume(its: list[SoakIteration], max_total_mb: float) -> list[SoakFailure]:
    """One failure when total stderr across all iterations exceeds the cap.

    Aggregate across iterations — a single noisy iteration is fine if others
    are quiet; the check fires only on systematic log growth.
    """
    total = sum(it.stderr_bytes for it in its)
    cap_bytes = int(max_total_mb * 1024 * 1024)
    if total > cap_bytes:
        return [
            SoakFailure(
                kind="log_volume",
                detail=(
                    f"total stderr volume {total / 1024 / 1024:.2f} MB exceeds cap {max_total_mb:.2f} MB "
                    f"across {len(its)} iterations (~{total // max(1, len(its))} bytes/iter average)"
                ),
            )
        ]
    return []


def _check_fd_leak(its: list[SoakIteration]) -> list[SoakFailure]:
    """Flag growing fd_count across iterations.

    Returns [] when fds couldn't be sampled (non-Linux). Tightens to >5 fd
    growth so transient cache-files-in-flight don't fire false positives.
    """
    if not its or its[0].fd_count is None:
        return []
    baseline = its[0].fd_count
    failures = []
    for it in its[1:]:
        if it.fd_count is not None and it.fd_count - baseline > 5:
            failures.append(
                _per_iter_failure(
                    kind="fd_leak",
                    iteration=it.index,
                    body=(
                        f"held {it.fd_count} fds vs baseline {baseline} (delta {it.fd_count - baseline} > 5 threshold)"
                    ),
                )
            )
    return failures


def _check_signature_drift(its: list[SoakIteration]) -> list[SoakFailure]:
    """Two iterations with different signatures = workload isn't reproducible.

    Catches per-call random state, clock leakage, hidden caches mutating
    across runs. The benchmark suite itself is deterministic when correctly
    configured, so signatures should match exactly across repeats.
    """
    if len(its) < 2:
        return []
    baseline = its[0].signature
    failures = []
    for it in its[1:]:
        if it.signature != baseline:
            failures.append(
                _per_iter_failure(
                    kind="signature_mismatch",
                    iteration=it.index,
                    body=(
                        f"signature {it.signature} differs from iter-0 "
                        f"{baseline} — workload not deterministic across repeats"
                    ),
                )
            )
    return failures


def run_soak(
    suite: str,
    repeat: int = 3,
    *,
    max_memory_growth_mb: float = DEFAULT_MAX_MEMORY_GROWTH_MB,
    max_log_volume_mb: float = DEFAULT_MAX_LOG_VOLUME_MB_PER_REPEAT,
    max_time_drift_pct: float = DEFAULT_MAX_TIME_DRIFT_PCT,
    workload_runner: Callable[[str], dict[str, Any]] | None = None,
) -> SoakResult:
    """Run the workload `repeat` times and assert no degradation.

    Args:
        suite: workload identifier, forwarded to the workload runner.
        repeat: number of iterations; ≥2 to have anything to compare.
        max_memory_growth_mb: per-iteration RSS growth cap (MB).
        max_log_volume_mb: cap on total stderr volume across all iterations
            (MB per repeat — scaled by `repeat`).
        max_time_drift_pct: max % drift in per-iteration wall time vs iter-0.
        workload_runner: callable(suite) -> envelope dict. Tests inject a fake;
            production omits and gets the default benchmark runner.

    Returns:
        SoakResult — never raises; top-level errors populate .error.
    """
    if repeat < 2:
        return SoakResult(
            suite=suite,
            repeat=repeat,
            failures=[
                SoakFailure(
                    kind="invalid_argument",
                    detail=f"repeat must be >= 2 to compare iterations; got {repeat}",
                )
            ],
            passed=False,
        )
    runner = workload_runner or _default_workload_runner
    cap_total_mb = max_log_volume_mb * repeat

    iterations: list[SoakIteration] = []
    try:
        for i in range(repeat):
            iterations.append(_run_one_iteration(index=i, suite=suite, workload_runner=runner))
    except Exception as exc:
        logger.warning("run_soak: workload raised at iteration %d — %s", len(iterations), exc, exc_info=True)
        return SoakResult(
            suite=suite,
            repeat=repeat,
            iterations=iterations,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    failures: list[SoakFailure] = []
    failures.extend(_check_memory_growth(iterations, max_memory_growth_mb))
    failures.extend(_check_time_drift(iterations, max_time_drift_pct))
    failures.extend(_check_log_volume(iterations, cap_total_mb))
    failures.extend(_check_fd_leak(iterations))
    failures.extend(_check_signature_drift(iterations))

    return SoakResult(
        suite=suite,
        repeat=repeat,
        iterations=iterations,
        failures=failures,
        passed=not failures,
    )
