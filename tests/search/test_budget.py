"""
Tests for kairix.core.search.budget — token budget enforcer.

Every test drives behaviour through the public ``apply_budget`` callable.
Phase 1 paths run with the default ``summary_loader=None``; Phase 2 paths
inject ``FakeSummaryLoader`` from ``tests/fakes.py``. No private helpers
(``_select_tier``, ``_get_content_for_tier``, ``_open_summaries_db``) are
imported or called directly.
"""

from __future__ import annotations

import pytest

from kairix.core.search.budget import (
    L1_BUDGET_MIN,
    L1_SCORE_THRESHOLD,
    L2_BUDGET_MIN,
    L2_SCORE_THRESHOLD,
    BudgetedResult,
    apply_budget,
)
from kairix.core.search.rrf import FusedResult
from tests.fakes import FakeSummaryLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fused(path: str = "doc.md", snippet: str = "snippet text", score: float = 0.5) -> FusedResult:
    return FusedResult(
        path=path,
        collection="vault-areas",
        title="Test Doc",
        snippet=snippet,
        rrf_score=score,
        boosted_score=score,
    )


# ---------------------------------------------------------------------------
# Phase 1 — no summary_loader: tier always L2, snippet content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyBudgetPhase1:
    def test_empty_results_returns_empty(self) -> None:
        assert apply_budget([], budget=3000) == []

    def test_zero_budget_returns_empty(self) -> None:
        assert apply_budget([_fused()], budget=0) == []

    def test_negative_budget_returns_empty(self) -> None:
        assert apply_budget([_fused()], budget=-1) == []

    def test_returns_budgeted_result_type(self) -> None:
        budgeted = apply_budget([_fused()], budget=10_000)
        assert len(budgeted) == 1
        assert isinstance(budgeted[0], BudgetedResult)

    def test_all_results_get_l2_tier_regardless_of_score_or_budget(self) -> None:
        """Phase 1 short-circuit: tier is L2 for every result, even low scores / tight budgets."""
        results = [
            _fused("low.md", snippet="abc", score=0.0),
            _fused("mid.md", snippet="def", score=L1_SCORE_THRESHOLD + 0.01),
            _fused("high.md", snippet="ghi", score=0.9),
        ]
        # Use a tight budget so a Phase 2 selector would have demoted to L0.
        budgeted = apply_budget(results, budget=L1_BUDGET_MIN - 100)
        assert [b.tier for b in budgeted] == ["L2", "L2", "L2"]

    def test_content_is_the_snippet_when_no_loader(self) -> None:
        """No loader → content is the original snippet (frontmatter-stripped)."""
        budgeted = apply_budget([_fused(snippet="the snippet content")], budget=10_000)
        assert budgeted[0].content == "the snippet content"

    def test_empty_snippet_returns_empty_content(self) -> None:
        budgeted = apply_budget([_fused(snippet="")], budget=10_000)
        assert budgeted[0].content == ""
        assert budgeted[0].token_estimate == 0

    def test_yaml_frontmatter_is_stripped_from_content(self) -> None:
        snippet = "---\ntitle: Test Doc\ntype: note\n---\n\nActual content here."
        budgeted = apply_budget([_fused(snippet=snippet)], budget=10_000)
        content = budgeted[0].content
        assert "---" not in content
        assert "title:" not in content
        assert "Actual content here." in content

    def test_snippet_without_frontmatter_is_returned_unchanged(self) -> None:
        snippet = "Just normal content here."
        budgeted = apply_budget([_fused(snippet=snippet)], budget=10_000)
        assert budgeted[0].content == snippet

    def test_truncates_content_when_single_result_exceeds_budget(self) -> None:
        """A single oversize result is truncated by char-multiplied budget, then re-tokenised."""
        large_snippet = " ".join(["word"] * 200)  # ~1000 chars → ~260 tokens
        budgeted = apply_budget([_fused(snippet=large_snippet)], budget=50)
        assert len(budgeted) == 1
        assert len(budgeted[0].content) < len(large_snippet)
        # Allow a small rounding tolerance between char-based truncation and word-based estimation.
        assert budgeted[0].token_estimate <= 60

    def test_stops_appending_results_once_budget_exhausted(self) -> None:
        """remaining <= 0 breaks the loop, so the trailing results are dropped."""
        snippet = "word " * 100  # ~500 chars → ~125 tokens
        results = [_fused(f"doc{i}.md", snippet=snippet) for i in range(10)]
        budgeted = apply_budget(results, budget=200)
        assert len(budgeted) < 10
        total = sum(b.token_estimate for b in budgeted)
        assert total <= 200 + 50  # rounding tolerance

    def test_unexpected_exception_returns_empty_list(self) -> None:
        """Any internal exception (here: ``.snippet`` access raises) is swallowed → []."""

        class _ExplodingResult:
            path = "x.md"
            collection = "c"
            title = "T"
            rrf_score = 0.5
            boosted_score = 0.5

            @property
            def snippet(self) -> str:
                raise RuntimeError("boom on snippet access")

        result = apply_budget([_ExplodingResult()], budget=3000)  # type: ignore[list-item]  # deliberate structural duck-type to exercise except branch
        assert result == []


