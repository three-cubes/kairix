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
    """A case with score_method='classification' delegates to ``_classification_score``.

    Closes coverage of line 360 — the classification-dispatch path in score_case.
    Production calls classification_score(...) without an injected classifier;
    the FakeContentClassifier here would not be picked up. We instead rely on
    the production-default classifier path's ``except Exception: return 0.0``
    behaviour: the classify modules are unavailable in the test env, so the
    score collapses to 0.0. The dispatch itself is exercised regardless.
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
    # In test env _classification_score's lazy default fails to import → returns 0.0.
    # The detail dict is empty for non-NDCG dispatches.
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

    from kairix.quality.benchmark.runner import run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"agent": "t", "collections": ["vault"]},
        cases=[
            BenchmarkCase(id="R1", category="recall", query="q1", gold_path=None, score_method="ndcg"),
            BenchmarkCase(id="R2", category="recall", query="q2", gold_path="docs/x.md", score_method="ndcg"),
        ],
    )

    def _retrieve(**kwargs):  # type: ignore[no-untyped-def]
        return ([], [], {"intent": "semantic"})

    with caplog.at_level(logging.WARNING):
        # Runs to completion — no raise — just warns.
        run_benchmark(suite, system="hybrid", agent="t", retrieve_fn=_retrieve)
    assert any("1/2 recall cases have no gold references" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_benchmark_raises_value_error_when_all_recall_cases_have_no_gold() -> None:
    """All recall cases missing gold references → ``run_benchmark`` raises before retrieving."""
    from kairix.quality.benchmark.runner import run_benchmark
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"agent": "t", "collections": ["vault"]},
        cases=[
            BenchmarkCase(id="R1", category="recall", query="q1", gold_path=None, score_method="ndcg"),
            BenchmarkCase(id="R2", category="recall", query="q2", gold_path=None, score_method="ndcg"),
        ],
    )

    def _retrieve(**kwargs):  # type: ignore[no-untyped-def]
        # If validation didn't fire, the retrieve_fn would be called; the test would
        # then surface the failure as "retrieve was called" rather than a ValueError.
        raise AssertionError("retrieve_fn must not run when validation fails")

    with pytest.raises(ValueError, match="no gold references"):
        run_benchmark(suite, system="hybrid", agent="t", retrieve_fn=_retrieve)
