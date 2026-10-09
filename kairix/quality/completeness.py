"""Is this quality result complete? — the one shared answer.

A benchmark result or eval suite result is **partial** when an LLM judge
failed on any case/question: its totals then cover only the judged cases,
so it must not be gated, compared, ranked, trended or published as if it
were complete. Every consumer routes through :func:`judge_failures` /
:func:`is_complete` so the rule lives in one place:

* partial  ⇔  the result's ``judge_failures`` count is > 0;
* results written before judge failures were recorded carry no count and
  count as complete.

Gate and compare paths return :data:`EXIT_INCONCLUSIVE` with
:func:`partial_diagnostic`; display-only paths print :func:`partial_warning`.

Each result type carries the count in exactly ONE canonical place (dicts
loaded from JSON, or the in-memory dataclasses):

* benchmark result (``BenchmarkResult``) — ``summary.judge_failures``;
* eval suite result (``SuiteResult``, LoCoMo rows, cutover partial marker) —
  top-level ``judge_failures``.

Stdlib-only so CI scripts can import it without the heavy kairix stack.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

EXIT_INCONCLUSIVE: int = 3
"""Shared exit code: the result is partial, so the gate / compare did not run."""

_KEY_JUDGE_FAILURES = "judge_failures"


def _as_mapping(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        return result
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    return {}


def judge_failures(result: Any) -> int:
    """Number of LLM-judge failures recorded in ``result`` (0 = complete).

    Reads the one canonical count: ``summary.judge_failures`` for a benchmark
    result (it has a ``summary``), else the top-level ``judge_failures``.
    """
    data = _as_mapping(result)
    summary = data.get("summary")
    holder = summary if isinstance(summary, Mapping) else data
    return int(holder.get(_KEY_JUDGE_FAILURES) or 0)


def is_complete(result: Any) -> bool:
    """True when every case/question in ``result`` was actually judged."""
    return judge_failures(result) == 0


def partial_diagnostic(label: str, failures: int) -> str:
    """F21 diagnostic for a gate / compare path that refuses a partial result."""
    return (
        f"INCONCLUSIVE: {label} is PARTIAL — the LLM judge failed on {failures} case(s), "
        f"so its scores cover only the judged cases and are not compared or gated.\n"
        f"fix: check the LLM provider credentials / availability and re-run.\n"
        f"next: re-run until judge_failures is 0, then repeat this step."
    )


def partial_warning(label: str, failures: int) -> str:
    """One-line warning for display-only paths showing a partial result."""
    return (
        f"⚠️  PARTIAL RESULT: {label} — the LLM judge failed on {failures} case(s); "
        f"the scores shown cover only the judged cases."
    )


__all__ = [
    "EXIT_INCONCLUSIVE",
    "is_complete",
    "judge_failures",
    "partial_diagnostic",
    "partial_warning",
]
