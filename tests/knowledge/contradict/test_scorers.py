"""Unit tests for ContradictionScorer Strategies + Composite.

Tests via the public scorer surface using a small fake LLM. No @patch,
no monkeypatch, no private symbol imports.
"""

from __future__ import annotations

import pytest

from kairix.knowledge.contradict.scorers import (
    CompositeContradictionScorer,
    DirectContradictionScorer,
    OverstatementScorer,
    StatusMismatchScorer,
    default_contradiction_scorer,
    parse_llm_score,
)


class _FakeLLM:
    """LLM stub: returns canned response strings for each chat() call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self._responses.pop(0) if self._responses else "{}"


# parse_llm_score ------------------------------------------------------


@pytest.mark.unit
def test_parse_llm_score_valid_json() -> None:
    score, reason = parse_llm_score('{"score": 0.8, "reason": "clear conflict"}')
    assert score == pytest.approx(0.8)
    assert reason == "clear conflict"


@pytest.mark.unit
def test_parse_llm_score_with_preamble_extracts_object() -> None:
    """LLM may emit prose before the JSON; the parser still finds it."""
    raw = 'Here is my analysis: {"score": 0.7, "reason": "overstatement"}'
    score, reason = parse_llm_score(raw)
    assert score == pytest.approx(0.7)
    assert "overstatement" in reason


@pytest.mark.unit
def test_parse_llm_score_clamps_to_unit_range() -> None:
    above, _ = parse_llm_score('{"score": 1.5, "reason": ""}')
    below, _ = parse_llm_score('{"score": -0.3, "reason": ""}')
    assert above == 1.0
    assert below == 0.0


@pytest.mark.unit
def test_parse_llm_score_returns_zero_on_garbage() -> None:
    score, reason = parse_llm_score("not json at all")
    assert score == 0.0
    assert reason == ""


@pytest.mark.unit
def test_parse_llm_score_handles_empty_input() -> None:
    score, reason = parse_llm_score("")
    assert score == 0.0
    assert reason == ""


# Single-category scorers ----------------------------------------------


@pytest.mark.unit
def test_direct_scorer_uses_direct_prompt() -> None:
    llm = _FakeLLM(['{"score": 0.9, "reason": "direct conflict"}'])
    scorer = DirectContradictionScorer(llm)

    score, reason = scorer.score("claim X is true", "evidence X is false")

    assert score == pytest.approx(0.9)
    assert reason == "direct conflict"
    assert scorer.category == "direct"
    # The prompt sent to the LLM mentions "directly" — distinguishing it from the others
    sent = llm.calls[0][0]["content"].lower()
    assert "direct" in sent


@pytest.mark.unit
def test_overstatement_scorer_uses_overstatement_prompt() -> None:
    llm = _FakeLLM(['{"score": 0.6, "reason": "claim overstates"}'])
    scorer = OverstatementScorer(llm)

    score, reason = scorer.score("X is the only one", "Y also does it")

    assert score == pytest.approx(0.6)
    assert scorer.category == "overstatement"
    assert "overstate" in llm.calls[0][0]["content"].lower()


@pytest.mark.unit
def test_status_mismatch_scorer_uses_status_prompt() -> None:
    llm = _FakeLLM(['{"score": 0.55, "reason": "status differs"}'])
    scorer = StatusMismatchScorer(llm)

    score, _ = scorer.score("X is published", "X is unpublished")

    assert score == pytest.approx(0.55)
    assert scorer.category == "status_mismatch"
    assert "status" in llm.calls[0][0]["content"].lower()


@pytest.mark.unit
def test_scorer_returns_zero_on_llm_failure() -> None:
    """When the LLM raises, the scorer must return (0.0, '') rather than propagating."""

    class _BrokenLLM:
        def chat(self, messages: list[dict]) -> str:
            raise RuntimeError("rate-limited")

    score, reason = DirectContradictionScorer(_BrokenLLM()).score("a", "b")
    assert score == 0.0
    assert reason == ""


# Composite ------------------------------------------------------------


@pytest.mark.unit
def test_composite_aggregates_by_max() -> None:
    """The winning category is the one with the highest score."""
    llm = _FakeLLM(
        [
            '{"score": 0.3, "reason": "weak direct"}',
            '{"score": 0.85, "reason": "strong overstatement"}',
            '{"score": 0.4, "reason": "moderate status"}',
        ]
    )
    composite = CompositeContradictionScorer(
        scorers=[DirectContradictionScorer(llm), OverstatementScorer(llm), StatusMismatchScorer(llm)]
    )

    score, reason = composite.score("claim", "candidate")

    assert score == pytest.approx(0.85)
    assert "overstatement" in reason


@pytest.mark.unit
def test_composite_best_category_returns_winner() -> None:
    llm = _FakeLLM(
        [
            '{"score": 0.2, "reason": "weak"}',
            '{"score": 0.5, "reason": "moderate"}',
            '{"score": 0.9, "reason": "strong status mismatch"}',
        ]
    )
    composite = CompositeContradictionScorer(
        scorers=[DirectContradictionScorer(llm), OverstatementScorer(llm), StatusMismatchScorer(llm)]
    )

    cat, score, reason = composite.best_category("claim", "candidate")

    assert cat == "status_mismatch"
    assert score == pytest.approx(0.9)
    assert "strong" in reason


@pytest.mark.unit
def test_composite_score_all_returns_per_category_breakdown() -> None:
    llm = _FakeLLM(
        [
            '{"score": 0.1, "reason": "a"}',
            '{"score": 0.2, "reason": "b"}',
            '{"score": 0.3, "reason": "c"}',
        ]
    )
    composite = CompositeContradictionScorer(
        scorers=[DirectContradictionScorer(llm), OverstatementScorer(llm), StatusMismatchScorer(llm)]
    )

    breakdown = composite.score_all("claim", "candidate")

    assert set(breakdown.keys()) == {"direct", "overstatement", "status_mismatch"}
    assert breakdown["direct"] == (pytest.approx(0.1), "a")
    assert breakdown["overstatement"] == (pytest.approx(0.2), "b")
    assert breakdown["status_mismatch"] == (pytest.approx(0.3), "c")


@pytest.mark.unit
def test_composite_with_no_scorers_returns_zero() -> None:
    composite = CompositeContradictionScorer(scorers=[])
    score, reason = composite.score("claim", "candidate")
    assert score == 0.0
    assert reason == ""


@pytest.mark.unit
def test_default_factory_constructs_three_category_composite() -> None:
    llm = _FakeLLM(['{"score": 0.0}'] * 10)
    composite = default_contradiction_scorer(llm)
    breakdown = composite.score_all("a", "b")
    assert set(breakdown.keys()) == {"direct", "overstatement", "status_mismatch"}
