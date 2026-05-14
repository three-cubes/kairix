"""Tests for cross-encoder re-ranking module.

Drives all behaviour through the ``rerank()`` public surface, the public
``get_cross_encoder()`` lazy-loader, and the ``encoder=`` DI seam. Module-level
singleton state is reset between lazy-loader tests via ``importlib.reload``
so the success / ImportError / generic-Exception paths can each be exercised
deterministically without importing or mutating private names directly.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import kairix.core.search.rerank as rerank_module
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


# ---------------------------------------------------------------------------
# Lazy-loader public surface — get_cross_encoder()
#
# These tests reload the module to reset its singleton state, then drive the
# public ``get_cross_encoder()`` function under controlled ``sys.modules``
# state. ``sys.modules['sentence_transformers']`` is a third-party namespace
# (not a kairix internal), so manipulating it does not violate F1.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_rerank_module() -> ModuleType:
    """Reload ``kairix.core.search.rerank`` so its lazy-singleton state is
    reset between tests. Returns the freshly-loaded module."""
    return importlib.reload(rerank_module)


@pytest.fixture
def no_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the lazy-loader's ``from sentence_transformers import CrossEncoder``
    to raise ``ImportError`` regardless of whether the package is installed.

    Setting ``sys.modules['sentence_transformers'] = None`` is the documented
    Python convention for blocking an import: the import machinery sees the
    sentinel and raises ``ImportError``. This is third-party namespace
    manipulation and does not touch any kairix internal."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


@pytest.fixture
def stub_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Inject a stub ``sentence_transformers`` module exposing ``CrossEncoder``
    so the lazy-loader's success path can be exercised without pulling in
    the real ~22 MB model. Yields the stub module so tests can swap behaviour
    (e.g. raise during construction)."""
    stub = ModuleType("sentence_transformers")

    class _StubCrossEncoder:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def predict(self, pairs: list[tuple[str, str]]) -> _ScoreArray:
            return _ScoreArray([0.0] * len(pairs))

    # type: ignore[attr-defined] — ModuleType has no static CrossEncoder attr;
    # this is dynamic stub injection to drive the rerank lazy-loader.
    stub.CrossEncoder = _StubCrossEncoder  # type: ignore[attr-defined]  # dynamic stub injection — see comment above
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    return stub


@pytest.mark.unit
def test_get_cross_encoder_returns_none_on_import_error(
    fresh_rerank_module: ModuleType,
    no_sentence_transformers: None,
) -> None:
    """Per docstring: returns None on import failure (sentence-transformers
    not installed). Sabotage-prove by re-asserting after a second call —
    the cached state must continue to return None, not silently swap in a
    truthy value."""
    out = fresh_rerank_module.get_cross_encoder("any-model")
    assert out is None
    # Second call uses the cached-checked branch.
    again = fresh_rerank_module.get_cross_encoder("any-model")
    assert again is None


@pytest.mark.unit
def test_get_cross_encoder_constructs_and_caches_encoder_on_success(
    fresh_rerank_module: ModuleType,
    stub_sentence_transformers: ModuleType,
) -> None:
    """Per docstring: the model is loaded on first call and reused for
    subsequent calls. Sabotage-prove by swapping the stub's CrossEncoder
    after the first call — the cached instance MUST be returned, not a new
    one constructed from the swapped class."""
    first = fresh_rerank_module.get_cross_encoder("test-model-name")
    assert first is not None
    assert first.model_name == "test-model-name"

    # Swap the stub class so any new construction would produce a different
    # instance. The cache means the cached `first` is what we get back.
    class _OtherCrossEncoder:
        def __init__(self, model_name: str) -> None:
            self.model_name = "should-not-be-used"

    stub_sentence_transformers.CrossEncoder = _OtherCrossEncoder  # type: ignore[attr-defined]  # dynamic stub mutation on injected module

    second = fresh_rerank_module.get_cross_encoder("a-different-model")
    assert second is first  # cached singleton
    assert second.model_name == "test-model-name"


