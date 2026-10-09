#!/usr/bin/env python3
"""Build the benchmark-gate PR comment body (``benchmark-gate.yml``).

Usage::

    python3 scripts/ci/benchmark_pr_comment.py <current.json> <baseline.json> <gate-outcome> <out.txt>

``gate-outcome`` is the compare step's outcome (``success`` / ``failure`` /
...). The body is written to ``out.txt`` for the posting step.

Completeness is checked FIRST, through the shared
:mod:`kairix.quality.completeness` rule: if either result is partial
(LLM-judge failures) the body is ONLY the INCONCLUSIVE diagnostic — no
weighted-total line and no category-delta table, since partial scores are
never compared. A complete pair renders the usual delta table and verdict.

Unreadable result files are reported on stderr and no body is written (the
posting step then falls back to its "could not read results" notice); the
script still exits 0 so the comment step never masks the gate's own status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from kairix.quality.completeness import judge_failures

_HEADER = "Benchmark Gate — Contract Suite"
_CATEGORY_FLOOR = 0.50
_CATEGORY_WARN_DELTA = -0.01


def _inconclusive_body(partial: dict[str, int]) -> str:
    detail = ", ".join(f"{name}: {n} judge failure(s)" for name, n in partial.items())
    return "\n".join(
        [
            f"## ⚠️ {_HEADER}",
            "",
            f"_Gate INCONCLUSIVE: PARTIAL result ({detail}) — scores not compared._",
            "",
            "fix: check the LLM provider credentials / availability, then re-run the gate.",
        ]
    )


def _delta_body(current: dict[str, Any], baseline: dict[str, Any], passed: bool) -> str:
    cur_total = current["summary"]["weighted_total"]
    base_total = baseline["summary"]["weighted_total"]
    delta = cur_total - base_total
    lines = [
        f"## {'✅' if passed else '❌'} {_HEADER}",
        "",
        f"**Weighted total:** {base_total:.4f} → {cur_total:.4f} ({delta:+.4f})",
        "",
        "| Category | Baseline | Current | Delta |",
        "|---|---|---|---|",
    ]
    cur_cats = current["summary"].get("category_scores", {})
    base_cats = baseline["summary"].get("category_scores", {})
    for cat in sorted(set(cur_cats) | set(base_cats)):
        b = base_cats.get(cat, 0.0)
        c = cur_cats.get(cat, 0.0)
        d = c - b
        flag = " ⚠️" if c < _CATEGORY_FLOOR else (" ↓" if d < _CATEGORY_WARN_DELTA else "")
        lines.append(f"| {cat} | {b:.4f} | {c:.4f} | {d:+.4f}{flag} |")
    lines += [
        "",
        "_No regression detected. Gate passed._"
        if passed
        else "_Gate failed: regression detected (threshold ±0.02). Review scores above._",
    ]
    return "\n".join(lines)


def build_body(current: dict[str, Any], baseline: dict[str, Any], gate_outcome: str) -> str:
    """Return the PR comment body; partial on either side → INCONCLUSIVE only."""
    partial = {name: judge_failures(data) for name, data in (("current", current), ("baseline", baseline))}
    partial = {name: n for name, n in partial.items() if n}
    if partial:
        return _inconclusive_body(partial)
    return _delta_body(current, baseline, gate_outcome == "success")


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: benchmark_pr_comment.py <current.json> <baseline.json> <gate-outcome> <out.txt>", file=sys.stderr)
        return 2
    current_path, baseline_path, gate_outcome, out_path = argv[1:]
    try:
        current = json.loads(Path(current_path).read_text(encoding="utf-8"))
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read result files: {type(exc).__name__}", file=sys.stderr)
        return 0
    Path(out_path).write_text(build_body(current, baseline, gate_outcome), encoding="utf-8")
    print("Comment body written.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
