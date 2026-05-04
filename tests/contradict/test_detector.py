"""
Tests for kairix.knowledge.contradict.detector — contradiction detection.

All tests use mocked hybrid search and mocked LLM backend.
No external services required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kairix.knowledge.contradict.detector import (
    ContradictionResult,
    _parse_llm_response,
    check_contradiction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_result(path: str, content: str) -> MagicMock:
    """Build a mock search bundle for use in patched hybrid_search returns."""
    bundle = MagicMock()
    bundle.content = content
    bundle.result.path = path
    return bundle


def _make_sr(bundles: list) -> MagicMock:
    """Build a mock SearchResponse."""
    sr = MagicMock()
    sr.results = bundles
    return sr


def _fake_search(bundles: list):
    """Return a callable that returns a mock SearchResponse."""

    def _search(**kwargs):
        return _make_sr(bundles)

    return _search


def _failing_search(exc):
    """Return a callable that raises an exception."""

    def _search(**kwargs):
        raise exc

    return _search


def _llm_response(score: float, reason: str = "test reason") -> str:
    return json.dumps({"score": score, "reason": reason})


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_llm_response_valid_json() -> None:
    raw = '{"score": 0.8, "reason": "conflicting facts"}'
    score, reason = _parse_llm_response(raw)
    assert score == pytest.approx(0.8)
    assert reason == "conflicting facts"


@pytest.mark.unit
def test_parse_llm_response_clamps_above_1() -> None:
    raw = '{"score": 1.5, "reason": "extreme"}'
    score, _reason = _parse_llm_response(raw)
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_parse_llm_response_clamps_below_0() -> None:
    raw = '{"score": -0.3, "reason": "negative"}'
    score, _reason = _parse_llm_response(raw)
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_parse_llm_response_zero_score() -> None:
    raw = '{"score": 0.0, "reason": "no contradiction"}'
    score, reason = _parse_llm_response(raw)
    assert score == pytest.approx(0.0)
    assert reason == "no contradiction"


@pytest.mark.unit
def test_parse_llm_response_empty_string() -> None:
    score, reason = _parse_llm_response("")
    assert score is None
    assert reason == ""


@pytest.mark.unit
def test_parse_llm_response_no_json() -> None:
    score, _reason = _parse_llm_response("no json here at all")
    assert score is None


@pytest.mark.unit
def test_parse_llm_response_malformed_json() -> None:
    score, _reason = _parse_llm_response("{score: not valid json}")
    assert score is None


@pytest.mark.unit
def test_parse_llm_response_extracts_from_preamble() -> None:
    """Model may prefix JSON with explanatory text — still parses."""
    raw = 'Here is my assessment: {"score": 0.7, "reason": "contradicts existing record"}'
    score, reason = _parse_llm_response(raw)
    assert score == pytest.approx(0.7)
    assert "contradicts" in reason


@pytest.mark.unit
def test_parse_llm_response_missing_score_key() -> None:
    raw = '{"reason": "no score key"}'
    score, _reason = _parse_llm_response(raw)
    assert score is None


@pytest.mark.unit
def test_parse_llm_response_non_numeric_score() -> None:
    raw = '{"score": "high", "reason": "non-numeric"}'
    score, _reason = _parse_llm_response(raw)
    assert score is None


# ---------------------------------------------------------------------------
# check_contradiction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_contradiction_returns_empty_on_search_failure() -> None:
    """Returns [] when hybrid search raises an exception."""
    llm = MagicMock()
    results = check_contradiction("some claim", llm=llm, search_fn=_failing_search(RuntimeError("no db")))
    assert results == []


@pytest.mark.unit
def test_check_contradiction_returns_empty_when_no_results_above_threshold() -> None:
    """Returns [] when all LLM scores are below threshold."""
    llm = MagicMock()
    llm.chat.return_value = _llm_response(0.2)  # well below default threshold 0.6

    bundles = [_make_search_result("a/doc.md", "some content")]
    results = check_contradiction("new claim", llm=llm, search_fn=_fake_search(bundles))
    assert results == []


@pytest.mark.unit
def test_check_contradiction_returns_result_above_threshold() -> None:
    """Returns a ContradictionResult when score >= threshold."""
    llm = MagicMock()
    llm.chat.return_value = _llm_response(0.9, "directly conflicts with existing record")

    bundles = [_make_search_result("decisions/d01.md", "The project was cancelled in Q3.")]
    results = check_contradiction(
        "The project launched in Q3.",
        llm=llm,
        threshold=0.6,
        search_fn=_fake_search(bundles),
    )
    assert len(results) == 1
    r = results[0]
    assert r.doc_path == "decisions/d01.md"
    assert r.score == pytest.approx(0.9)
    assert "conflicts" in r.reason.lower()


@pytest.mark.unit
def test_check_contradiction_respects_top_k() -> None:
    """Only top_k bundles are evaluated, not all search results."""
    from kairix.knowledge.contradict.scorers import (
        CompositeContradictionScorer,
        DirectContradictionScorer,
    )

    llm = MagicMock()
    llm.chat.return_value = _llm_response(0.8)

    bundles = [_make_search_result(f"doc{i}.md", f"content {i}") for i in range(10)]
    # Use a single-scorer composite so the LLM call count maps 1:1 to candidates
    # — keeps the test's "respects top_k" intent intact under the WS2-B
    # three-category default which would otherwise make 3 calls per candidate.
    scorer = CompositeContradictionScorer(scorers=[DirectContradictionScorer(llm)])
    check_contradiction(
        "claim",
        llm=llm,
        top_k=3,
        threshold=0.0,
        search_fn=_fake_search(bundles),
        scorer=scorer,
    )
    # Only 3 LLM calls should have been made (3 unique candidates x 1 scorer)
    assert llm.chat.call_count == 3


@pytest.mark.unit
def test_check_contradiction_sorts_by_score_descending() -> None:
    """Results are sorted by score descending."""
    llm = MagicMock()
    # Return alternating scores
    llm.chat.side_effect = [
        _llm_response(0.7, "moderate"),
        _llm_response(0.9, "strong"),
        _llm_response(0.8, "high"),
    ]

    bundles = [_make_search_result(f"doc{i}.md", "content") for i in range(3)]
    results = check_contradiction("claim", llm=llm, threshold=0.0, search_fn=_fake_search(bundles))
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(0.9)


@pytest.mark.unit
def test_check_contradiction_handles_llm_exception() -> None:
    """Skips a document when LLM call raises — does not crash."""
    from kairix.knowledge.contradict.scorers import (
        CompositeContradictionScorer,
        DirectContradictionScorer,
    )

    llm = MagicMock()
    llm.chat.side_effect = [RuntimeError("LLM timeout"), _llm_response(0.8)]

    bundles = [
        _make_search_result("fail.md", "will fail"),
        _make_search_result("ok.md", "will succeed"),
    ]
    # Use a single-scorer composite so each candidate gets exactly one LLM call
    # — keeps the test's intent (one fail, one succeed) intact under WS2-B's
    # three-category composite which would otherwise consume 3 side-effects per candidate.
    scorer = CompositeContradictionScorer(scorers=[DirectContradictionScorer(llm)])
    results = check_contradiction(
        "claim",
        llm=llm,
        threshold=0.5,
        search_fn=_fake_search(bundles),
        scorer=scorer,
    )
    assert len(results) == 1
    assert results[0].doc_path == "ok.md"


@pytest.mark.unit
def test_check_contradiction_no_search_results() -> None:
    """Returns [] when search returns no results."""
    llm = MagicMock()
    results = check_contradiction("claim", llm=llm, search_fn=_fake_search([]))
    assert results == []
    llm.chat.assert_not_called()


@pytest.mark.unit
def test_contradiction_result_dataclass_fields() -> None:
    """ContradictionResult holds all expected fields."""
    r = ContradictionResult(
        doc_path="x/y.md",
        score=0.75,
        reason="because of X",
        snippet="relevant text...",
    )
    assert r.doc_path == "x/y.md"
    assert r.score == pytest.approx(0.75)
    assert r.reason == "because of X"
    assert "relevant" in r.snippet


@pytest.mark.unit
def test_check_contradiction_snippet_truncated_to_300_chars() -> None:
    """Snippet stored in result is capped at 300 chars."""
    llm = MagicMock()
    llm.chat.return_value = _llm_response(0.9)

    long_content = "X" * 1000
    bundles = [_make_search_result("doc.md", long_content)]
    results = check_contradiction("claim", llm=llm, threshold=0.0, search_fn=_fake_search(bundles))
    assert len(results[0].snippet) <= 300
