"""
Tests for kairix.quality.benchmark.runner — covers previously-untested paths:
- exact_match(): gold path matching variants
- fuzzy_match(): partial path matching
- classification_score(): rule classifier integration
- llm_judge(): API call mocked + error paths
- score_tier(): tier labels
- _category_diagnosis(): diagnostic strings
- format_interpretation(): output structure
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.quality.benchmark.runner import (
    BenchmarkResult,
    classification_score,
    exact_match,
    format_interpretation,
    fuzzy_match,
    llm_judge,
    score_tier,
    title_in_retrieved,
)
from kairix.quality.eval.metrics import (
    dcg,
    hit_at_k_graded,
    ideal_dcg_graded,
    match_gold_to_path,
    ndcg_graded,
    reciprocal_rank_graded,
)

# ---------------------------------------------------------------------------
# _exact_match
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_match_returns_1_for_direct_match() -> None:
    paths = ["04-Agent-Knowledge/builder/patterns.md", "some/other/doc.md"]
    assert exact_match(paths, "04-Agent-Knowledge/builder/patterns.md") == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_match_returns_1_for_substring_match() -> None:
    paths = ["04-Agent-Knowledge/builder/patterns.md"]
    assert exact_match(paths, "builder/patterns") == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_match_returns_0_when_no_match() -> None:
    paths = ["some/unrelated/doc.md"]
    assert exact_match(paths, "builder/patterns.md") == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_returns_0_for_empty_gold() -> None:
    assert exact_match(["any/path.md"], "") == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_returns_0_for_empty_paths() -> None:
    assert exact_match([], "builder/patterns.md") == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_is_case_insensitive() -> None:
    paths = ["04-Agent-Knowledge/Builder/Patterns.md"]
    assert exact_match(paths, "builder/patterns.md") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _fuzzy_match
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fuzzy_match_returns_1_for_suffix_match() -> None:
    paths = ["04-Agent-Knowledge/entities/jordan-blake.md"]
    assert fuzzy_match(paths, "entities/jordan-blake.md") == pytest.approx(1.0)


@pytest.mark.unit
def test_fuzzy_match_returns_0_for_no_match() -> None:
    paths = ["totally/unrelated/file.md"]
    assert fuzzy_match(paths, "entities/jordan-blake.md") == pytest.approx(0.0)


@pytest.mark.unit
def test_fuzzy_match_returns_0_for_empty_gold() -> None:
    assert fuzzy_match(["any/path.md"], "") == pytest.approx(0.0)


@pytest.mark.unit
def test_fuzzy_match_respects_topk_limit() -> None:
    # gold is in position 11 (0-indexed), beyond top-10
    paths = [f"unrelated/{i}.md" for i in range(10)] + ["04-Agent-Knowledge/entities/target.md"]
    assert fuzzy_match(paths, "entities/target.md") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _classification_score
# ---------------------------------------------------------------------------


@dataclass
class _FakeClassifyResult:
    type: str


@pytest.mark.unit
def test_classification_score_returns_1_when_rules_classifier_matches_expected() -> None:
    """Returns 1.0 when the injected classifier's rules step returns the expected type."""
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_type="decision")
    score = classification_score("We decided to use PostgreSQL.", "decision", classifier=classifier)
    assert score == pytest.approx(1.0)
    # Rules step ran exactly once with agent="shared"; LLM fallback was NOT consulted.
    assert classifier.rules_calls == [{"query": "We decided to use PostgreSQL.", "agent": "shared"}]
    assert classifier.llm_calls == []


@pytest.mark.unit
def test_classification_score_returns_0_when_rules_returns_different_type() -> None:
    """Returns 0.0 when the rules step returns a non-matching, non-unknown type."""
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_type="pattern")
    score = classification_score("We decided to use PostgreSQL.", "decision", classifier=classifier)
    assert score == pytest.approx(0.0)
    # Rules result wasn't 'unknown' so LLM fallback is skipped.
    assert classifier.llm_calls == []


@pytest.mark.unit
def test_classification_score_returns_0_when_classifier_raises() -> None:
    """When the classifier raises, the score is 0.0 and no exception propagates."""
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_raises=RuntimeError("oops"))
    score = classification_score("anything", "decision", classifier=classifier)
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_classification_score_falls_back_to_llm_when_rules_returns_unknown() -> None:
    """When rules returns 'unknown' the LLM step decides; on match the score is 1.0."""
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_type="unknown", llm_type="decision")
    score = classification_score("We decided to use PostgreSQL.", "decision", classifier=classifier)
    assert score == pytest.approx(1.0)
    # Both steps were consulted, with the rules step first.
    assert len(classifier.rules_calls) == 1
    assert len(classifier.llm_calls) == 1


@pytest.mark.unit
def test_classification_score_returns_0_when_llm_fallback_also_misses() -> None:
    """When rules returns 'unknown' and the LLM also returns a different type, score is 0.0."""
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_type="unknown", llm_type="pattern")
    score = classification_score("anything", "decision", classifier=classifier)
    assert score == pytest.approx(0.0)
    assert len(classifier.llm_calls) == 1


# ---------------------------------------------------------------------------
# _llm_judge — DI via FakeChatBackend (replaces the legacy chat_fn= substitution)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_judge_returns_score_from_chat_backend() -> None:
    """The chat backend's response is parsed as a float and returned."""
    from tests.fakes import FakeChatBackend

    backend = FakeChatBackend(responses=["0.8"])
    score = llm_judge(
        query="what are our engineering patterns?",
        paths=["04-Agent-Knowledge/builder/patterns.md"],
        snippets=["Engineering patterns for Builder"],
        chat_backend=backend,
    )
    assert score == pytest.approx(0.8)
    # The backend was called exactly once and the prompt named the query.
    assert len(backend.calls) == 1
    assert "what are our engineering patterns" in backend.calls[0]["prompt"]


@pytest.mark.unit
def test_llm_judge_clamps_score_to_unit_interval() -> None:
    """Backend returning 1.5 clamps to 1.0; -0.3 clamps to 0.0."""
    from tests.fakes import FakeChatBackend

    high = llm_judge("q", ["p.md"], ["s"], chat_backend=FakeChatBackend(responses=["1.5"]))
    assert high == pytest.approx(1.0)
    low = llm_judge("q", ["p.md"], ["s"], chat_backend=FakeChatBackend(responses=["-0.3"]))
    assert low == pytest.approx(0.0)


