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

from dataclasses import dataclass, field
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
        from kairix.quality.benchmark.runner import exact_match

        # Score against each gold entry; return max (any match is a hit)
        best = 0.0
        for g in gold:
            ref = g.get("title") or g.get("path", "")
            if ref:
                best = max(best, exact_match(retrieved[: self._top_k], ref))
        return best


class FuzzyMatchScorer:
    """Score 1.0 if gold path appears in top-k retrieved paths (relaxed matching)."""

    def __init__(self, top_k: int = 10) -> None:
        self._top_k = top_k

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        if not gold:
            return 0.0
        from kairix.quality.benchmark.runner import fuzzy_match

        best = 0.0
        for g in gold:
            ref = g.get("title") or g.get("path", "")
            if ref:
                best = max(best, fuzzy_match(retrieved[: self._top_k], ref))
        return best


class NDCGScorer:
    """NDCG@k with graded relevance from gold list."""

    def __init__(self, k: int = 10) -> None:
        self._k = k

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        return ndcg_graded(retrieved, gold, k=self._k)


@dataclass
class LLMJudgeScorer:
    """LLM-based relevance scoring (gpt-4o-mini rates 0.0-1.0).

    Refactored per #204: chat_backend is a non-Optional ``ChatBackend``
    field with a ``default_factory`` wiring the production
    :class:`~kairix.quality.eval.chat_backend.ProviderEvalChatBackend`.
    Tests inject ``LLMJudgeScorer(chat_backend=fake)``; production
    callers leave the kwarg unset and get the live backend wired to the
    configured provider plugin.

    Attributes:
        chat_backend: ``ChatBackend`` protocol implementation.
    """

    chat_backend: ChatBackend = field(default_factory=lambda: _default_chat_backend())

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        from kairix.quality.benchmark.runner import llm_judge

        # LLM judge needs a query — extract from gold context or use empty
        query = ""
        if gold:
            query = gold[0].get("query", "")
        return llm_judge(
            query=query,
            paths=retrieved,
            snippets=[],
            chat_backend=self.chat_backend,
        )


def _default_chat_backend() -> ChatBackend:  # pragma: no cover — prod wrapper; tests pass chat_backend=fake
    """Production ``ChatBackend`` factory — wraps the configured provider plugin.

    Kept as a separate function so the lambda in ``LLMJudgeScorer.chat_backend``
    has a stable, typed default that doesn't import the provider layer
    at module-import time.
    """
    from kairix.quality.eval.chat_backend import ProviderEvalChatBackend

    return ProviderEvalChatBackend.from_config()


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

SCORERS: dict[str, type] = {
    "exact": ExactMatchScorer,
    "fuzzy": FuzzyMatchScorer,
    "ndcg": NDCGScorer,
    "llm": LLMJudgeScorer,
}