@pytest.mark.unit
def test_get_cross_encoder_returns_none_on_construction_error(
    fresh_rerank_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per docstring: any non-ImportError failure during model load (corrupt
    weights, bad model name, OOM) returns None. Sabotage-prove by also
    asserting the cached state — a subsequent call must continue to return
    None even though the failing class is still in sys.modules."""
    stub = ModuleType("sentence_transformers")

    class _BlowingUpCrossEncoder:
        def __init__(self, model_name: str) -> None:
            raise RuntimeError("simulated model load failure")

    stub.CrossEncoder = _BlowingUpCrossEncoder  # type: ignore[attr-defined]  # dynamic stub injection on synthesised ModuleType
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)

    out = fresh_rerank_module.get_cross_encoder("broken-model")
    assert out is None
    # The cached-checked branch still returns None.
    out2 = fresh_rerank_module.get_cross_encoder("broken-model")
    assert out2 is None


@pytest.mark.unit
def test_get_cross_encoder_default_model_argument(
    fresh_rerank_module: ModuleType,
    no_sentence_transformers: None,
) -> None:
    """Public ``get_cross_encoder()`` accepts a default model argument and
    returns None when sentence-transformers is unavailable. Confirms the
    default-arg path of the public alias is reachable."""
    out = fresh_rerank_module.get_cross_encoder()
    assert out is None


# ---------------------------------------------------------------------------
# rerank() ↔ lazy-loader integration
#
# When ``encoder=None``, ``rerank()`` falls through to the lazy loader.
# These tests exercise that path end-to-end with controlled sys.modules state.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rerank_falls_back_to_lazy_loader_when_encoder_is_none(
    fresh_rerank_module: ModuleType,
    no_sentence_transformers: None,
) -> None:
    """Per docstring: ``encoder=None`` triggers lazy-load; on import failure
    the function returns the input list unchanged. This covers the
    ``encoder is None`` -> ``_get_cross_encoder`` -> ``return results``
    path inside ``rerank()`` itself."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    out = fresh_rerank_module.rerank("query", results)
    assert out == results
    # Sabotage-prove: scores are NOT touched (no rerank happened).
    # Use approx — float equality on the default sentinel still triggers S1244.
    assert all(r.rerank_score == pytest.approx(0.0) for r in out)


@pytest.mark.unit
def test_rerank_uses_lazy_loaded_encoder_when_encoder_arg_omitted(
    fresh_rerank_module: ModuleType,
    stub_sentence_transformers: ModuleType,
) -> None:
    """Per docstring: when the ``encoder`` kwarg is omitted, ``rerank()``
    pulls the lazy-loaded singleton. With a working stub injected via
    ``sys.modules``, the encoder is constructed once and used. Sabotage-
    prove by asserting that ``rerank_score`` was overwritten on each
    result (the stub returns 0.0 for all pairs, but the field is still
    populated, distinguishing this from the import-failure short-circuit)."""

    # Override the stub to produce a discriminating signal.
    class _RecordingCrossEncoder:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def predict(self, pairs: list[tuple[str, str]]) -> _ScoreArray:
            return _ScoreArray([1.5] * len(pairs))

    stub_sentence_transformers.CrossEncoder = _RecordingCrossEncoder  # type: ignore[attr-defined]  # dynamic stub mutation on injected module

    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    out = fresh_rerank_module.rerank("query", results)
    # Encoder ran — every result got the stub score (sabotage check: a
    # broken short-circuit would leave rerank_score at 0.0).
    assert all(r.rerank_score == pytest.approx(1.5) for r in out)
    assert all(r.boosted_score == pytest.approx(1.5) for r in out)


# ---------------------------------------------------------------------------
# Edge case — encoder returns fewer scores than candidates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rerank_handles_encoder_returning_fewer_scores_than_candidates() -> None:
    """If the LLM/cross-encoder backend returns fewer scores than candidates,
    ``zip(..., strict=False)`` truncates pairwise — extra candidates keep
    their pre-rerank ``boosted_score`` and ``rerank_score`` defaults (0.0).
    The function MUST NOT raise and MUST return the full candidate set
    (count preserved)."""
    results = [
        _make_result("a.md", 0.9),
        _make_result("b.md", 0.5),
        _make_result("c.md", 0.3),
    ]
    # Encoder returns ONLY 2 scores for 3 candidates.
    encoder = _StubEncoder(scores=[2.0, 1.0])
    out = rerank("query", results, encoder=encoder)

    # Count preserved — no candidate was dropped.
    assert len(out) == 3
    paths_out = sorted(r.path for r in out)
    assert paths_out == ["a.md", "b.md", "c.md"]

    # The third candidate kept its default rerank_score (0.0) because the
    # encoder didn't score it. This is the documented zip-truncate
    # behaviour; the test pins it so a future refactor that pads/raises
    # is a deliberate decision rather than a silent regression.
    by_path = {r.path: r for r in out}
    # approx 0.0 matches both the bare default and any tiny-epsilon variant (S1244).
    assert by_path["c.md"].rerank_score == pytest.approx(0.0)