@pytest.mark.unit
def test_llm_judge_returns_0_when_chat_backend_raises() -> None:
    """Backend raises → returns 0.0 (never propagates)."""
    from tests.fakes import FakeChatBackend

    backend = FakeChatBackend(raise_on_call=OSError("timeout"))
    score = llm_judge("q", ["p.md"], ["s"], chat_backend=backend)
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_llm_judge_returns_0_for_empty_paths_without_calling_backend() -> None:
    """Empty paths short-circuits — the chat backend is never invoked."""
    from tests.fakes import FakeChatBackend

    backend = FakeChatBackend(responses=[])  # would IndexError if called
    score = llm_judge("q", [], [], chat_backend=backend)
    assert score == pytest.approx(0.0)
    assert len(backend.calls) == 0


@pytest.mark.unit
def test_llm_judge_returns_0_when_response_not_parseable_as_float() -> None:
    """Non-numeric backend response → returns 0.0 (the float() raises ValueError)."""
    from tests.fakes import FakeChatBackend

    backend = FakeChatBackend(responses=["not a number"])
    score = llm_judge("q", ["p.md"], ["s"], chat_backend=backend)
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# score_tier
# ---------------------------------------------------------------------------


@pytest.mark.unit
def testscore_tier_production() -> None:
    assert "Production" in score_tier(0.762)


@pytest.mark.unit
def testscore_tier_stable() -> None:
    assert "Phase 2" in score_tier(0.69)


@pytest.mark.unit
def testscore_tier_developing() -> None:
    assert "BM25" in score_tier(0.61)


@pytest.mark.unit
def testscore_tier_needs_work() -> None:
    assert "BM25" in score_tier(0.45)


# ---------------------------------------------------------------------------
# Category-diagnosis output — observed via format_interpretation, which
# embeds the diagnosis line for each category in the rendered report.
# ---------------------------------------------------------------------------


def _result_with_category_score(category: str, score: float) -> BenchmarkResult:
    """Build a minimal BenchmarkResult with one category at ``score`` and others at 1.0."""
    cat_scores = {"recall": 1.0, "temporal": 1.0, "entity": 1.0, "conceptual": 1.0, "multi_hop": 1.0, "procedural": 1.0}
    cat_scores[category] = score
    return BenchmarkResult(
        meta={"suite_name": "t", "system": "hybrid", "date": "2026-05-09", "n_cases": 6},
        summary={"weighted_total": 0.7, "category_scores": cat_scores, "gates": {"phase1": True}},
        diagnostics={"category_counts": dict.fromkeys(cat_scores, 1)},
        cases=[],
    )


@pytest.mark.unit
def test_format_interpretation_emits_category_specific_diagnosis_for_low_temporal() -> None:
    """A low temporal score surfaces a temporal-aware diagnosis line in the report."""
    output = format_interpretation(_result_with_category_score("temporal", 0.3)).lower()
    assert "temporal" in output
    # The temporal-low diagnosis mentions date-aware chunking.
    assert "date" in output or "chunking" in output


@pytest.mark.unit
def test_format_interpretation_marks_above_floor_categories_with_check_mark() -> None:
    """Categories at or above CATEGORY_FLOOR get the ``above floor`` diagnosis."""
    output = format_interpretation(_result_with_category_score("entity", 1.0))
    # Each above-floor category gets the ``above floor`` diagnosis text.
    assert "above floor" in output


# Note: ``_category_diagnosis``'s unknown-category fallback (the
# ``diagnoses.get(category, ...)`` default) is unreachable through
# format_interpretation, which only iterates the fixed CATEGORY_WEIGHTS
# keys. The fallback is pragma'd in runner.py.


# ---------------------------------------------------------------------------
# format_interpretation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_interpretation_returns_string() -> None:
    result = BenchmarkResult(
        meta={
            "suite_name": "test-suite",
            "system": "hybrid",
            "date": "2026-03-23",
            "n_cases": 4,
        },
        summary={
            "weighted_total": 0.762,
            "category_scores": {
                "recall": 0.875,
                "entity": 0.933,
                "classification": 1.0,
            },
            "gates": {"phase1": True, "phase2": True, "phase3": True},
        },
        diagnostics={},
        cases=[],
    )
    output = format_interpretation(result)
    assert "0.762" in output
    assert isinstance(output, str)
    assert len(output) > 50


# ---------------------------------------------------------------------------
# NDCG@10 helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dcg_perfect_relevance() -> None:
    import math

    expected = 2 / math.log2(2) + 1 / math.log2(3)
    assert dcg([2, 1, 0], k=3) == pytest.approx(expected)


@pytest.mark.unit
def test_dcg_empty_relevances() -> None:
    assert dcg([], k=10) == pytest.approx(0.0)


@pytest.mark.unit
def test_dcg_k_truncates() -> None:
    import math

    assert dcg([2, 1, 1], k=1) == pytest.approx(2 / math.log2(2))


@pytest.mark.unit
def test_ideal_dcg_sorts_by_relevance() -> None:
    gold = [{"path": "a.md", "relevance": 1}, {"path": "b.md", "relevance": 2}]
    import math

    expected = 2 / math.log2(2) + 1 / math.log2(3)
    assert ideal_dcg_graded(gold, k=10) == pytest.approx(expected)


@pytest.mark.unit
def test_ndcg_score_perfect_retrieval() -> None:
    gold = [{"path": "a.md", "relevance": 2}, {"path": "b.md", "relevance": 1}]
    retrieved = ["a.md", "b.md", "c.md"]
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(1.0)


@pytest.mark.unit
def test_ndcg_score_no_relevant_retrieved() -> None:
    gold = [{"path": "a.md", "relevance": 2}]
    retrieved = ["x.md", "y.md"]
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(0.0)


@pytest.mark.unit
def test_ndcg_score_empty_gold() -> None:
    assert ndcg_graded(["a.md", "b.md"], [], k=10) == pytest.approx(0.0)


