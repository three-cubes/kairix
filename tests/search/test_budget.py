"""
Tests for kairix.core.search.budget — token budget enforcer.

Tests cover:
  - apply_budget() returns [] on empty results
  - apply_budget() returns [] on zero budget
  - apply_budget() Phase 1 (no summaries_db): all results get L2 tier
  - apply_budget() truncates content when a single result exceeds budget
  - apply_budget() stops adding results when budget exhausted
  - _open_summaries_db() returns None when path does not exist
  - _open_summaries_db() returns connection when path exists
  - _open_summaries_db() returns None when sqlite3.connect raises
  - _select_tier() Phase 1 (summaries_db=None) always returns L2
  - _select_tier() Phase 2 score/budget thresholds: L0, L1, L2 paths
  - _get_content_for_tier() Phase 1 returns snippet
  - _get_content_for_tier() Phase 2 L0 returns abstract when available
  - _get_content_for_tier() Phase 2 L1 falls back to L0 when L1 unavailable
  - apply_budget() unexpected error returns []
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kairix.core.search.budget import (
    L1_BUDGET_MIN,
    L1_SCORE_THRESHOLD,
    L2_BUDGET_MIN,
    L2_SCORE_THRESHOLD,
    BudgetedResult,
    _get_content_for_tier,
    _open_summaries_db,
    _select_tier,
    apply_budget,
)
from kairix.core.search.rrf import FusedResult
from kairix.text import strip_frontmatter
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
# apply_budget() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyBudget:
    @pytest.mark.unit
    def test_empty_results_returns_empty(self) -> None:
        assert apply_budget([], budget=3000) == []

    @pytest.mark.unit
    def test_zero_budget_returns_empty(self) -> None:
        results = [_fused()]
        assert apply_budget(results, budget=0) == []

    @pytest.mark.unit
    def test_negative_budget_returns_empty(self) -> None:
        results = [_fused()]
        assert apply_budget(results, budget=-1) == []

    @pytest.mark.unit
    def test_phase1_all_results_l2(self) -> None:
        """With no summaries DB, all results should be L2 tier."""
        results = [_fused(f"doc{i}.md", snippet="abc " * 10) for i in range(3)]
        budgeted = apply_budget(results, budget=10_000)
        assert all(r.tier == "L2" for r in budgeted)

    @pytest.mark.unit
    def test_returns_budgeted_result_type(self) -> None:
        results = [_fused()]
        budgeted = apply_budget(results, budget=10_000)
        assert len(budgeted) == 1
        assert isinstance(budgeted[0], BudgetedResult)

    @pytest.mark.unit
    def test_truncates_content_when_exceeds_budget(self) -> None:
        """A single large result should be truncated to fit the budget."""
        # 200 words * 1.3 = 260 tokens. Budget = 50 → truncate.
        large_snippet = " ".join(["word"] * 200)
        results = [_fused(snippet=large_snippet)]
        budgeted = apply_budget(results, budget=50)
        assert len(budgeted) == 1
        # Content should be shorter than original
        assert len(budgeted[0].content) < len(large_snippet)
        # Allow small rounding tolerance between char-based truncation and word-based estimation
        assert budgeted[0].token_estimate <= 60

    @pytest.mark.unit
    def test_stops_when_budget_exhausted(self) -> None:
        """Should stop adding results once budget is used up."""
        # Each snippet is ~500 chars → ~125 tokens
        snippet = "word " * 100  # 500 chars
        results = [_fused(f"doc{i}.md", snippet=snippet) for i in range(10)]
        budgeted = apply_budget(results, budget=200)
        # Should get fewer than 10 results
        assert len(budgeted) < 10
        # Total tokens should not exceed budget (allowing 1 partial result)
        total = sum(r.token_estimate for r in budgeted)
        assert total <= 200 + 50  # allow for rounding

    @pytest.mark.unit
    def test_unexpected_exception_returns_empty(self) -> None:
        """``apply_budget`` swallows internal exceptions and returns [].

        Drives the exception via a ``FusedResult`` whose ``.snippet`` attribute
        access raises — exercising the outer ``except Exception`` branch
        without monkey-patching ``_apply_budget_impl``.
        """

        class _ExplodingResult:
            path = "x.md"
            collection = "c"
            title = "T"
            rrf_score = 0.5
            boosted_score = 0.5

            @property
            def snippet(self) -> str:
                raise RuntimeError("boom on snippet access")

        result = apply_budget([_ExplodingResult()], budget=3000)  # type: ignore[list-item]
        assert result == []


# ---------------------------------------------------------------------------
# _open_summaries_db() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenSummariesDb:
    @pytest.mark.unit
    def test_returns_none_when_explicit_db_path_does_not_exist(self, tmp_path: Path) -> None:
        """A non-existent ``db_path=`` resolves the ``not db_path.exists()`` branch to None."""
        result = _open_summaries_db(db_path=tmp_path / "no-such-summaries.sqlite")
        assert result is None

    @pytest.mark.unit
    def test_returns_open_connection_when_explicit_db_path_exists(self, tmp_path: Path) -> None:
        """A real SQLite file at ``db_path=`` returns an open connection.

        Asserts the connection is actually usable (executes a trivial query)
        and points at the path we provided — not a smoke "either-way" check.
        """
        db_path = tmp_path / "summaries.sqlite"
        # Create a real summaries-shaped DB so the count query in the warning
        # branch succeeds (won't be hit on first call because _summaries_warned
        # is module-state, but this still proves the connection is real).
        seed = sqlite3.connect(str(db_path))
        seed.execute("CREATE TABLE summaries (path TEXT PRIMARY KEY, l0 TEXT, l1 TEXT)")
        seed.commit()
        seed.close()

        conn = _open_summaries_db(db_path=db_path)
        assert conn is not None, "expected a connection for an existing DB"
        try:
            # The connection points at the right database — we can read the
            # ``summaries`` table we just created.
            row = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    @pytest.mark.unit
    def test_returns_none_when_path_exists_but_is_not_a_database(self, tmp_path: Path) -> None:
        """A path that exists but isn't a SQLite DB → ``sqlite3.connect`` opens it
        but the subsequent query raises, surfacing as None via the broad ``except``.
        """
        # Write a non-database file at the path. sqlite3.connect succeeds (the
        # file exists), but the ``COUNT(*) FROM summaries`` query inside the
        # warning branch raises, which is caught by the outer except.
        bad = tmp_path / "not-a-db.txt"
        bad.write_bytes(b"this is not a sqlite database")

        # Reset module-state for this test so the warning branch (which would
        # otherwise short-circuit) actually executes the query.
        import kairix.core.search.budget as _budget_mod

        _budget_mod._summaries_warned = False
        try:
            result = _open_summaries_db(db_path=bad)
        finally:
            _budget_mod._summaries_warned = False
        assert result is None


# ---------------------------------------------------------------------------
# _select_tier() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSelectTier:
    @pytest.mark.unit
    def test_phase1_always_l2(self) -> None:
        """No summaries_db → always L2, regardless of score or budget."""
        result = _fused(score=0.0)
        assert _select_tier(result, 100, L1_SCORE_THRESHOLD, L2_SCORE_THRESHOLD, None) == "L2"
        assert _select_tier(result, 10_000, L1_SCORE_THRESHOLD, L2_SCORE_THRESHOLD, None) == "L2"

    @pytest.mark.unit
    def test_phase2_l2_when_high_score_high_budget(self) -> None:
        """High score + budget ≥ L2_BUDGET_MIN → L2."""
        result = _fused(score=L2_SCORE_THRESHOLD + 0.1)
        db = MagicMock()
        tier = _select_tier(result, L2_BUDGET_MIN, L1_SCORE_THRESHOLD, L2_SCORE_THRESHOLD, db)
        assert tier == "L2"

    @pytest.mark.unit
    def test_phase2_l1_when_medium_score_medium_budget(self) -> None:
        """Medium score + budget ≥ L1_BUDGET_MIN but < L2_BUDGET_MIN → L1."""
        result = _fused(score=L1_SCORE_THRESHOLD + 0.01)
        db = MagicMock()
        # Budget between L1_BUDGET_MIN and L2_BUDGET_MIN, score below L2 threshold
        budget = L1_BUDGET_MIN
        tier = _select_tier(
            result,
            budget,
            L1_SCORE_THRESHOLD,
            L2_SCORE_THRESHOLD + 1.0,
            db,  # raise L2 threshold
        )
        assert tier == "L1"

    @pytest.mark.unit
    def test_phase2_l0_when_low_score(self) -> None:
        """Low score → L0 regardless of budget."""
        result = _fused(score=0.0)
        db = MagicMock()
        tier = _select_tier(result, L2_BUDGET_MIN, L1_SCORE_THRESHOLD, L2_SCORE_THRESHOLD, db)
        assert tier == "L0"

    @pytest.mark.unit
    def test_phase2_l0_when_low_budget(self) -> None:
        """Very low budget → L0 even if score is high."""
        result = _fused(score=1.0)
        db = MagicMock()
        tier = _select_tier(result, 10, L1_SCORE_THRESHOLD, L2_SCORE_THRESHOLD, db)
        assert tier == "L0"


# ---------------------------------------------------------------------------
# _get_content_for_tier() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetContentForTier:
    @pytest.mark.unit
    def test_phase1_returns_snippet(self) -> None:
        """No summaries_db → return result.snippet."""
        result = _fused(snippet="the snippet content")
        content = _get_content_for_tier(result, "L2", summaries_db=None)
        assert content == "the snippet content"

    @pytest.mark.unit
    def test_phase1_empty_snippet_returns_empty(self) -> None:
        result = FusedResult(
            path="x.md",
            collection="c",
            title="T",
            snippet="",
            rrf_score=0.5,
            boosted_score=0.5,
        )
        content = _get_content_for_tier(result, "L2", summaries_db=None)
        assert content == ""

    @pytest.mark.unit
    def test_phase2_l0_returns_abstract_when_loader_provides_one(self) -> None:
        """``loader.get_l0`` hit → that abstract is returned.

        Asserts the loader was called exactly once with the expected path,
        proving the L0 branch was actually taken (not a coincidence of an
        ``isinstance(content, str)`` weak assertion).
        """
        result = _fused(path="docs/x.md", snippet="fallback snippet")
        loader = FakeSummaryLoader(l0_by_path={"docs/x.md": "abstract text"})
        content = _get_content_for_tier(result, "L0", summaries_db=MagicMock(), loader=loader)
        assert content == "abstract text"
        assert loader.l0_calls == ["docs/x.md"]
        assert loader.l1_calls == []

    @pytest.mark.unit
    def test_phase2_l0_falls_back_to_snippet_when_loader_returns_none(self) -> None:
        """L0 with no abstract → fall through to ``result.snippet`` (frontmatter-stripped)."""
        result = _fused(path="docs/x.md", snippet="fallback snippet")
        loader = FakeSummaryLoader()  # no l0/l1 configured → both return None
        content = _get_content_for_tier(result, "L0", summaries_db=MagicMock(), loader=loader)
        assert content == "fallback snippet"
        # Loader was queried for L0; not for L1 (the L0 branch doesn't fall back to L1).
        assert loader.l0_calls == ["docs/x.md"]
        assert loader.l1_calls == []

    @pytest.mark.unit
    def test_phase2_l1_falls_back_to_l0_when_l1_unavailable(self) -> None:
        """L1 with no overview → loader is queried for L0 next, and that abstract is returned."""
        result = _fused(path="docs/x.md", snippet="fallback snippet")
        loader = FakeSummaryLoader(l0_by_path={"docs/x.md": "l0 abstract"})
        content = _get_content_for_tier(result, "L1", summaries_db=MagicMock(), loader=loader)
        assert content == "l0 abstract"
        # Both calls happen in order: L1 first (miss), then L0 (hit).
        assert loader.l1_calls == ["docs/x.md"]
        assert loader.l0_calls == ["docs/x.md"]

    @pytest.mark.unit
    def test_phase2_l1_returns_l1_overview_when_available(self) -> None:
        """L1 hit → L1 overview is returned and L0 is NOT queried."""
        result = _fused(path="docs/x.md", snippet="fallback snippet")
        loader = FakeSummaryLoader(
            l0_by_path={"docs/x.md": "l0 abstract"},
            l1_by_path={"docs/x.md": "l1 overview"},
        )
        content = _get_content_for_tier(result, "L1", summaries_db=MagicMock(), loader=loader)
        assert content == "l1 overview"
        assert loader.l1_calls == ["docs/x.md"]
        # L0 is not consulted when L1 is found — proves the early-return branch fired.
        assert loader.l0_calls == []

    @pytest.mark.unit
    def test_phase2_exception_in_loader_falls_back_to_snippet(self) -> None:
        """A raising loader → caught by the outer ``except Exception`` → snippet fallback."""
        result = _fused(snippet="safe fallback")
        loader = FakeSummaryLoader(raises=RuntimeError("loader DB error"))
        content = _get_content_for_tier(result, "L0", summaries_db=MagicMock(), loader=loader)
        assert content == "safe fallback"

    @pytest.mark.unit
    def test_phase1_strips_yaml_frontmatter_from_snippet(self) -> None:
        """S18-16: snippets with YAML frontmatter should have it stripped."""
        snippet = "---\ntitle: Test Doc\ntype: note\n---\n\nActual content here."
        result = _fused(snippet=snippet)
        content = _get_content_for_tier(result, "L2", summaries_db=None)
        assert "---" not in content
        assert "title:" not in content
        assert "Actual content" in content

    @pytest.mark.unit
    def test_phase1_preserves_snippet_without_frontmatter(self) -> None:
        """S18-16: snippets without frontmatter are returned unchanged."""
        snippet = "Just normal content here."
        result = _fused(snippet=snippet)
        content = _get_content_for_tier(result, "L2", summaries_db=None)
        assert content == snippet


# ---------------------------------------------------------------------------
# strip_frontmatter() tests (kairix.text utility)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStripFrontmatter:
    @pytest.mark.unit
    def test_snippet_excludes_yaml_frontmatter(self) -> None:
        """S18-16: YAML frontmatter block is stripped from text."""
        text = "---\ntitle: Test Doc\ntype: note\n---\n\nActual content here."
        stripped = strip_frontmatter(text)
        assert "---" not in stripped
        assert "title:" not in stripped
        assert "Actual content" in stripped

    @pytest.mark.unit
    def test_no_frontmatter_unchanged(self) -> None:
        text = "No frontmatter here, just content."
        assert strip_frontmatter(text) == text

    @pytest.mark.unit
    def test_empty_string(self) -> None:
        assert strip_frontmatter("") == ""

    @pytest.mark.unit
    def test_mid_text_dashes_not_stripped(self) -> None:
        """Dashes in the middle of text are not treated as frontmatter."""
        text = "Some text\n---\nnot frontmatter\n---\nmore text"
        assert strip_frontmatter(text) == text


# ---------------------------------------------------------------------------
# _estimate_tokens — empty-string guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_tokens_returns_zero_for_empty_string() -> None:
    """Empty / falsy text short-circuits to 0 — caller doesn't need to guard."""
    from kairix.core.search.budget import _estimate_tokens

    assert _estimate_tokens("") == 0


@pytest.mark.unit
def test_estimate_tokens_returns_positive_for_non_empty_string() -> None:
    """Non-empty text delegates to the canonical word-based estimator and returns >0."""
    from kairix.core.search.budget import _estimate_tokens

    n = _estimate_tokens("the quick brown fox jumps over the lazy dog")
    assert n > 0
