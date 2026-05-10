"""Tests for cross-encoder re-ranking module.

Drives all behaviour through the ``rerank()`` public surface and the ``encoder=``
DI seam. The internal singleton + lazy-load mechanics are an implementation
detail — never tested directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kairix.core.search.rerank import (
    RERANK_CANDIDATE_LIMIT,
    RERANK_MODEL,
    rerank,
)
from kairix.core.search.rrf import FusedResult


def _make_result(path: str, score: float, snippet: str = "") -> FusedResult:
    return FusedResult(
        path=path,
        collection="test",
        title=path,
        snippet=snippet or f"Snippet for {path}",
        rrf_score=score,
        boosted_score=score,
    )


class _StubEncoder:
    """Minimal cross-encoder-shaped stub (DI seam).

    Production `CrossEncoder.predict()` returns a numpy array; we mimic via
    a list-like with `.tolist()`.
    """

    def __init__(self, scores: list[float] | None = None, raises: Exception | None = None) -> None:
        self.scores = list(scores or [])
        self.raises = raises
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]]):
        self.calls.append(list(pairs))
        if self.raises is not None:
            raise self.raises
        return _ScoreArray(self.scores[: len(pairs)])


class _ScoreArray:
    """numpy-array-shaped stub with `.tolist()`."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def tolist(self) -> list[float]:
        return list(self._scores)


# ---------------------------------------------------------------------------
# Public-surface behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reorders_by_cross_encoder_score() -> None:
    results = [
        _make_result("a.md", 0.9, snippet="irrelevant content"),
        _make_result("b.md", 0.5, snippet="highly relevant content"),
    ]
    encoder = _StubEncoder(scores=[0.1, 0.9])
    out = rerank("highly relevant query", results, encoder=encoder)
    assert out[0].path == "b.md"
    assert out[1].path == "a.md"


@pytest.mark.unit
def test_overwrites_boosted_score_with_rerank_score() -> None:
    """Per docstring: boosted_score is overwritten with the rerank score so
    apply_budget (which sorts by boosted_score) respects the new order."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.1)]
    encoder = _StubEncoder(scores=[3.0, 7.0])
    out = rerank("query", results, encoder=encoder)
    assert out[0].path == "b.md"
    assert out[0].boosted_score == pytest.approx(7.0)


@pytest.mark.unit
def test_tail_results_appended_unchanged() -> None:
    """Per docstring: results beyond candidate_limit are appended after the
    re-ranked candidates, preserving their original relative order."""
    many = [_make_result(f"{i}.md", float(i)) for i in range(25)]
    encoder = _StubEncoder(scores=[float(i) for i in range(RERANK_CANDIDATE_LIMIT)])
    out = rerank("query", many, candidate_limit=RERANK_CANDIDATE_LIMIT, encoder=encoder)

    assert len(out) == 25
    # Tail paths are the original 20-24 in original order.
    tail_paths = [r.path for r in out[RERANK_CANDIDATE_LIMIT:]]
    assert tail_paths == [f"{i}.md" for i in range(RERANK_CANDIDATE_LIMIT, 25)]


@pytest.mark.unit
def test_returns_unchanged_on_inference_error() -> None:
    """Per docstring: on any error the function returns input unchanged."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    encoder = _StubEncoder(raises=RuntimeError("inference failed"))
    out = rerank("query", results, encoder=encoder)
    assert out == results


@pytest.mark.unit
def test_empty_results_returned_unchanged() -> None:
    out = rerank("query", [], encoder=_StubEncoder(scores=[]))
    assert out == []


@pytest.mark.unit
def test_rerank_score_field_populated() -> None:
    results = [_make_result("a.md", 0.5)]
    encoder = _StubEncoder(scores=[4.2])
    out = rerank("query", results, encoder=encoder)
    assert out[0].rerank_score == pytest.approx(4.2)