@pytest.mark.unit
def test_ndcg_score_partial_retrieval() -> None:
    gold = [{"path": "a.md", "relevance": 2}, {"path": "b.md", "relevance": 1}]
    retrieved = ["b.md"]
    score = ndcg_graded(retrieved, gold, k=10)
    assert 0.0 < score < 1.0


@pytest.mark.unit
def test_ndcg_score_case_insensitive() -> None:
    gold = [{"path": "Docs/Alpha.md", "relevance": 2}]
    retrieved = ["docs/alpha.md"]
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(1.0)


@pytest.mark.unit
def test_ndcg_score_known_value() -> None:
    import math

    gold = [
        {"path": "a.md", "relevance": 2},
        {"path": "b.md", "relevance": 1},
        {"path": "c.md", "relevance": 0},
    ]
    retrieved = ["b.md", "a.md"]
    idcg = ideal_dcg_graded(gold, k=10)
    actual_dcg = 1 / math.log2(2) + 2 / math.log2(3)
    expected = actual_dcg / idcg
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(expected, abs=1e-9)


@pytest.mark.unit
def test_hit_at_k_true_when_relevant_in_top_k() -> None:
    gold = [{"path": "a.md", "relevance": 2}]
    assert hit_at_k_graded(["x.md", "a.md", "y.md"], gold, k=5) is True


@pytest.mark.unit
def test_hit_at_k_false_when_outside_k() -> None:
    gold = [{"path": "a.md", "relevance": 2}]
    retrieved = ["x1.md", "x2.md", "x3.md", "x4.md", "x5.md", "a.md"]
    assert hit_at_k_graded(retrieved, gold, k=5) is False


@pytest.mark.unit
def test_hit_at_k_excludes_zero_relevance() -> None:
    gold = [{"path": "a.md", "relevance": 0}]
    assert hit_at_k_graded(["a.md"], gold, k=5) is False


@pytest.mark.unit
def test_reciprocal_rank_first_position() -> None:
    gold = [{"path": "a.md", "relevance": 1}]
    assert reciprocal_rank_graded(["a.md", "b.md"], gold, k=10) == pytest.approx(1.0)


@pytest.mark.unit
def test_reciprocal_rank_second_position() -> None:
    gold = [{"path": "b.md", "relevance": 1}]
    assert reciprocal_rank_graded(["a.md", "b.md", "c.md"], gold, k=10) == pytest.approx(0.5)


