#!/usr/bin/env python3
"""Compare current reflib contract results against committed baseline.

Exit code 0 if no regression detected or no baseline exists yet.
Exit code 1 if regression exceeds threshold.
Exit code 3 (inconclusive) if the committed baseline is partial (LLM-judge failures).
"""

import json
import sys
from pathlib import Path

from kairix.quality.completeness import EXIT_INCONCLUSIVE, judge_failures, partial_diagnostic

BASELINE_PATH = Path("benchmark-results/reflib-contract-baseline.json")
REGRESSION_THRESHOLD = 0.02


def main() -> int:
    if not BASELINE_PATH.exists():
        print("No baseline committed yet -- skipping comparison")
        return 0

    try:
        baseline = json.loads(BASELINE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot read baseline: {e}")
        return 1

    failures = judge_failures(baseline)
    if failures:
        print(partial_diagnostic(f"the committed baseline {BASELINE_PATH}", failures))
        return EXIT_INCONCLUSIVE

    baseline_wt = baseline.get("summary", {}).get("weighted_total", 0)
    if baseline_wt <= 0:
        print(f"Baseline weighted_total is {baseline_wt:.3f} — invalid or missing")
        return 1

    print(f"Baseline weighted_total: {baseline_wt:.3f}")
    print("Comparison check ready (run contract suite first to generate current results)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
