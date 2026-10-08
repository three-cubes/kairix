#!/usr/bin/env python3
"""Fail when a ``kairix eval --json`` SuiteResult has LLM-judge failures.

A SuiteResult with ``judge_failures > 0`` covers only the questions the
judge actually scored, so its pass rate and mean are partial. The LoCoMo
nightly must not publish such a result as a trend point, and the nightly
comparison must not compare against (or from) one.

Usage::

    python3 scripts/ci/judge_coverage.py <label> <suite-result.json>

Exit 0 when every question was judged; exit 1 (with a GitHub ``::error::``
line and fix:/next: hints) when any judge call failed or the file cannot
be read. Artifacts written before ``judge_failures`` existed carry no
count and are treated as complete.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def judge_failures(data: dict) -> int:
    """Judge failures in a SuiteResult: the summary count or the failed rows, whichever is larger."""
    counted = int(data.get("judge_failures", 0) or 0)
    failed_rows = sum(1 for row in data.get("rows") or [] if isinstance(row, dict) and row.get("judge_failure"))
    return max(counted, failed_rows)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: judge_coverage.py <label> <suite-result.json>", file=sys.stderr)
        return 2
    label, path = argv[1], Path(argv[2])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::{label} SuiteResult {path} is unreadable: {exc}")
        print("fix: re-run `kairix eval <suite> --json` and check its stderr")
        print("next: re-run the workflow once the result is valid JSON")
        return 1
    failures = judge_failures(data)
    if failures:
        print(
            f"::error::{label} SuiteResult {path} is partial — the LLM judge failed on "
            f"{failures} question(s), so its pass rate / mean cover only the judged questions"
        )
        print("fix: check the LLM provider credentials / availability for the eval run")
        print("next: re-run the nightly; partial results are never published or compared")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