@pytest.mark.unit
def test_snippet_truncated_to_500_chars_before_passing_to_encoder() -> None:
    """Per docstring: re-ranking uses snippet[:500] to stay within latency budget."""
    long_snippet = "x" * 1000
    results = [_make_result("a.md", 0.5, snippet=long_snippet)]
    encoder = _StubEncoder(scores=[1.0])
    rerank("query", results, encoder=encoder)
    assert len(encoder.calls[0][0][1]) == 500


@pytest.mark.unit
def test_uses_title_when_snippet_empty() -> None:
    """When snippet is empty/falsy, title is used instead."""
    result = FusedResult(
        path="doc.md",
        collection="test",
        title="doc.md",
        snippet="",
        rrf_score=0.5,
        boosted_score=0.5,
    )
    encoder = _StubEncoder(scores=[2.0])
    rerank("query", [result], encoder=encoder)
    assert encoder.calls[0][0][1] == "doc.md"


@pytest.mark.unit
def test_single_result_reranked() -> None:
    results = [_make_result("only.md", 0.3)]
    encoder = _StubEncoder(scores=[5.5])
    out = rerank("query", results, encoder=encoder)
    assert len(out) == 1
    assert out[0].rerank_score == pytest.approx(5.5)
    assert out[0].boosted_score == pytest.approx(5.5)


@pytest.mark.unit
def test_custom_candidate_limit_caps_encoder_calls() -> None:
    """Custom candidate_limit controls how many results are re-scored."""
    results = [_make_result(f"{i}.md", float(i)) for i in range(10)]
    encoder = _StubEncoder(scores=[float(i) for i in range(3)])
    out = rerank("query", results, candidate_limit=3, encoder=encoder)
    assert len(out) == 10
    # Encoder called with exactly 3 pairs.
    assert len(encoder.calls[0]) == 3


@pytest.mark.unit
def test_negative_scores_sort_correctly() -> None:
    """Cross-encoders can return negative scores; descending sort still applies."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    encoder = _StubEncoder(scores=[-2.0, -0.5])
    out = rerank("query", results, encoder=encoder)
    # -0.5 > -2.0 → b ranks first.
    assert out[0].path == "b.md"
    assert out[1].path == "a.md"


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_model_constant() -> None:
    """RERANK_MODEL is the expected default."""
    assert RERANK_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2"


@pytest.mark.unit
def test_default_candidate_limit() -> None:
    """RERANK_CANDIDATE_LIMIT is 20."""
    assert RERANK_CANDIDATE_LIMIT == 20


# ---------------------------------------------------------------------------
# Contract surface — query & encoder pairing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_is_first_element_of_each_pair_passed_to_encoder() -> None:
    """The cross-encoder receives (query, doc_text) pairs — query first."""
    results = [_make_result("a.md", 0.5), _make_result("b.md", 0.4)]
    encoder = _StubEncoder(scores=[1.0, 2.0])
    rerank("the canonical query", results, encoder=encoder)
    pairs = encoder.calls[0]
    assert pairs[0][0] == "the canonical query"
    assert pairs[1][0] == "the canonical query"


@pytest.mark.unit
def test_results_with_fewer_than_candidate_limit_are_all_reranked() -> None:
    """When results count < candidate_limit, all results are re-scored."""
    results = [_make_result(f"{i}.md", float(i)) for i in range(5)]
    encoder = _StubEncoder(scores=[float(i) for i in range(5)])
    out = rerank("q", results, candidate_limit=20, encoder=encoder)
    # All 5 reordered, no tail.
    assert len(out) == 5
    assert len(encoder.calls[0]) == 5


@pytest.mark.unit
def test_returns_unchanged_when_encoder_arg_is_explicit_falsy_via_mock() -> None:
    """Passing an encoder whose predict immediately raises ImportError must
    surface as unchanged results — covers the production failure mode where
    sentence-transformers isn't installed.
    """
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    encoder = MagicMock()
    encoder.predict.side_effect = ImportError("sentence-transformers not installed")
    out = rerank("q", results, encoder=encoder)
    # Unchanged — same objects, same order.
    assert out == results
