"""
Baseline comparison for the CI benchmark gate.

Loads the committed baseline JSON, compares it to a fresh run, and emits
a structured comparison result. Used by the benchmark-gate CI workflow.

Baseline format (benchmark-results/contract-baseline.json):
  {
    "meta": { ... },
    "summary": {
      "weighted_total": 0.XXXX,
      "category_scores": { "recall": 0.X, ... },
      ...
    },
    ...
  }

Gate rules:
  FAIL if overall weighted_total drops by > REGRESSION_THRESHOLD (default 0.02)
  FAIL if any category score drops below CATEGORY_FLOOR (default 0.50)
  WARN (non-failing) if any category delta < -0.01 but overall within threshold
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD: float = 0.02  # fail if weighted_total drops more than this
CATEGORY_FLOOR: float = 0.50  # fail if any category drops below this
CATEGORY_WARN_THRESHOLD: float = 0.01  # warn if any category drops more than this


def load_result(path: str | Path) -> dict:
    """Load a benchmark result JSON file. Raises FileNotFoundError or ValueError."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Benchmark result not found: {p}")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if "summary" not in data:
        raise ValueError(f"Result file missing 'summary' key: {p}")
    return data


def _build_summary_lines(
    baseline_total: float,
    current_total: float,
    overall_delta: float,
    baseline_cats: dict[str, float],
    current_cats: dict[str, float],
    all_cats: list[str],
    category_deltas: dict[str, float],
    category_warns: list[str],
    category_fails: list[str],
    regression: bool,
) -> list[str]:
    """Build human-readable summary lines for CI output."""
    lines: list[str] = [
        "Benchmark Gate — Contract Suite",
        "=" * 45,
        f"Baseline:  {baseline_total:.4f}",
        f"Current:   {current_total:.4f}",
        f"Delta:     {overall_delta:+.4f}",
        "",
        "Category breakdown:",
    ]
    for cat in all_cats:
        b = baseline_cats.get(cat, 0.0)
        c = current_cats.get(cat, 0.0)
        d = category_deltas[cat]
        flag = ""
        if cat in category_fails:
            flag = " ❌ BELOW FLOOR"
        elif cat in category_warns:
            flag = " ⚠️  WARN"
        lines.append(f"  {cat:<14} {b:.4f} → {c:.4f}  ({d:+.4f}){flag}")
    lines.append("")

    if regression:
        lines.append(f"❌ FAIL: weighted_total dropped {abs(overall_delta):.4f} (threshold: {REGRESSION_THRESHOLD})")
    elif category_fails:
        lines.append(f"❌ FAIL: categories below floor ({CATEGORY_FLOOR}): {', '.join(category_fails)}")
    else:
        if category_warns:
            lines.append(f"⚠️  WARN: category regression in: {', '.join(category_warns)}")
        lines.append("✅ PASS: no regression detected")

    return lines


def compare(baseline: dict, current: dict) -> dict:
    """
    Compare current benchmark result to baseline.

    Returns a comparison dict with keys:
      passed:          bool — True if all gate rules pass
      overall_delta:   float — current weighted_total minus baseline
      regression:      bool — True if delta < -REGRESSION_THRESHOLD
      category_deltas: dict[str, float] — per-category score deltas
      category_warns:  list[str] — categories with delta < -CATEGORY_WARN_THRESHOLD
      category_fails:  list[str] — categories below CATEGORY_FLOOR
      baseline_score:  float
      current_score:   float
      summary_lines:   list[str] — human-readable lines for CI output
    """
    baseline_total = baseline["summary"]["weighted_total"]
    current_total = current["summary"]["weighted_total"]
    overall_delta = current_total - baseline_total

    baseline_cats = baseline["summary"].get("category_scores", {})
    current_cats = current["summary"].get("category_scores", {})

    category_deltas: dict[str, float] = {}
    category_warns: list[str] = []
    category_fails: list[str] = []

    all_cats = sorted(set(baseline_cats) | set(current_cats))
    for cat in all_cats:
        b = baseline_cats.get(cat, 0.0)
        c = current_cats.get(cat, 0.0)
        delta = c - b
        category_deltas[cat] = round(delta, 4)
        if c < CATEGORY_FLOOR and not (b < 1e-9 and c < 1e-9):
            # Skip floor check when both baseline and current are 0.0
            # (no test cases exist for this category)
            category_fails.append(cat)
        elif delta < -CATEGORY_WARN_THRESHOLD:
            category_warns.append(cat)

    regression = overall_delta < -REGRESSION_THRESHOLD
    passed = not regression and not category_fails

    lines = _build_summary_lines(
        baseline_total,
        current_total,
        overall_delta,
        baseline_cats,
        current_cats,
        all_cats,
        category_deltas,
        category_warns,
        category_fails,
        regression,
    )

    return {
        "passed": passed,
        "overall_delta": round(overall_delta, 4),
        "regression": regression,
        "category_deltas": category_deltas,
        "category_warns": category_warns,
        "category_fails": category_fails,
        "baseline_score": baseline_total,
        "current_score": current_total,
        "summary_lines": lines,
    }


def run_gate(baseline_path: str, current_path: str) -> int:
    """
    Load baseline and current results, compare, print summary, return exit code.

    Returns 0 (pass) or 1 (fail). Suitable for direct invocation from CI.
    """
    try:
        baseline = load_result(baseline_path)
    except (FileNotFoundError, ValueError):
        logger.exception("Loading baseline")
        return 1

    try:
        current = load_result(current_path)
    except (FileNotFoundError, ValueError):
        logger.exception("Loading current result")
        return 1

    result = compare(baseline, current)
    for line in result["summary_lines"]:
        logger.info(line)

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        logger.error("Usage: python -m kairix.quality.benchmark.baseline <baseline.json> <current.json>")
        sys.exit(1)
    sys.exit(run_gate(sys.argv[1], sys.argv[2]))
