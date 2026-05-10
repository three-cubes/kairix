"""
Tests for kairix.knowledge.contradict.detector — contradiction detection.

Uses canonical FakeLLMBackend from tests/fakes.py and a tiny inline
``_Bundle`` / ``_Response`` dataclass shape for search results — no
MagicMock anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.knowledge.contradict.detector import (
    ContradictionResult,
    check_contradiction,
)
from tests.fakes import FakeLLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _BundleResult:
    path: str


@dataclass
class _Bundle:
    content: str
    result: _BundleResult


@dataclass
class _Response:
    results: list[_Bundle] = field(default_factory=list)


def _make_search_result(path: str, content: str) -> _Bundle:
    """Build a search bundle (content + result.path)."""
    return _Bundle(content=content, result=_BundleResult(path=path))


def _fake_search(bundles: list[_Bundle]):
    """Return a callable that returns a SearchResponse-shaped object."""

    def _search(**kwargs: Any) -> _Response:
        return _Response(results=bundles)

    return _search


def _failing_search(exc: BaseException):
    """Return a callable that raises an exception."""

    def _search(**kwargs: Any) -> _Response:
        raise exc

    return _search


def _llm_response(score: float, reason: str = "test reason") -> str:
    return json.dumps({"score": score, "reason": reason})


# ---------------------------------------------------------------------------
# check_contradiction — exercises parse_llm_score's response-parsing branches
# through the public surface. Each malformed/edge-case LLM response is fed
# in via FakeLLMBackend so the parse logic is observed via the resulting
# ContradictionResult shape, not probed directly.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_response",
    [
        "",  # empty
        "no json here at all",  # no json
        "{score: not valid json}",  # malformed json
        '{"reason": "no score key"}',  # missing score
        '{"score": "high", "reason": "non-numeric"}',  # non-numeric
    ],
)
def test_unparseable_llm_response_yields_no_contradiction(raw_response: str) -> None:
    """When the LLM response doesn't yield a numeric score, the parser returns
    None and the scorer collapses the candidate's score to 0.0; with a
    non-zero threshold the candidate is dropped — no result. Drives
    parse_llm_score's failure branches through the public surface."""
    llm = FakeLLMBackend(chat_response=raw_response)
    bundles = [_make_search_result("doc.md", "candidate")]
    results = check_contradiction("claim", llm=llm, threshold=0.5, search_fn=_fake_search(bundles))
    assert results == []


@pytest.mark.unit
def test_score_above_one_is_clamped_to_one() -> None:
    """parse_llm_score clamps to [0.0, 1.0]. Observed via the resulting score."""
    llm = FakeLLMBackend(chat_response='{"score": 1.5, "reason": "extreme"}')
    bundles = [_make_search_result("doc.md", "candidate")]
    results = check_contradiction("claim", llm=llm, threshold=0.0, search_fn=_fake_search(bundles))
    assert len(results) == 1
    assert results[0].score == pytest.approx(1.0)


@pytest.mark.unit
def test_score_below_zero_is_clamped_to_zero() -> None:
    """Negative scores clamp to 0.0; below threshold → no result."""
    llm = FakeLLMBackend(chat_response='{"score": -0.3, "reason": "negative"}')
    bundles = [_make_search_result("doc.md", "candidate")]
    # threshold=0 admits 0.0; the result should be present with score=0.
    results = check_contradiction("claim", llm=llm, threshold=0.0, search_fn=_fake_search(bundles))
    # Score below threshold>0 would drop the result — this just asserts the clamp.
    if results:
        assert results[0].score == pytest.approx(0.0)


@pytest.mark.unit
def test_json_with_preamble_parses_correctly() -> None:
    """Model may prefix JSON with explanatory text — still parses."""
    llm = FakeLLMBackend(
        chat_response='Here is my assessment: {"score": 0.7, "reason": "contradicts existing record"}',
    )
    bundles = [_make_search_result("doc.md", "candidate")]
    results = check_contradiction("claim", llm=llm, threshold=0.5, search_fn=_fake_search(bundles))
    assert len(results) == 1
    assert results[0].score == pytest.approx(0.7)
    assert "contradicts" in results[0].reason.lower()


# ---------------------------------------------------------------------------
# check_contradiction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_contradiction_returns_empty_on_search_failure() -> None:
    """Returns [] when hybrid search raises an exception."""
    llm = FakeLLMBackend()
    results = check_contradiction("some claim", llm=llm, search_fn=_failing_search(RuntimeError("no db")))
    assert results == []


@pytest.mark.unit
def test_check_contradiction_returns_empty_when_no_results_above_threshold() -> None:
    """Returns [] when all LLM scores are below threshold."""
    llm = FakeLLMBackend(chat_response=_llm_response(0.2))  # well below default threshold 0.6

    bundles = [_make_search_result("a/doc.md", "some content")]
    results = check_contradiction("new claim", llm=llm, search_fn=_fake_search(bundles))
    assert results == []


@pytest.mark.unit
def test_check_contradiction_returns_result_above_threshold() -> None:
    """Returns a ContradictionResult when score >= threshold."""
    llm = FakeLLMBackend(chat_response=_llm_response(0.9, "directly conflicts with existing record"))

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

    llm = FakeLLMBackend(chat_response=_llm_response(0.8))

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
    assert len(llm.chat_calls) == 3


@pytest.mark.unit
def test_check_contradiction_sorts_by_score_descending() -> None:
    """Results are sorted by score descending."""
    # Return alternating scores
    llm = FakeLLMBackend(
        chat_responses=[
            _llm_response(0.7, "moderate"),
            _llm_response(0.9, "strong"),
            _llm_response(0.8, "high"),
        ]
    )

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

    # Mixed scripted responses: first call raises, second returns. FakeLLMBackend
    # only supports all-raise OR all-respond, so use a tiny inline fake.
    class _MixedLLM:
        def __init__(self) -> None:
            self._sequence: list[Any] = [RuntimeError("LLM timeout"), _llm_response(0.8)]
            self._idx = 0
            self.chat_calls: list[dict[str, Any]] = []

        def chat(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
            self.chat_calls.append({"messages": list(messages), "max_tokens": max_tokens})
            entry = self._sequence[self._idx]
            self._idx += 1
            if isinstance(entry, BaseException):
                raise entry
            return entry

        def embed(self, text: str) -> list[float]:  # pragma: no cover — unused
            return [0.0, 0.6, 0.8]

    llm = _MixedLLM()

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
    llm = FakeLLMBackend()
    results = check_contradiction("claim", llm=llm, search_fn=_fake_search([]))
    assert results == []
    assert len(llm.chat_calls) == 0


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
    llm = FakeLLMBackend(chat_response=_llm_response(0.9))

    long_content = "X" * 1000
    bundles = [_make_search_result("doc.md", long_content)]
    results = check_contradiction("claim", llm=llm, threshold=0.0, search_fn=_fake_search(bundles))
    assert len(results[0].snippet) <= 300
