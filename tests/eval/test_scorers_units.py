"""Unit tests for kairix.quality.eval.scorers.

Covers each ScoringStrategy implementation through its public ``.score()`` API:
  - ExactMatchScorer         — top-k case-insensitive substring match
  - FuzzyMatchScorer         — wider top-k relaxed match
  - NDCGScorer               — delegates to ndcg_graded with graded relevance
  - LLMJudgeScorer           — delegates to runner.llm_judge with a chat backend

Tests use FakeChatBackend from tests/fakes.py for the LLM path — no @patch,
no monkeypatch, no internal-private imports. Public surface only.
"""

from __future__ import annotations

import pytest

from kairix.quality.eval.scorers import (
    SCORERS,
    ExactMatchScorer,
    FuzzyMatchScorer,
    LLMJudgeScorer,
    NDCGScorer,
)
from tests.fakes import FakeChatBackend

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ExactMatchScorer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExactMatchScorer:
    @pytest.mark.unit
    def test_score_returns_1_when_gold_title_in_top_k(self) -> None:
        scorer = ExactMatchScorer(top_k=5)
        retrieved = ["04-Agent-Knowledge/builder/patterns.md", "other/file.md"]
        score = scorer.score(retrieved, [{"title": "builder/patterns"}])
        assert score == pytest.approx(1.0)

    @pytest.mark.unit
    def test_score_uses_path_when_title_missing(self) -> None:
        scorer = ExactMatchScorer(top_k=5)
        retrieved = ["docs/alpha.md"]
        # gold has 'path' but no 'title' → ref falls back to gold['path']
        score = scorer.score(retrieved, [{"path": "docs/alpha.md"}])
        assert score == pytest.approx(1.0)

    @pytest.mark.unit
    def test_score_returns_0_when_no_match(self) -> None:
        scorer = ExactMatchScorer(top_k=5)
        retrieved = ["unrelated/path.md"]
        score = scorer.score(retrieved, [{"title": "different.md"}])
        assert score == pytest.approx(0.0)

    @pytest.mark.unit
    def test_score_returns_0_for_empty_gold(self) -> None:
        # Pin the early-return branch: `if not gold: return 0.0`.
        # Sabotage proof: regressing to `return 1.0` would fire this.
        scorer = ExactMatchScorer(top_k=5)
        assert scorer.score(["anything.md"], []) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_score_skips_gold_entries_with_empty_title_and_path(self) -> None:
        # When neither 'title' nor 'path' produces a non-empty ref string,
        # the entry is skipped (the `if ref:` guard). best stays at 0.0.
        scorer = ExactMatchScorer(top_k=5)
        gold = [{"title": "", "path": ""}, {"some_other_field": "ignored"}]
        assert scorer.score(["anything.md"], gold) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_score_takes_max_across_gold_entries(self) -> None:
        # Multiple gold entries — best wins.
        scorer = ExactMatchScorer(top_k=5)
        retrieved = ["docs/alpha.md"]
        gold = [{"title": "missing.md"}, {"title": "docs/alpha.md"}]
        assert scorer.score(retrieved, gold) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# FuzzyMatchScorer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFuzzyMatchScorer:
    @pytest.mark.unit
    def test_score_returns_1_when_gold_in_top_k(self) -> None:
        scorer = FuzzyMatchScorer(top_k=10)
        retrieved = ["docs/alpha.md", "docs/beta.md"]
        score = scorer.score(retrieved, [{"title": "docs/beta.md"}])
        assert score == pytest.approx(1.0)

    @pytest.mark.unit
    def test_score_uses_path_when_title_missing(self) -> None:
        scorer = FuzzyMatchScorer(top_k=10)
        retrieved = ["docs/alpha.md"]
        score = scorer.score(retrieved, [{"path": "docs/alpha.md"}])
        assert score == pytest.approx(1.0)

    @pytest.mark.unit
    def test_score_returns_0_for_empty_gold(self) -> None:
        # Early-return branch: `if not gold: return 0.0`.
        scorer = FuzzyMatchScorer(top_k=10)
        assert scorer.score(["anything.md"], []) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_score_skips_gold_entries_without_ref(self) -> None:
        # 'title' and 'path' both empty → ref is falsy → entry skipped.
        scorer = FuzzyMatchScorer(top_k=10)
        gold = [{"title": "", "path": ""}]
        assert scorer.score(["anything.md"], gold) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# NDCGScorer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNDCGScorer:
    @pytest.mark.unit
    def test_score_delegates_to_ndcg_graded(self) -> None:
        # Perfect ranking: gold path at position 1 with relevance 2.
        scorer = NDCGScorer(k=10)
        retrieved = ["docs/alpha.md"]
        gold = [{"path": "docs/alpha.md", "relevance": 2}]
        # ndcg_graded normalises to ideal — perfect retrieval → 1.0.
        assert scorer.score(retrieved, gold) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_score_returns_0_when_no_relevant_retrieved(self) -> None:
        scorer = NDCGScorer(k=10)
        retrieved = ["x.md", "y.md"]
        gold = [{"path": "docs/alpha.md", "relevance": 2}]
        assert scorer.score(retrieved, gold) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_score_with_empty_gold(self) -> None:
        # ndcg_graded returns 0.0 for empty gold (no ideal ranking).
        scorer = NDCGScorer(k=10)
        assert scorer.score(["a.md"], []) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# LLMJudgeScorer — uses FakeChatBackend, no live API
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMJudgeScorer:
    @pytest.mark.unit
    def test_score_uses_injected_chat_backend(self) -> None:
        """LLMJudgeScorer with a FakeChatBackend returns the parsed score.

        Sabotage proof: regressing to ignore the injected backend (e.g. by
        always calling AzureChatBackend()) would either crash on missing
        credentials or return 0.0 — never the canned 0.8.
        """
        fake = FakeChatBackend(responses=["0.8"])
        scorer = LLMJudgeScorer(chat_backend=fake)

        gold = [{"query": "what is the search pipeline"}]
        score = scorer.score(["search/pipeline.md"], gold)

        assert score == pytest.approx(0.8)
        assert len(fake.calls) == 1, "fake backend must be called exactly once"

    @pytest.mark.unit
    def test_score_with_empty_gold_uses_empty_query(self) -> None:
        # When gold is empty, the implementation passes query="" to llm_judge.
        # llm_judge with no paths returns 0.0; with paths it would call the
        # backend with an empty query. Use canned response to confirm wiring.
        fake = FakeChatBackend(responses=["0.4"])
        scorer = LLMJudgeScorer(chat_backend=fake)

        score = scorer.score(["search/pipeline.md"], [])
        # The implementation still calls the backend even with empty query,
        # so the canned "0.4" is consumed and parsed.
        assert score == pytest.approx(0.4)

    @pytest.mark.unit
    def test_score_with_no_retrieved_paths_returns_zero_without_calling_backend(
        self,
    ) -> None:
        """llm_judge has an early `if not paths: return 0.0` guard.
        Sabotage proof: the fake backend records no calls.
        """
        fake = FakeChatBackend(responses=["1.0"])  # would crash if consumed wrong
        scorer = LLMJudgeScorer(chat_backend=fake)

        score = scorer.score([], [{"query": "anything"}])
        assert score == pytest.approx(0.0)
        assert len(fake.calls) == 0, "no paths → backend must NOT be called"


# ---------------------------------------------------------------------------
# SCORERS registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScorersRegistry:
    @pytest.mark.unit
    def test_registry_contains_all_strategies(self) -> None:
        assert SCORERS["exact"] is ExactMatchScorer
        assert SCORERS["fuzzy"] is FuzzyMatchScorer
        assert SCORERS["ndcg"] is NDCGScorer
        assert SCORERS["llm"] is LLMJudgeScorer
