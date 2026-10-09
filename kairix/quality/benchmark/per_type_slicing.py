"""Per-source-type slicing + boundary-spanning canary aggregation.

ADR-028 §"Quality evaluation" measurement scaffolding. Sits next to
``runner.py`` and reads its already-built case-result dicts; never
mutates the existing scoring path (F47 — composition seam, not a
substitution).

Two exported helpers:

* :func:`aggregate_per_source_type` — slices case results by
  ``source_type``, returning a ``{type: {ndcg_at_10, mrr_at_10,
  hit_at_10, n}}`` summary mapping.
* :func:`aggregate_canary` — projects canary-flagged cases into
  ``{unit: {passed, total, rate}}`` plus an overall rate.

The ``source_type`` for each case is resolved by:

1. Explicit ``case.source_type`` field on the suite YAML (wins).
2. Falling back to the file extension on the gold answer's title /
   path (``meeting.docx`` → ``docx``; ``slide-7.pptx`` → ``pptx``).
3. Final fallback: ``"unknown"`` — surfaces in the per-type table so
   gaps are visible rather than silently aggregated into another type.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# Mapping from file extension (lowercase, no dot) to the canonical
# source-type slug used in the per-type summary. ADR-028 §"Per-type
# chunking specification" defines the type set; this map ensures every
# extension the synthetic per-type-fixture corpus produces resolves to
# one of those slugs.
_EXTENSION_TO_TYPE: dict[str, str] = {
    "md": "markdown",
    "markdown": "markdown",
    "pptx": "pptx",
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "xls": "xlsx",
    "eml": "email",
    "msg": "email",
    "ics": "calendar",
    "ical": "calendar",
}

# Canary atomic-unit names — ADR-028 §"Quality evaluation" #3. A
# canary's unit names which atomic boundary the chunker plugin must
# preserve; failure means the regression split that unit.
_CANONICAL_UNITS: tuple[str, ...] = ("slide", "row", "event", "message")

# Pass-rate threshold for canary scoring. An NDCG@10 ≥ 0.5 means the
# retriever surfaced enough of the answer chunks for the canary to be
# considered passing — a stricter bar than the global category floor
# because canaries are deliberately constructed easy-when-chunked,
# hard-when-split queries.
_CANARY_PASS_THRESHOLD: float = 0.5


def _strip_extension(name: str) -> tuple[str, str]:
    """Return ``(stem, ext)`` where ``ext`` is lowercase and dotless."""
    m = re.search(r"\.([A-Za-z0-9]+)$", name)
    if not m:
        return name, ""
    return name[: m.start()], m.group(1).lower()


def _gold_list_candidates(case: Any, attr: str, key: str) -> list[str]:
    """Pull string candidates from a gold-list attribute on ``case``."""
    out: list[str] = []
    for entry in getattr(case, attr, None) or []:
        if not isinstance(entry, dict):
            continue
        value = entry.get(key)
        if value:
            out.append(str(value))
    return out


def _gold_scalar_candidate(case: Any, attr: str) -> list[str]:
    """Wrap a scalar gold-* attribute on ``case`` into a candidate list."""
    value = getattr(case, attr, None)
    return [str(value)] if value else []


def _derive_source_type(case: Any) -> str:
    """Pick the source-type slug for a single BenchmarkCase.

    Resolution order:

    1. ``case.source_type`` (explicit).
    2. Extension of the highest-relevance ``gold_titles[i].title``.
    3. Extension of the highest-relevance ``gold_paths[i].path``.
    4. Extension of ``case.gold_path`` / ``case.gold_title``.
    5. ``"unknown"``.
    """
    explicit = getattr(case, "source_type", None)
    if explicit:
        return str(explicit)

    candidates: list[str] = [
        *_gold_list_candidates(case, "gold_titles", "title"),
        *_gold_list_candidates(case, "gold_paths", "path"),
        *_gold_scalar_candidate(case, "gold_title"),
        *_gold_scalar_candidate(case, "gold_path"),
    ]
    for cand in candidates:
        _, ext = _strip_extension(cand)
        if ext and ext in _EXTENSION_TO_TYPE:
            return _EXTENSION_TO_TYPE[ext]
    return "unknown"


def _hit_at_10(case_result: dict[str, Any]) -> float:
    """Synth Hit@10 from the per-case score and gold list.

    The runner already records ``hit_at_5`` and ``rr`` (reciprocal rank)
    for NDCG cases; Hit@10 is 1.0 iff the reciprocal rank > 0 (i.e. at
    least one relevant doc landed in the top-10). A non-NDCG case
    contributes Hit@10 = 1.0 iff the score itself is ≥ 0.5.
    """
    rr = case_result.get("rr")
    if rr is not None:
        return 1.0 if float(rr) > 0.0 else 0.0
    return 1.0 if float(case_result.get("score", 0.0)) >= 0.5 else 0.0


def aggregate_per_source_type(
    cases: Sequence[Any],
    case_results: Sequence[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Slice the case results by source-type.

    Returns a mapping ``{type: {ndcg_at_10, mrr_at_10, hit_at_10, n}}``
    where ``n`` is the integer query count for that type. Types with
    zero cases are omitted.

    ``cases`` and ``case_results`` must be index-aligned — the runner
    builds them in the same loop. The function never assumes any
    particular ordering inside either sequence other than that.
    """
    if len(cases) != len(case_results):
        raise ValueError(
            f"per-type slicing requires index-aligned inputs; got "
            f"{len(cases)} cases vs {len(case_results)} case_results. "
            f"fix: pass the BenchmarkSuite.cases + BenchmarkResult.cases "
            f"that the runner emitted in the same loop. "
            f"next: see kairix.quality.benchmark.runner.run_benchmark."
        )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for case, result in zip(cases, case_results, strict=False):
        if result.get("score_method") != "ndcg":
            continue
        source_type = _derive_source_type(case)
        by_type.setdefault(source_type, []).append(result)

    summary: dict[str, dict[str, float]] = {}
    for stype, rows in by_type.items():
        n = len(rows)
        if n == 0:
            continue
        ndcg = sum(float(r.get("score", 0.0)) for r in rows) / n
        mrr = sum(float(r.get("rr", 0.0)) for r in rows) / n
        hit = sum(_hit_at_10(r) for r in rows) / n
        summary[stype] = {
            "ndcg_at_10": round(ndcg, 4),
            "mrr_at_10": round(mrr, 4),
            "hit_at_10": round(hit, 4),
            "n": float(n),
        }
    return summary