@pytest.mark.unit
def test_reciprocal_rank_not_found() -> None:
    gold = [{"path": "x.md", "relevance": 1}]
    assert reciprocal_rank_graded(["a.md", "b.md"], gold, k=10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Title-and-stem normalisation — observed via match_gold_to_path, the
# public caller of _normalise_title / _stem_from_path. Each test asserts on
# the matching outcome rather than on the helper return values directly.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_match_gold_normalises_spaces_in_gold_title_against_hyphenated_filename() -> None:
    """Gold ``Jordan Blake`` (space-separated) matches a filename ``jordan-blake.md``."""
    assert match_gold_to_path("Jordan Blake", "vault/jordan-blake.md") is True


@pytest.mark.unit
def test_match_gold_normalises_underscores_in_gold_against_hyphenated_filename() -> None:
    """Gold ``some_slug`` (underscore) matches a filename ``some-slug.md``."""
    assert match_gold_to_path("some_slug", "vault/some-slug.md") is True


@pytest.mark.unit
def test_match_gold_already_normalised_title_round_trips() -> None:
    """An already-normalised gold title matches its identical-stem filename."""
    assert match_gold_to_path("already-normalised", "vault/already-normalised.md") is True


@pytest.mark.unit
def test_match_gold_collapses_runs_of_separators_in_title() -> None:
    """Gold ``foo  bar--baz`` (double space + double hyphen) matches ``foo-bar-baz.md``."""
    assert match_gold_to_path("foo  bar--baz", "vault/foo-bar-baz.md") is True


@pytest.mark.unit
def test_match_gold_strips_leading_and_trailing_separators_in_title() -> None:
    """Gold ``-leading-trailing-`` matches ``leading-trailing.md``."""
    assert match_gold_to_path("-leading-trailing-", "vault/leading-trailing.md") is True


@pytest.mark.unit
def test_match_gold_extracts_stem_from_simple_filename() -> None:
    """Path-stem extraction: gold ``patterns`` matches ``patterns.md``."""
    assert match_gold_to_path("patterns", "patterns.md") is True


@pytest.mark.unit
def test_match_gold_extracts_stem_from_deep_document_path() -> None:
    """Stem extraction works for a deep filesystem path."""
    assert match_gold_to_path("acme-corp", "02-Areas/00-Clients/Acme-Corp/Acme-Corp.md") is True


@pytest.mark.unit
def test_match_gold_extracts_stem_from_entity_path() -> None:
    assert match_gold_to_path("jordan-blake", "entities/person/jordan-blake.md") is True


@pytest.mark.unit
def test_match_gold_extracts_stem_from_dated_log_filename() -> None:
    assert match_gold_to_path("2026-04-10", "agent-memory/builder/2026-04-10.md") is True


# ---------------------------------------------------------------------------
# _title_in_retrieved
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_title_in_retrievedexact_match() -> None:
    paths = ["02-Areas/00-Clients/Acme-Corp/Acme-Corp.md", "other/doc.md"]
    assert title_in_retrieved("Acme Corp", paths, top_k=5) is True


@pytest.mark.unit
def test_title_in_retrieved_entity_slug_match() -> None:
    paths = ["entities/person/jordan-blake.md"]
    assert title_in_retrieved("jordan-blake", paths, top_k=5) is True


@pytest.mark.unit
def test_title_in_retrieved_no_match() -> None:
    paths = ["some/unrelated/doc.md"]
    assert title_in_retrieved("jordan-blake", paths, top_k=5) is False


@pytest.mark.unit
def test_title_in_retrieved_respects_top_k() -> None:
    # Gold title is at position 3, but top_k=2 — must not match
    paths = ["a.md", "b.md", "entities/person/jordan-blake.md"]
    assert title_in_retrieved("jordan-blake", paths, top_k=2) is False


@pytest.mark.unit
def test_title_in_retrieved_case_insensitive() -> None:
    paths = ["Vault/JORDAN-BLAKE.md"]
    assert title_in_retrieved("Jordan Blake", paths, top_k=5) is True


# ---------------------------------------------------------------------------
# ndcg_graded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ndcg_by_title_perfect_retrieval() -> None:
    gold = [{"title": "jordan-blake", "relevance": 2}]
    retrieved = ["entities/person/jordan-blake.md", "other/doc.md"]
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(1.0)


@pytest.mark.unit
def test_ndcg_by_title_partial_retrieval() -> None:
    gold = [
        {"title": "jordan-blake", "relevance": 2},
        {"title": "team-overview", "relevance": 1},
    ]
    # Only second gold retrieved; first (highest relevance) not found -> NDCG < 1
    retrieved = ["shared/team-overview.md", "other/doc.md"]
    score = ndcg_graded(retrieved, gold, k=10)
    assert 0.0 < score < 1.0


@pytest.mark.unit
def test_ndcg_by_title_miss() -> None:
    gold = [{"title": "jordan-blake", "relevance": 2}]
    retrieved = ["some/unrelated/doc.md"]
    assert ndcg_graded(retrieved, gold, k=10) == pytest.approx(0.0)


@pytest.mark.unit
def test_ndcg_by_title_empty_gold() -> None:
    assert ndcg_graded(["doc.md"], [], k=10) == pytest.approx(0.0)


@pytest.mark.unit
def test_ndcg_by_title_file_moved_still_matches() -> None:
    """Score is unaffected by document store reorganisation — title is the stable identity."""
    gold = [{"title": "patterns", "relevance": 2}]
    # Same note, different folder
    original_path = ["04-Agent-Knowledge/builder/patterns.md"]
    moved_path = ["Archive/old-knowledge/patterns.md"]
    assert ndcg_graded(original_path, gold, k=10) == pytest.approx(ndcg_graded(moved_path, gold, k=10))


# ---------------------------------------------------------------------------
# hit_at_k_graded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def testhit_at_k_graded_true() -> None:
    gold = [{"title": "jordan-blake", "relevance": 1}]
    assert hit_at_k_graded(["entities/person/jordan-blake.md"], gold, k=5) is True


@pytest.mark.unit
def testhit_at_k_graded_false_beyond_k() -> None:
    gold = [{"title": "jordan-blake", "relevance": 1}]
    paths = ["a.md", "b.md", "entities/person/jordan-blake.md"]
    assert hit_at_k_graded(paths, gold, k=2) is False


@pytest.mark.unit
def testhit_at_k_graded_excludes_zero_relevance() -> None:
    gold = [{"title": "jordan-blake", "relevance": 0}]
    assert hit_at_k_graded(["entities/person/jordan-blake.md"], gold, k=5) is False


# ---------------------------------------------------------------------------
# reciprocal_rank_graded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def testreciprocal_rank_graded_first_position() -> None:
    gold = [{"title": "jordan-blake", "relevance": 1}]
    paths = ["entities/person/jordan-blake.md", "other.md"]
    assert reciprocal_rank_graded(paths, gold, k=10) == pytest.approx(1.0)


@pytest.mark.unit
def testreciprocal_rank_graded_second_position() -> None:
    gold = [{"title": "jordan-blake", "relevance": 1}]
    paths = ["other.md", "entities/person/jordan-blake.md"]
    assert reciprocal_rank_graded(paths, gold, k=10) == pytest.approx(0.5)


@pytest.mark.unit
def testreciprocal_rank_graded_not_found() -> None:
    gold = [{"title": "jordan-blake", "relevance": 1}]
    assert reciprocal_rank_graded(["unrelated.md"], gold, k=10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# format_interpretation — NDCG display
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_interpretation_shows_ndcg_when_present() -> None:
    result = BenchmarkResult(
        meta={
            "suite_name": "test-suite",
            "system": "hybrid",
            "date": "2026-04-15",
            "n_cases": 5,
        },
        summary={
            "weighted_total": 0.55,
            "category_scores": {"recall": 0.60, "entity": 0.70},
            "gates": {"phase1": False, "phase2": False, "phase3": False},
            "ndcg_at_10": 0.587,
            "hit_rate_at_5": 0.720,
            "mrr_at_10": 0.650,
        },
        diagnostics={},
        cases=[],
    )
    output = format_interpretation(result)
    assert "NDCG@10" in output
    assert "0.587" in output
    assert "Hit@5" in output
    assert "MRR@10" in output


@pytest.mark.unit
def test_format_interpretation_omits_ndcg_section_when_absent() -> None:
    result = BenchmarkResult(
        meta={
            "suite_name": "test-suite",
            "system": "hybrid",
            "date": "2026-04-15",
            "n_cases": 3,
        },
        summary={
            "weighted_total": 0.70,
            "category_scores": {"recall": 0.75},
            "gates": {"phase1": True, "phase2": False, "phase3": False},
            "ndcg_at_10": None,
            "hit_rate_at_5": None,
            "mrr_at_10": None,
        },
        diagnostics={},
        cases=[],
    )
    output = format_interpretation(result)
    assert "NDCG@10" not in output


# ---------------------------------------------------------------------------
# Branch coverage — _exact_match / _fuzzy_match suffix paths, score_tier, etc.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_match_returns_1_via_progressive_suffix_match() -> None:
    """``exact_match`` returns 1.0 when the gold path's last component matches a result path.

    Both paths are absolute strings that don't share a substring, but the gold's
    last segment ``rules.md`` matches as a suffix of the retrieved path.
    """
    paths = ["04-Agent-Knowledge/builder/rules.md"]
    score = exact_match(paths, "alt-prefix/that-doesnt-overlap/rules.md")
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_fuzzy_match_returns_1_via_progressive_suffix_match() -> None:
    """``fuzzy_match`` matches via the same suffix-shortening loop."""
    paths = ["a/b/c/d/e/f/notes.md"]
    # Last segment ``notes.md`` matches but the longer suffix ``e/f/notes.md`` is
    # what the loop step lands on.
    score = fuzzy_match(paths, "different/dir/e/f/notes.md")
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_score_tier_returns_below_baseline_label_for_zero_score() -> None:
    """Score below every threshold returns the bottommost SCORE_TIERS label.

    Closes coverage of the ``return SCORE_TIERS[-1][1]`` final return at line 281.
    The score-tier loop short-circuits on the first threshold met; only a score
    that beats none of them reaches the trailing fallback.
    """
    from kairix.quality.benchmark.runner import score_tier

    label = score_tier(-1.0)  # below 0.0 → falls through every tier
    assert "Below" in label or "broken" in label


@pytest.mark.unit
def test_format_interpretation_lists_categories_below_floor_when_any_fail() -> None:
    """The interpretation output lists each category whose score is below the floor.

    Closes coverage of the ``floors_failed`` block (lines 334-336).
    """
    from kairix.quality.benchmark.runner import BenchmarkResult, format_interpretation

    result = BenchmarkResult(
        meta={"suite_name": "x", "system": "hybrid", "agent": None, "n_cases": 1},
        summary={
            "weighted_total": 0.30,
            "category_scores": {"recall": 0.20, "temporal": 0.10, "entity": 0.95},
            "ndcg_at_10": None,
            "hit_rate_at_5": None,
            "mrr_at_10": None,
        },
        diagnostics={"category_counts": {"recall": 1, "temporal": 1, "entity": 1}},
        cases=[],
    )
    output = format_interpretation(result)
    # The two below-floor categories are named explicitly with their scores.
    assert "Categories below floor" in output
    assert "recall:" in output
    assert "temporal:" in output
    # The above-floor category is NOT listed in the failures block.
    above_block = output.split("Categories below floor")[1] if "Categories below floor" in output else ""
    assert "entity:" not in above_block


@pytest.mark.unit
def test_score_case_dispatches_classification_viaclassification_score() -> None:
    """A case with score_method='classification' delegates to ``classification_score``.

    Closes coverage of the classification-dispatch path in score_case. Without
    explicit deps the production ``DefaultContentClassifier`` is constructed:
    rules return ``unknown`` for the unstructured query, the LLM fallback
    short-circuits to ``unknown`` when Azure creds aren't resolvable in the
    test env, so the final score collapses to 0.0 (unknown != "decision").
    The dispatch and the deps-default chain are exercised end-to-end.
    """
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import score_case

    case = SimpleNamespace(
        score_method="classification",
        query="any query",
        expected_type="decision",
        gold_title=None,
        gold_paths=None,
        gold_titles=None,
        gold_path=None,
    )
    score, detail = score_case(case, paths=[], snippets=[], retrieval_meta={})
    # Default deps wire DefaultContentClassifier; rules → unknown, LLM → unknown
    # (no creds), result.type != "decision" → 0.0.
    assert score == pytest.approx(0.0)
    assert detail == {}


@pytest.mark.unit
def test_score_case_exact_with_gold_title_uses_title_in_retrieved_helper() -> None:
    """When gold_title is set and score_method='exact', the title-based helper decides.

    Closes coverage of lines 363-364 (gold_title branch in exact-match scoring).
    """
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import score_case

    case = SimpleNamespace(
        score_method="exact",
        gold_title="rules",  # title keyword
        gold_path=None,
        gold_titles=None,
        gold_paths=None,
        query="q",
        expected_type=None,
    )
    # The retrieved path's stem contains "rules"; _title_in_retrieved → True → score 1.0.
    score, detail = score_case(
        case,
        paths=["04-Agent-Knowledge/builder/rules.md"],
        snippets=[],
        retrieval_meta={},
    )
    assert score == pytest.approx(1.0)
    assert detail == {}


@pytest.mark.unit
def test_score_case_exact_with_gold_title_misses_when_title_absent_from_paths() -> None:
    """gold_title not present in any retrieved path → score 0.0 via the gold_title branch."""
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import score_case

    case = SimpleNamespace(
        score_method="exact",
        gold_title="missing-title",
        gold_path=None,
        gold_titles=None,
        gold_paths=None,
        query="q",
        expected_type=None,
    )
    score, detail = score_case(
        case,
        paths=["docs/something-else.md", "docs/another.md"],
        snippets=[],
        retrieval_meta={},
    )
    assert score == pytest.approx(0.0)
    assert detail == {}


@pytest.mark.unit
def test_score_case_fuzzy_with_gold_title_uses_title_in_retrieved_helper() -> None:
    """fuzzy + gold_title also routes through _title_in_retrieved at the wider top-k.

    Closes coverage of lines 370-371.
    """
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import score_case

    case = SimpleNamespace(
        score_method="fuzzy",
        gold_title="patterns",
        gold_path=None,
        gold_titles=None,
        gold_paths=None,
        query="q",
        expected_type=None,
    )
    # Place the matching path further down the list to exercise the wider top-k.
    paths = [f"unrelated/path-{i}.md" for i in range(8)] + ["04-Agent-Knowledge/builder/patterns.md"]
    score, _ = score_case(case, paths=paths, snippets=[], retrieval_meta={})
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_score_case_fuzzy_with_gold_path_uses_fuzzy_match_helper() -> None:
    """fuzzy + gold_path routes through _fuzzy_match (lines 372-374)."""
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import score_case

    case = SimpleNamespace(
        score_method="fuzzy",
        gold_title=None,
        gold_path="docs/architecture.md",
        gold_titles=None,
        gold_paths=None,
        query="q",
        expected_type=None,
    )
    score, _ = score_case(
        case,
        paths=["docs/architecture.md", "other.md"],
        snippets=[],
        retrieval_meta={},
    )
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_compute_weighted_total_uses_v1_1_classification_weight_when_classification_present() -> None:
    """For suite version >= '1.1' with a classification score, the v1.1 weight
    scheme applies — temporal cedes 0.10 of its weight to classification.

    With identical non-classification categories the per-category mix matters:
    a low temporal + high classification yields a HIGHER v1.1 total than v1.0
    (because more weight now sits on the high-scoring classification category).
    """
    from kairix.quality.benchmark.runner import compute_weighted_total

    # Temporal scores low, classification scores high — the v1.1 reweighting
    # moves 0.10 of weight from the low-scoring temporal to the high-scoring
    # classification, raising the total.
    per_cat = {
        "recall": 1.0,
        "temporal": 0.0,  # low — losing weight is good for the total
        "entity": 1.0,
        "conceptual": 1.0,
        "multi_hop": 1.0,
        "procedural": 1.0,
        "classification": 1.0,  # high — gaining weight is good for the total
    }
    v10 = compute_weighted_total(per_cat, "1.0")
    v11 = compute_weighted_total(per_cat, "1.1")
    # v1.1 total must be higher because weight shifted from a 0.0 category
    # to a 1.0 category.
    assert v11 > v10, f"v1.1 reweighting failed to apply: v10={v10}, v11={v11}"


@pytest.mark.unit
def test_run_benchmark_warns_when_some_recall_cases_have_no_gold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Some-but-not-all recall cases missing gold references → warning, no raise.

    The validation step runs at the start of ``run_benchmark`` and observes
    the suite. We invoke ``run_benchmark`` with a mixed suite and assert the
    warning record appears; the run still completes (no raise).
    """
    import logging

    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"agent": "t", "collections": ["vault"]},
        cases=[
            BenchmarkCase(id="R1", category="recall", query="q1", gold_path=None, score_method="ndcg"),
            BenchmarkCase(id="R2", category="recall", query="q2", gold_path="docs/x.md", score_method="ndcg"),
        ],
    )

    def _retrieve(**kwargs):  # type: ignore[no-untyped-def]  # local test stub; kwargs unused
        return ([], [], {"intent": "semantic"})

    with caplog.at_level(logging.WARNING):
        # Runs to completion — no raise — just warns.
        run_benchmark(suite, system="hybrid", agent="t", deps=BenchmarkDeps(retrieve=_retrieve))
    assert any("1/2 recall cases have no gold references" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_benchmark_raises_value_error_when_all_recall_cases_have_no_gold() -> None:
    """All recall cases missing gold references → ``run_benchmark`` raises before retrieving."""
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"agent": "t", "collections": ["vault"]},
        cases=[
            BenchmarkCase(id="R1", category="recall", query="q1", gold_path=None, score_method="ndcg"),
            BenchmarkCase(id="R2", category="recall", query="q2", gold_path=None, score_method="ndcg"),
        ],
    )

    def _retrieve(**kwargs):  # type: ignore[no-untyped-def]  # local test stub; signature intentionally permissive
        # If validation didn't fire, the retrieve callable would be called; the test
        # would then surface the failure as "retrieve was called" rather than ValueError.
        raise AssertionError("deps.retrieve must not run when validation fails")

    with pytest.raises(ValueError, match="no gold references"):
        run_benchmark(suite, system="hybrid", agent="t", deps=BenchmarkDeps(retrieve=_retrieve))


# ---------------------------------------------------------------------------
# BenchmarkDeps — production-default coverage for the formerly-pragma'd branches.
#
# The three # pragma: no cover markers in runner.py guarded production-only
# paths: the lazy classify_content / classify_with_llm imports inside
# DefaultContentClassifier, and the lazy AzureChatBackend construction inside
# llm_judge. With BenchmarkDeps in place those branches are reachable through
# the public surface — the tests below drive each one.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_content_classifier_classify_rules_runs_real_classify_content() -> None:
    """DefaultContentClassifier.classify_rules delegates to classify_content.

    Driving classification_score with no injected classifier exercises the
    runtime import of kairix.core.classify.rules.classify_content. A query
    that the rules module recognises as a decision yields a typed result;
    the score of 1.0 confirms the production-default classifier ran end to end.
    """
    score = classification_score("We decided to use PostgreSQL.", "semantic-decision")
    # The decision-pattern rule fires and the result type matches the
    # expected_type — only possible if the lazy import + delegation worked.
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_default_content_classifier_classify_with_llm_fires_when_rules_return_unknown() -> None:
    """When rules return ``unknown`` the LLM fallback path is invoked.

    ``classify_with_llm("")`` short-circuits to ``unknown`` without an Azure
    API call (empty content guard inside judge.py). This drives the lazy
    ``from kairix.core.classify.judge import classify_with_llm`` import in
    DefaultContentClassifier without requiring credentials.
    """
    # Empty query: rules return unknown → LLM fallback runs and also returns
    # unknown (its empty-content guard). result.type ("unknown") != "decision"
    # so score = 0.0. The fallback PATH being executed is what's under test.
    score = classification_score("", "decision")
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_llm_judge_lazy_default_chat_backend_returns_zero_on_credential_failure() -> None:
    """``llm_judge`` without ``chat_backend=`` constructs ``AzureChatBackend``.

    The test environment doesn't resolve Azure credentials, so the constructed
    backend's ``complete()`` raises and the wrapping try/except returns 0.0.
    The lazy default-construction branch is what's covered — the score being
    0.0 (rather than IndexError or NameError) is the receipt that the import
    plus construction succeeded and the exception path handled the failure.
    """
    # No chat_backend kwarg → the AzureChatBackend factory inside llm_judge runs.
    score = llm_judge(query="q", paths=["doc.md"], snippets=["snippet"])
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# BenchmarkDeps — defaults factory wires the production collaborators.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benchmark_deps_default_constructor_wires_production_collaborators() -> None:
    """``BenchmarkDeps()`` (no args) constructs production defaults via field factories."""
    from kairix.quality.benchmark.runner import BenchmarkDeps, DefaultContentClassifier

    deps = BenchmarkDeps()
    # Each field is the production default — an instance of the production class.
    assert isinstance(deps.classifier, DefaultContentClassifier)
    # Chat backend duck-types as ChatBackend (has a ``complete`` method).
    assert hasattr(deps.chat_backend, "complete")
    # Retrieve callable accepts the documented kwargs.
    assert callable(deps.retrieve)


@pytest.mark.unit
def test_benchmark_deps_each_field_overridable_independently() -> None:
    """Tests can override one field without disturbing the others."""
    from kairix.quality.benchmark.runner import BenchmarkDeps, DefaultContentClassifier
    from tests.fakes import FakeChatBackend

    fake_backend = FakeChatBackend(responses=["0.7"])
    deps = BenchmarkDeps(chat_backend=fake_backend)

    assert deps.chat_backend is fake_backend
    # Other fields fell back to production defaults — receipt that overrides
    # don't drag siblings.
    assert isinstance(deps.classifier, DefaultContentClassifier)


# ---------------------------------------------------------------------------
# Metric-aggregation drift — empty / single / all-skipped categories.
# These are the failure modes the issue calls out: silent miscalc when a
# category has no cases (empty), exactly one case (single), or every case is
# a classification (skips retrieval). Each test drives ``run_benchmark``
# through the public surface and asserts the per-category score.
# ---------------------------------------------------------------------------


def _bench_case(id_: str, category: str, gold_path: str) -> Any:
    """Build an exact-match BenchmarkCase for the given category."""
    from kairix.quality.benchmark.suite import BenchmarkCase

    return BenchmarkCase(
        id=id_,
        category=category,
        query=f"query-{id_}",
        gold_path=gold_path,
        score_method="exact",
    )


def _retrieve_returning(paths: list[str]):
    """Retrieve callable that returns the same paths on every call."""

    def _fn(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return paths, ["snippet"] * len(paths), {"intent": "semantic"}

    return _fn


@pytest.mark.unit
def test_run_benchmark_empty_category_aggregates_to_zero_not_nan() -> None:
    """A category with no cases must aggregate to 0.0, not NaN or KeyError.

    Silent miscalc here means an empty ``temporal`` category would show as
    0.0 in the report — operators could mistake "no cases" for "all failed".
    The receipt is that the category appears in ``category_scores`` and
    its value is exactly 0.0 (not absent, not NaN).
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkSuite

    # Suite has only ONE category populated — recall — every other category
    # ends up with an empty score list inside run_benchmark.
    suite = BenchmarkSuite(
        meta={"name": "empty-cats", "version": "1.0", "agent": "t"},
        cases=[_bench_case("R01", "recall", "vault/x.md")],
    )
    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(retrieve=_retrieve_returning(["vault/x.md"])),
    )

    # Every category from CATEGORY_WEIGHTS is present in the aggregate
    # (run_benchmark seeds the dict with all categories).
    cat_scores = result.summary["category_scores"]
    for cat in ("temporal", "entity", "conceptual", "multi_hop", "procedural"):
        assert cat in cat_scores, f"empty category {cat!r} dropped from category_scores"
        assert cat_scores[cat] == pytest.approx(0.0), (
            f"empty category {cat!r} aggregated to {cat_scores[cat]!r} (expected 0.0)"
        )
    # And recall — the single populated category — scored 1.0.
    assert cat_scores["recall"] == pytest.approx(1.0)
    # diagnostics.category_counts reflects zero cases for the empty categories.
    counts = result.diagnostics["category_counts"]
    for cat in ("temporal", "entity", "conceptual", "multi_hop", "procedural"):
        assert counts.get(cat, 0) == 0


@pytest.mark.unit
def test_run_benchmark_single_item_category_score_equals_that_items_score() -> None:
    """A category with exactly one case scores equal to that case's score.

    The aggregation formula is sum/len — for n=1 it must be the bare score
    (no rounding artefact, no divide-by-zero). Sabotage-prove: assert the
    score isn't 0.0 (the empty-category default) and isn't 0.5 (an arithmetic
    mistake mid-aggregation).
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"name": "single", "version": "1.0", "agent": "t"},
        cases=[_bench_case("E01", "entity", "vault/jordan-blake.md")],
    )
    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(retrieve=_retrieve_returning(["vault/jordan-blake.md"])),
    )

    cat_scores = result.summary["category_scores"]
    # Single matching case → score 1.0.
    assert cat_scores["entity"] == pytest.approx(1.0)
    # Sabotage check — values that would surface a faulty aggregator.
    assert cat_scores["entity"] != pytest.approx(0.0)
    assert cat_scores["entity"] != pytest.approx(0.5)
    # Counts confirm n=1 for the populated category.
    assert result.diagnostics["category_counts"]["entity"] == 1


@pytest.mark.unit
def test_run_benchmark_single_item_category_with_miss_scores_zero() -> None:
    """A single-case category that misses scores 0.0 — the receipt that the
    aggregation isn't accidentally treating "score=0" as "no data".

    Without this distinction, a low-but-real score could be confused for
    "no cases" by readers of the report.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"name": "miss", "version": "1.0", "agent": "t"},
        cases=[_bench_case("R01", "recall", "vault/expected.md")],
    )
    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(retrieve=_retrieve_returning(["vault/something-else.md"])),
    )

    # The single recall case scored 0.0 (gold not in retrieved). Counts must
    # still be 1 — score=0.0 is data, not absence.
    assert result.summary["category_scores"]["recall"] == pytest.approx(0.0)
    assert result.diagnostics["category_counts"]["recall"] == 1