# ---------------------------------------------------------------------------
# Phase 2 — summary_loader injected: tier varies by score/budget; loader
# delivers L0 abstract / L1 overview, with snippet fallback.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyBudgetPhase2Tiering:
    """Tier selection driven through ``apply_budget(summary_loader=...)``."""

    def test_high_score_high_budget_selects_l2_and_does_not_query_loader(self) -> None:
        """L2 path: score ≥ l2_threshold and remaining ≥ L2_BUDGET_MIN."""
        loader = FakeSummaryLoader()
        budgeted = apply_budget(
            [_fused(score=L2_SCORE_THRESHOLD + 0.1)],
            budget=L2_BUDGET_MIN,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L2"
        # L2 returns the snippet directly — proves the loader was NOT consulted.
        assert loader.l0_calls == []
        assert loader.l1_calls == []
        assert budgeted[0].content == "snippet text"

    def test_medium_score_medium_budget_selects_l1_and_queries_l1_only(self) -> None:
        """L1 path: l1_threshold ≤ score < l2_threshold AND L1_BUDGET_MIN ≤ budget < L2_BUDGET_MIN."""
        loader = FakeSummaryLoader(l1_by_path={"doc.md": "l1 overview"})
        budgeted = apply_budget(
            [_fused(score=L1_SCORE_THRESHOLD + 0.01)],
            budget=L1_BUDGET_MIN,  # ≥ L1_BUDGET_MIN, < L2_BUDGET_MIN
            l2_threshold=L2_SCORE_THRESHOLD + 1.0,  # raise the L2 bar so we land in L1
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L1"
        assert budgeted[0].content == "l1 overview"
        # L1 hit short-circuits before querying L0.
        assert loader.l1_calls == ["doc.md"]
        assert loader.l0_calls == []

    def test_low_score_with_high_budget_selects_l0(self) -> None:
        """L0 path: score below l1_threshold even with plenty of budget."""
        loader = FakeSummaryLoader(l0_by_path={"doc.md": "l0 abstract"})
        budgeted = apply_budget(
            [_fused(score=0.0)],
            budget=L2_BUDGET_MIN,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L0"
        assert budgeted[0].content == "l0 abstract"
        assert loader.l0_calls == ["doc.md"]
        assert loader.l1_calls == []

    def test_low_budget_with_high_score_selects_l0(self) -> None:
        """L0 path: tight budget demotes even a perfect-score result."""
        loader = FakeSummaryLoader(l0_by_path={"doc.md": "l0 abstract"})
        budgeted = apply_budget(
            [_fused(score=1.0, snippet="x" * 200)],
            budget=L1_BUDGET_MIN - 1,  # below L1_BUDGET_MIN
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L0"
        assert budgeted[0].content == "l0 abstract"


@pytest.mark.unit
class TestApplyBudgetPhase2Content:
    """Content selection driven through ``apply_budget(summary_loader=...)``."""

    def test_l0_falls_back_to_snippet_when_loader_has_no_abstract(self) -> None:
        """L0 with no abstract → snippet (frontmatter-stripped)."""
        loader = FakeSummaryLoader()  # no l0/l1 configured
        budgeted = apply_budget(
            [_fused(score=0.0, snippet="fallback snippet")],
            budget=L2_BUDGET_MIN,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L0"
        assert budgeted[0].content == "fallback snippet"
        # L0 attempted; L1 not consulted (the L0 branch doesn't fall back to L1).
        assert loader.l0_calls == ["doc.md"]
        assert loader.l1_calls == []

    def test_l1_falls_back_to_l0_when_l1_overview_unavailable(self) -> None:
        """L1 miss → loader is queried for L0 next; that abstract is returned."""
        loader = FakeSummaryLoader(l0_by_path={"doc.md": "l0 abstract"})
        budgeted = apply_budget(
            [_fused(score=L1_SCORE_THRESHOLD + 0.01)],
            budget=L1_BUDGET_MIN,
            l2_threshold=L2_SCORE_THRESHOLD + 1.0,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L1"
        assert budgeted[0].content == "l0 abstract"
        # Both calls happen in order: L1 (miss), then L0 (hit).
        assert loader.l1_calls == ["doc.md"]
        assert loader.l0_calls == ["doc.md"]

    def test_l1_miss_and_l0_miss_falls_back_to_snippet(self) -> None:
        """L1 miss → L0 miss → snippet."""
        loader = FakeSummaryLoader()
        budgeted = apply_budget(
            [_fused(score=L1_SCORE_THRESHOLD + 0.01, snippet="from snippet")],
            budget=L1_BUDGET_MIN,
            l2_threshold=L2_SCORE_THRESHOLD + 1.0,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L1"
        assert budgeted[0].content == "from snippet"
        assert loader.l1_calls == ["doc.md"]
        assert loader.l0_calls == ["doc.md"]

    def test_loader_exception_in_l0_falls_back_to_snippet(self) -> None:
        """A raising loader → caught by the inner try/except → snippet fallback."""
        loader = FakeSummaryLoader(raises=RuntimeError("loader DB error"))
        budgeted = apply_budget(
            [_fused(score=0.0, snippet="safe fallback")],
            budget=L2_BUDGET_MIN,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L0"
        assert budgeted[0].content == "safe fallback"

    def test_loader_exception_in_l1_falls_back_to_snippet(self) -> None:
        """A raising loader on the L1 path also falls back to the snippet."""
        loader = FakeSummaryLoader(raises=RuntimeError("loader DB error"))
        budgeted = apply_budget(
            [_fused(score=L1_SCORE_THRESHOLD + 0.01, snippet="safe fallback")],
            budget=L1_BUDGET_MIN,
            l2_threshold=L2_SCORE_THRESHOLD + 1.0,
            summary_loader=loader,
        )
        assert budgeted[0].tier == "L1"
        assert budgeted[0].content == "safe fallback"