def aggregate_canary(
    cases: Sequence[Any],
    case_results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Project canary-flagged cases into a per-unit pass-rate summary.

    Returns ``{"overall": {passed, total, rate}, "by_unit": {unit:
    {passed, total, rate}}}``. The ``rate`` value is rounded to four
    decimal places.

    A canary is "passed" when its NDCG@10 score is ≥
    :data:`_CANARY_PASS_THRESHOLD` — strict enough that a chunker
    regression that splits the atomic unit drops the score below the
    bar and the canary fails loudly.

    Cases without ``canary=True`` are skipped, as are unscored cases
    (``score`` is ``None`` after an LLM-judge failure); an empty result is
    returned when no canaries are present so callers can treat zero
    canaries as a separate visible state rather than absorbing it into
    a 100% pass-rate.
    """
    if len(cases) != len(case_results):
        raise ValueError(
            f"canary aggregation requires index-aligned inputs; got "
            f"{len(cases)} cases vs {len(case_results)} case_results. "
            f"fix: pass the BenchmarkSuite.cases + BenchmarkResult.cases "
            f"that the runner emitted in the same loop. "
            f"next: see kairix.quality.benchmark.runner.run_benchmark."
        )

    by_unit: dict[str, list[bool]] = {unit: [] for unit in _CANONICAL_UNITS}
    overall: list[bool] = []
    for case, result in zip(cases, case_results, strict=False):
        if not getattr(case, "canary", False):
            continue
        if result.get("score") is None:
            # Unscored (LLM-judge failure) — neither a pass nor a fail.
            continue
        passed = float(result.get("score", 0.0)) >= _CANARY_PASS_THRESHOLD
        overall.append(passed)
        unit = getattr(case, "canary_unit", None) or "other"
        by_unit.setdefault(unit, []).append(passed)

    def _rate(rows: list[bool]) -> dict[str, float]:
        total = len(rows)
        passed = sum(1 for r in rows if r)
        rate = round(passed / total, 4) if total else 0.0
        return {"passed": float(passed), "total": float(total), "rate": rate}

    by_unit_summary = {unit: _rate(rows) for unit, rows in by_unit.items() if rows}
    return {
        "overall": _rate(overall),
        "by_unit": by_unit_summary,
    }


__all__ = [
    "aggregate_canary",
    "aggregate_per_source_type",
]