@pytest.mark.unit
def test_run_benchmark_all_classification_cases_skip_retrieval() -> None:
    """A suite of only classification cases never invokes retrieval —
    classification cases are aggregated under the ``classification`` category
    and other categories are empty.

    Sabotage-prove: install a retrieve callable that raises if invoked. If
    the test passes, the retrieval skip is honoured.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    def _retrieve_must_not_run(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        raise AssertionError("retrieve was called for a classification-only suite")

    # Two classification cases — each will be scored by classification_score
    # using the deps.classifier (production default → 0.0 here, see above).
    suite = BenchmarkSuite(
        meta={"name": "all-classification", "version": "1.0", "agent": "t"},
        cases=[
            BenchmarkCase(
                id="C01",
                category="classification",
                query="please classify",
                gold_path=None,
                score_method="classification",
                expected_type="decision",
            ),
            BenchmarkCase(
                id="C02",
                category="classification",
                query="another classify",
                gold_path=None,
                score_method="classification",
                expected_type="pattern",
            ),
        ],
    )

    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(retrieve=_retrieve_must_not_run),
    )

    # classification category recorded both cases.
    assert result.diagnostics["category_counts"]["classification"] == 2
    # retrieval was skipped — no AssertionError surfaced.
    # Every other category has zero cases.
    for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"):
        assert result.diagnostics["category_counts"].get(cat, 0) == 0


@pytest.mark.unit
def test_aggregate_scores_by_category_avoids_divide_by_zero_for_empty_input() -> None:
    """The aggregator must not raise ZeroDivisionError for an empty list.

    Sabotage-prove: calling sum([])/len([]) would raise. The receipt is the
    aggregator returning 0.0 for the empty list (and the expected average
    for the populated list).
    """
    from kairix.quality.benchmark.runner import aggregate_scores_by_category

    out = aggregate_scores_by_category({"empty": [], "two": [0.5, 1.0]})
    assert out == {"empty": 0.0, "two": pytest.approx(0.75)}


@pytest.mark.unit
def test_aggregate_scores_by_category_rounds_to_four_places() -> None:
    """The aggregator rounds to 4 places — a sanity check that the rounding
    contract documented in the function holds for irrational averages.

    A plain ``sum([1/3, 1/3, 1/3]) / 3`` would emit 0.3333333333333333; the
    aggregator must collapse this to 0.3333.
    """
    from kairix.quality.benchmark.runner import aggregate_scores_by_category

    out = aggregate_scores_by_category({"thirds": [1 / 3, 1 / 3, 1 / 3]})
    # Round to 4 places — anything else means the aggregator dropped the
    # rounding contract.
    assert out["thirds"] == pytest.approx(0.3333, abs=1e-9)


# ---------------------------------------------------------------------------
# retrieve_case + run_benchmark error handling — error meta surfaces as
# {"error": "..."} when the retrieve callable raises.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retrieve_case_returns_error_meta_when_retrieve_callable_raises() -> None:
    """``retrieve_case`` swallows retrieval exceptions into a meta dict.

    The wrapper exists precisely so a single failing case doesn't kill the
    whole benchmark. The receipt is (paths=[], snippets=[], meta={"error": "..."}).
    """
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import BenchmarkDeps, retrieve_case

    case = SimpleNamespace(score_method="exact", query="q", agent=None)

    def _boom(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        raise RuntimeError("retrieval down")

    paths, snippets, meta = retrieve_case(
        case,
        system="hybrid",
        agent="t",
        db_path=None,
        collection=None,
        fusion_override=None,
        deps=BenchmarkDeps(retrieve=_boom),
    )

    assert paths == []
    assert snippets == []
    assert "error" in meta
    assert "retrieval down" in meta["error"]


@pytest.mark.unit
def test_retrieve_case_classification_skips_retrieve_callable() -> None:
    """A classification case never invokes the retrieve callable.

    Sabotage-prove with a retrieve that raises if called.
    """
    from types import SimpleNamespace

    from kairix.quality.benchmark.runner import BenchmarkDeps, retrieve_case

    case = SimpleNamespace(score_method="classification", query="q", agent=None)

    def _retrieve_must_not_run(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        raise AssertionError("retrieve must not be called for classification cases")

    paths, snippets, meta = retrieve_case(
        case,
        system="hybrid",
        agent="t",
        db_path=None,
        collection=None,
        fusion_override=None,
        deps=BenchmarkDeps(retrieve=_retrieve_must_not_run),
    )

    assert paths == []
    assert snippets == []
    # Marker meta differentiates classification skips from real retrieval errors.
    assert meta == {"scored_by": "classification"}


@pytest.mark.unit
def test_run_benchmark_threads_chat_backend_through_to_llm_judge() -> None:
    """A custom ``deps.chat_backend`` is consulted for ``llm`` score-method cases.

    Receipt: the FakeChatBackend logs the call. If ``score_case`` constructed
    its own backend (regression), the fake's ``calls`` list would be empty.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
    from tests.fakes import FakeChatBackend

    backend = FakeChatBackend(responses=["0.9"])
    suite = BenchmarkSuite(
        meta={"name": "llm-route", "version": "1.0", "agent": "t"},
        # Use a non-classification, non-recall category so validation doesn't
        # trip the all-no-gold guard. ``temporal`` with score_method="llm"
        # routes through llm_judge.
        cases=[
            BenchmarkCase(
                id="T01",
                category="temporal",
                query="when did this happen?",
                gold_path=None,
                score_method="llm",
            )
        ],
    )

    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(
            chat_backend=backend,
            retrieve=_retrieve_returning(["vault/some-doc.md"]),
        ),
    )

    # The fake backend was consulted exactly once — confirms the deps
    # threading reached llm_judge.
    assert len(backend.calls) == 1
    # The case's recorded score reflects the backend response.
    assert result.cases[0]["score"] == pytest.approx(0.9)


