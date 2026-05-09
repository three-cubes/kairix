"""
Strategy pattern implementations for retrieval scoring.

Wraps existing scoring functions from the benchmark runner and metrics module
as ScoringStrategy protocol implementations. No logic duplication — delegates
to the existing functions.

Registry:
    SCORERS maps scorer names to their classes for dynamic lookup by the
    benchmark runner and configuration system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairix.quality.eval.metrics import ndcg_graded

if TYPE_CHECKING:
    from kairix.core.protocols import ChatBackend


class ExactMatchScorer:
    """Score 1.0 if gold path appears in top-k retrieved paths (case-insensitive substring)."""

    def __init__(self, top_k: int = 5) -> None:
        self._top_k = top_k

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        if not gold:
            return 0.0
        from kairix.quality.benchmark.runner import _exact_match

        # Score against each gold entry; return max (any match is a hit)
        best = 0.0
        for g in gold:
            ref = g.get("title") or g.get("path", "")
            if ref:
                best = max(best, _exact_match(retrieved[: self._top_k], ref))
        return best


class FuzzyMatchScorer:
    """Score 1.0 if gold path appears in top-k retrieved paths (relaxed matching)."""

    def __init__(self, top_k: int = 10) -> None:
        self._top_k = top_k

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        if not gold:
            return 0.0
        from kairix.quality.benchmark.runner import _fuzzy_match

        best = 0.0
        for g in gold:
            ref = g.get("title") or g.get("path", "")
            if ref:
                best = max(best, _fuzzy_match(retrieved[: self._top_k], ref))
        return best


class NDCGScorer:
    """NDCG@k with graded relevance from gold list."""

    def __init__(self, k: int = 10) -> None:
        self._k = k

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        return ndcg_graded(retrieved, gold, k=self._k)


class LLMJudgeScorer:
    """LLM-based relevance scoring (gpt-4o-mini rates 0.0-1.0).

    Args:
        chat_backend: ``ChatBackend`` protocol implementation. Defaults to
                      ``AzureChatBackend`` constructed lazily inside ``_llm_judge``.
    """

    def __init__(self, chat_backend: ChatBackend | None = None) -> None:
        self._chat_backend = chat_backend

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        from kairix.quality.benchmark.runner import _llm_judge

        # LLM judge needs a query — extract from gold context or use empty
        query = ""
        if gold:
            query = gold[0].get("query", "")
        return _llm_judge(
            query=query,
            paths=retrieved,
            snippets=[],
            chat_backend=self._chat_backend,
        )


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

SCORERS: dict[str, type] = {
    "exact": ExactMatchScorer,
    "fuzzy": FuzzyMatchScorer,
    "ndcg": NDCGScorer,
    "llm": LLMJudgeScorer,
}
