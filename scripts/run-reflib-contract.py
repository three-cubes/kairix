#!/usr/bin/env python3
"""Run the reference library contract suite against the mock backend.

Used in CI to verify search pipeline correctness without API credentials.
Exit code 0 if all thresholds pass, 1 otherwise, 3 (inconclusive) if the
run is partial (LLM-judge failures).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kairix.quality.benchmark.runner import run_benchmark
from kairix.quality.benchmark.suite import load_suite, resolve_suite_path
from kairix.quality.completeness import EXIT_INCONCLUSIVE, judge_failures, partial_diagnostic

FLOOR = 0.50  # Minimum weighted_total to pass


def main() -> int:
    # #450 — the suites moved under kairix/data/suites/ (package-data), so a
    # literal "suites/..." path no longer resolves. Resolve through the
    # bundled root by name: resolve_suite_path globs <name>-gold-v*.yaml then
    # <name>.yaml in kairix.paths.bundled_suites_root().
    suite = load_suite(str(resolve_suite_path("reflib-contract-suite")))
    result = run_benchmark(suite, system="mock-reflib", agent="shared")

    failures = judge_failures(result)
    if failures:
        print(partial_diagnostic("the reflib contract run", failures))
        return EXIT_INCONCLUSIVE

    wt = result.summary.get("weighted_total", 0)
    cats = result.summary.get("category_scores", {})

    print(f"Reflib contract suite: weighted_total = {wt:.3f}")
    for cat, score in sorted(cats.items()):
        print(f"  {cat}: {score:.3f}")

    if wt < FLOOR:
        print(f"FAIL: weighted_total {wt:.3f} < floor {FLOOR}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