@pytest.mark.unit
def test_run_benchmark_threads_classifier_through_to_classification_score() -> None:
    """A custom ``deps.classifier`` is consulted for classification cases.

    Receipt: the FakeContentClassifier logs the rules call.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
    from tests.fakes import FakeContentClassifier

    classifier = FakeContentClassifier(rules_type="decision")
    suite = BenchmarkSuite(
        meta={"name": "classifier-route", "version": "1.0", "agent": "t"},
        cases=[
            BenchmarkCase(
                id="C01",
                category="classification",
                query="we agreed to ship Friday",
                gold_path=None,
                score_method="classification",
                expected_type="decision",
            )
        ],
    )

    result = run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        deps=BenchmarkDeps(classifier=classifier),
    )

    # Receipt: the fake's rules_calls list captured the runner's invocation.
    assert len(classifier.rules_calls) == 1
    assert classifier.rules_calls[0] == {"query": "we agreed to ship Friday", "agent": "shared"}
    # And the case scored 1.0 because the fake returned the expected type.
    assert result.cases[0]["score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Output-dir JSON serialisation path — covers the file-write block.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_benchmark_writes_json_result_when_output_dir_set(tmp_path: Any) -> None:
    """``output_dir`` set → a JSON file lands at the expected path."""
    import json
    from pathlib import Path

    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"name": "OutSuite", "version": "1.0", "agent": "t"},
        cases=[_bench_case("R01", "recall", "vault/a.md")],
    )
    out = Path(tmp_path) / "results"

    run_benchmark(
        suite,
        system="hybrid",
        agent="t",
        output_dir=str(out),
        deps=BenchmarkDeps(retrieve=_retrieve_returning(["vault/a.md"])),
    )

    files = list(out.glob("B-outsuite-hybrid-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    # Payload carries the documented top-level keys.
    assert set(payload.keys()) == {"meta", "summary", "diagnostics", "cases"}
    assert payload["summary"]["weighted_total"] == pytest.approx(0.25)  # recall@1.0 * 0.25
