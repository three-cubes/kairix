#!/usr/bin/env python3
"""Refuse a partial ``kairix eval --json`` SuiteResult (LLM-judge failures).

Thin CLI over the shared :mod:`kairix.quality.completeness` rule: a result
with any judge failure covers only the questions the judge actually scored,
so the LoCoMo nightly must not publish it as a trend point and the nightly
comparison must not compare against (or from) one.

Usage::

    python3 scripts/ci/judge_coverage.py <label> <suite-result.json>

Exit 0 when every question was judged; exit 3 (the shared
``EXIT_INCONCLUSIVE``) with a GitHub ``::error::`` line and fix:/next:
hints when the result is partial; exit 1 when the file cannot be read.
Artifacts written before judge failures were recorded count as complete.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kairix.quality.completeness import EXIT_INCONCLUSIVE, judge_failures, partial_diagnostic


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
        print("::error::" + partial_diagnostic(f"{label} SuiteResult {path}", failures))
        return EXIT_INCONCLUSIVE
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
