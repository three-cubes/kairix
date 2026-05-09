"""
Proactive quality monitoring for kairix retrieval.

Runs a canary benchmark suite after each embed cycle and logs results to
a rolling JSONL file. Detects regression by comparing the current run's
weighted NDCG to the rolling window average.

Intended integration after `kairix embed`:

    kairix eval monitor \\
        --suite /path/to/canary.yaml \\
        --log /data/logs/kairix-monitor.jsonl \\
        --alert-threshold 0.05

Log format: one JSON object per line, same rolling-window pattern as
kairix/embed/recall_check.py (90-run cap).

Never raises — returns MonitorResult with regression=False on any error.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.quality.eval.constants import CATEGORY_WEIGHTS

if TYPE_CHECKING:
    from kairix.quality.benchmark.runner import BenchmarkResult
    from kairix.quality.benchmark.suite import BenchmarkSuite

# DI seams used by ``run_monitor`` — production defaults resolve lazily to
# ``kairix.quality.benchmark.suite.load_suite`` and
# ``kairix.quality.benchmark.runner.run_benchmark``. Tests inject fakes.
SuiteLoader = Callable[[str], "BenchmarkSuite"]
BenchmarkRunnerFn = Callable[..., "BenchmarkResult"]

logger = logging.getLogger(__name__)

# Default log path (override with KAIRIX_MONITOR_LOG env var)
_DEFAULT_LOG_PATH: str = str(Path.home() / ".cache/kairix/monitor.jsonl")

# Maximum log entries to retain (rolling window)
_MAX_LOG_ENTRIES: int = 90

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class MonitorResult:
    """Result of a single monitor run."""

    ts: str  # ISO-8601 timestamp
    suite_path: str
    n_cases: int
    ndcg_by_category: dict[str, float]
    weighted_ndcg: float
    vec_failed_count: int
    regression: bool
    regression_detail: str | None


# ---------------------------------------------------------------------------
# Log I/O
# ---------------------------------------------------------------------------


def _load_log(log_path: str) -> list[dict[str, Any]]:
    """Load existing monitor log entries. Returns [] on missing file or parse error."""
    p = Path(log_path)
    if not p.exists():
        return []
    entries = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # Defensive guard for read-time OS errors (unreadable file, permission
    # denied, transient I/O failure). The two test-reachable failure modes —
    # missing file and corrupt JSON — are handled above; this catch-all
    # only fires under the OS-level failures we can't induce in unit tests.
    except Exception as e:  # pragma: no cover
        logger.warning("monitor: failed to load log %r — %s", log_path, e)
    return entries


def _append_log(log_path: str, entry: dict[str, Any], max_entries: int = _MAX_LOG_ENTRIES) -> None:
    """Append a log entry and trim to max_entries. Never raises."""
    try:
        entries = _load_log(log_path)
        entries.append(entry)
        # Keep only the most recent max_entries
        entries = entries[-max_entries:]
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    # Defensive guard for write-time OS errors (read-only fs, disk full,
    # permission denied). These can't be induced in unit tests; the
    # happy path is exercised by every run_monitor test that writes a log.
    except Exception as e:  # pragma: no cover
        logger.warning("monitor: failed to append log entry — %s", e)


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


def _rolling_average(entries: list[dict[str, Any]], window_days: int) -> float | None:
    """
    Compute the weighted_ndcg rolling average over the last window_days.

    Returns None if no qualifying entries exist (e.g. first run).
    """
    from datetime import timedelta

    if not entries:
        return None

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=window_days)

    values = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                values.append(float(e["weighted_ndcg"]))
        except (KeyError, ValueError):
            pass

    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_monitor(
    suite_path: str,
    log_path: str | None = None,
    alert_threshold: float = 0.05,
    window_days: int = 7,
    agent: str = "shape",
    *,
    suite_loader: SuiteLoader | None = None,
    benchmark_runner: BenchmarkRunnerFn | None = None,
) -> MonitorResult:
    """
    Run the canary benchmark suite and check for retrieval regression.

    Args:
        suite_path:       Path to the canary suite YAML.
        log_path:         Path to the monitor JSONL log file.
                          Defaults to KAIRIX_MONITOR_LOG env var or ~/.cache/kairix/monitor.jsonl.
        alert_threshold:  Relative NDCG drop that triggers regression flag (default: 0.05 = 5%).
        window_days:      Rolling window for baseline average in days (default: 7).
        agent:            Agent name for retrieval scoping.
        suite_loader:     Injection seam for the suite-loading callable. ``None``
                          (default) resolves lazily to
                          ``kairix.quality.benchmark.suite.load_suite``. Tests pass
                          a callable returning a ``BenchmarkSuite``.
        benchmark_runner: Injection seam for the benchmark-running callable.
                          ``None`` (default) resolves lazily to
                          ``kairix.quality.benchmark.runner.run_benchmark``. Tests
                          pass a callable returning a ``BenchmarkResult``.

    Returns:
        MonitorResult. Never raises.
    """
    # Lazy production defaults — kept inside the function to avoid a circular
    # import (runner → eval.constants → eval.__init__ → monitor → runner).
    if suite_loader is None:  # pragma: no cover
        from kairix.quality.benchmark.suite import load_suite

        suite_loader = load_suite
    if benchmark_runner is None:  # pragma: no cover
        from kairix.quality.benchmark.runner import run_benchmark

        benchmark_runner = run_benchmark

    if log_path is None:
        log_path = os.environ.get("KAIRIX_MONITOR_LOG", _DEFAULT_LOG_PATH)

    ts = datetime.now(tz=timezone.utc).isoformat()

    # Defaults for error returns
    _empty = MonitorResult(
        ts=ts,
        suite_path=suite_path,
        n_cases=0,
        ndcg_by_category={},
        weighted_ndcg=0.0,
        vec_failed_count=0,
        regression=False,
        regression_detail=None,
    )

    try:
        suite = suite_loader(suite_path)
        n_cases = len(suite.cases)

        if n_cases == 0:
            logger.warning("monitor: suite %r has 0 cases", suite_path)
            return _empty

        result = benchmark_runner(suite, system="hybrid", agent=agent)

        ndcg_by_category = {
            cat: round(float(result.summary["category_scores"].get(cat, 0.0)), 4) for cat in CATEGORY_WEIGHTS
        }

        # Compute weighted NDCG
        weighted = sum(ndcg_by_category.get(cat, 0.0) * w for cat, w in CATEGORY_WEIGHTS.items())
        weighted_ndcg = round(weighted, 4)

        vec_failed = sum(1 for case in result.cases if case.get("meta", {}).get("vec_failed", False))

    except Exception as e:
        logger.warning("monitor: benchmark run failed — %s", e)
        return _empty

    # Load existing log and compute baseline
    existing_entries = _load_log(log_path)
    baseline = _rolling_average(existing_entries, window_days)

    regression = False
    regression_detail: str | None = None

    if baseline is not None:
        drop = baseline - weighted_ndcg
        relative_drop = drop / baseline if baseline > 0 else 0.0
        if relative_drop > alert_threshold:
            regression = True
            regression_detail = (
                f"weighted_ndcg dropped {drop:.4f} ({relative_drop:.1%}) vs "
                f"{window_days}d average {baseline:.4f} "
                f"(threshold: {alert_threshold:.1%})"
            )
            logger.warning("monitor: REGRESSION DETECTED — %s", regression_detail)
        else:
            logger.info(
                "monitor: no regression. weighted_ndcg=%.4f, %dd avg=%.4f",
                weighted_ndcg,
                window_days,
                baseline,
            )
    else:
        logger.info("monitor: first run. weighted_ndcg=%.4f (no baseline yet)", weighted_ndcg)

    monitor_result = MonitorResult(
        ts=ts,
        suite_path=suite_path,
        n_cases=n_cases,
        ndcg_by_category=ndcg_by_category,
        weighted_ndcg=weighted_ndcg,
        vec_failed_count=vec_failed,
        regression=regression,
        regression_detail=regression_detail,
    )

    # Append to log
    _append_log(log_path, asdict(monitor_result))

    return monitor_result


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------


def _filter_recent_entries(entries: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    """Return entries with a timestamp at or after the cutoff."""
    recent: list[dict[str, Any]] = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent.append(e)
        except (KeyError, ValueError):
            pass
    return recent


def generate_report(log_path: str, days: int = 30) -> str:
    """
    Generate a markdown report from the monitor log.

    Args:
        log_path: Path to the monitor JSONL log.
        days:     Number of days of history to include.

    Returns:
        Markdown string. Returns a "no data" message if log is empty.
    """
    from datetime import timedelta

    entries = _load_log(log_path)
    if not entries:
        return "# Kairix Monitor Report\n\nNo monitor data found.\n"

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=days)

    recent = _filter_recent_entries(entries, cutoff)

    if not recent:
        return f"# Kairix Monitor Report\n\nNo data in the last {days} days.\n"

    lines = [
        "# Kairix Monitor Report",
        f"\n**Period:** last {days} days  |  **Runs:** {len(recent)}\n",
    ]

    # Summary table
    lines.append("## Weighted NDCG Over Time\n")
    lines.append("| Timestamp | Weighted NDCG | Regression |")
    lines.append("|-----------|--------------|------------|")
    for e in recent[-20:]:  # last 20 runs
        ts_str = e.get("ts", "?")[:19]
        wt = e.get("weighted_ndcg", 0.0)
        reg = "🔴 YES" if e.get("regression") else "✅ no"
        lines.append(f"| {ts_str} | {wt:.4f} | {reg} |")

    lines.append("")

    # Category breakdown of the latest run
    latest = recent[-1]
    lines.append("## Latest Run — Category Breakdown\n")
    lines.append("| Category | NDCG | Weight | Contribution |")
    lines.append("|----------|------|--------|--------------|")
    by_cat = latest.get("ndcg_by_category", {})
    for cat, w in CATEGORY_WEIGHTS.items():
        ndcg = by_cat.get(cat, 0.0)
        contrib = ndcg * w
        lines.append(f"| {cat} | {ndcg:.4f} | {w:.0%} | {contrib:.4f} |")

    # Regressions
    regressions = [e for e in recent if e.get("regression")]
    if regressions:
        lines.append("\n## Regression Events\n")
        for e in regressions:
            lines.append(f"- **{e.get('ts', '?')[:19]}**: {e.get('regression_detail', 'unknown')}")

    lines.append(f"\n_Generated by `kairix eval report` at {now.isoformat()[:19]}Z_\n")

    return "\n".join(lines)
