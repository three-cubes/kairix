"""Is this quality result complete? — the one shared answer.

A benchmark result or eval suite result is **partial** when an LLM judge
failed on any case/question: its totals then cover only the judged cases,
so it must not be gated, compared, ranked, trended or published as if it
were complete. Every consumer routes through :func:`judge_failures` /
:func:`is_complete` so the rule lives in one place:

* partial  ⇔  ``judge_failures > 0`` (summary count, top-level count, or any
  case/row carrying ``judge_failure``) or ``gates.judge_coverage`` is False;
* results written before judge failures were recorded carry none of these
  fields and count as complete.

Gate and compare paths return :data:`EXIT_INCONCLUSIVE` with
:func:`partial_diagnostic`; display-only paths print :func:`partial_warning`.

Accepted shapes (dicts loaded from JSON, or the in-memory dataclasses):

* benchmark result — ``{"summary": {"judge_failures", "gates"}, "cases": [...]}``
  (``BenchmarkResult``);
* eval suite result — ``{"judge_failures", "rows": [...]}`` (``SuiteResult``).

Stdlib-only so CI scripts can import it without the heavy kairix stack.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
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


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _failed_rows(rows: Any) -> int:
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
        return 0
    return sum(1 for row in rows if isinstance(row, Mapping) and row.get("judge_failure"))


def judge_failures(result: Any) -> int:
    """Number of LLM-judge failures recorded in ``result`` (0 = complete).

    Takes the largest of the summary count, the top-level count and the
    number of failed case/row entries; a failed ``judge_coverage`` gate with
    no count still reports 1.
    """
    data = _as_mapping(result)
    summary = data.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    failures = max(
        _count(summary.get(_KEY_JUDGE_FAILURES)),
        _count(data.get(_KEY_JUDGE_FAILURES)),
        _failed_rows(data.get("cases")),
        _failed_rows(data.get("rows")),
    )
    gates = summary.get("gates")
    if failures == 0 and isinstance(gates, Mapping) and gates.get("judge_coverage") is False:
        return 1
    return failures


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
