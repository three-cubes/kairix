"""Tests for cross-encoder re-ranking module.

Drives all behaviour through the ``rerank()`` public surface, the pure
``load_cross_encoder()`` loader, the memoising ``get_cross_encoder()`` (reset
via ``reset_cross_encoder_cache()``), and the ``encoder=`` DI seam — no module
reload, no mutation of private names (F1/F5). The loader's
not-installed / load-failure / success paths are driven by putting ``None``
or a stub in ``sys.modules["sentence_transformers"]`` (a third-party key).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from kairix.core.search.rerank import (
    RERANK_CANDIDATE_LIMIT,
    RERANK_MODEL,
    get_cross_encoder,
    load_cross_encoder,
    rerank,
    reset_cross_encoder_cache,
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
# Loader + memo — load_cross_encoder() / get_cross_encoder()
#
# ``sys.modules['sentence_transformers']`` is a third-party namespace (not a
# kairix internal), so injecting ``None`` / a stub there does not violate F1.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_memo() -> Iterator[None]:
    """Start and end with an empty process-wide cross-encoder memo."""
    reset_cross_encoder_cache()
    yield
    reset_cross_encoder_cache()


@pytest.fixture
def no_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from sentence_transformers import CrossEncoder`` raise ImportError.

    ``sys.modules[name] = None`` is the documented Python convention for
    blocking an import, regardless of whether the package is installed."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


def _stub_module(cross_encoder: type) -> ModuleType:
    stub = ModuleType("sentence_transformers")
    stub.CrossEncoder = cross_encoder  # type: ignore[attr-defined]  # dynamic stub injection on synthesised ModuleType
    return stub


def _recording_module(constructed: list[str]) -> ModuleType:
    """A stub ``sentence_transformers`` whose ``CrossEncoder`` records each construction."""

    class _StubCrossEncoder:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name
            constructed.append(model_name)

        def predict(self, pairs: list[tuple[str, str]]) -> _ScoreArray:
            return _ScoreArray([1.5] * len(pairs))

    return _stub_module(_StubCrossEncoder)


@pytest.fixture
def stub_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Inject a recording stub ``sentence_transformers``; returns its construction record."""
    constructed: list[str] = []
    monkeypatch.setitem(sys.modules, "sentence_transformers", _recording_module(constructed))
    return constructed


def _failing_module(error: Exception) -> ModuleType:
    class _FailingCrossEncoder:
        def __init__(self, model_name: str) -> None:
            raise error

    return _stub_module(_FailingCrossEncoder)


@pytest.mark.unit
def test_load_cross_encoder_returns_none_when_not_installed(
    no_sentence_transformers: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="kairix.core.search.rerank"):
        assert load_cross_encoder("any-model") is None
    assert any("not installed" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_load_cross_encoder_constructs_the_named_model(stub_sentence_transformers: list[str]) -> None:
    encoder = load_cross_encoder("test-model-name")
    assert encoder.model_name == "test-model-name"
    assert load_cross_encoder().model_name == RERANK_MODEL
    assert stub_sentence_transformers == ["test-model-name", RERANK_MODEL]


@pytest.mark.unit
def test_load_cross_encoder_returns_none_on_construction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any failure during model load (corrupt weights, bad model name, OOM) returns None."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", _failing_module(RuntimeError("simulated failure")))
    assert load_cross_encoder("broken-model") is None


@pytest.mark.unit
def test_import_error_during_model_load_is_reported_as_a_load_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``ImportError`` raised WHILE the model loads (the package itself
    imported fine) is a load failure — logged as such — not mis-reported as
    "sentence-transformers not installed".

    Sabotage proof (executed): fold the ``CrossEncoder(model)`` call back into
    the import's ``try`` (the pre-fix shape) → the "not installed" message is
    logged instead and the assertion fails; restored.
    """
    monkeypatch.setitem(sys.modules, "sentence_transformers", _failing_module(ImportError("torch backend unavailable")))
    with caplog.at_level("WARNING", logger="kairix.core.search.rerank"):
        assert load_cross_encoder("model-x") is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("failed to load model" in m and "torch backend unavailable" in m for m in messages), messages
    assert not any("not installed" in m for m in messages), messages


@pytest.mark.unit
def test_get_cross_encoder_loads_once_until_reset(fresh_memo: None, stub_sentence_transformers: list[str]) -> None:
    """The first call loads; later calls (any model name) reuse that encoder
    until ``reset_cross_encoder_cache()``.

    Sabotage proof (executed): drop the ``_cross_encoder_checked = True``
    assignment → the second call constructs again and this fails; restored.
    """
    first = get_cross_encoder("test-model-name")
    assert get_cross_encoder("a-different-model") is first
    assert stub_sentence_transformers == ["test-model-name"]

    reset_cross_encoder_cache()
    assert get_cross_encoder("after-reset").model_name == "after-reset"
    assert stub_sentence_transformers == ["test-model-name", "after-reset"]


@pytest.mark.unit
def test_concurrent_first_calls_construct_the_model_once(fresh_memo: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two threads racing the first ``get_cross_encoder()`` (the shared rerank
    executor's shape) construct the model exactly once and share it.

    Both threads start together on a barrier. The stub constructor then waits
    briefly for a second constructor to arrive: unserialised, both threads
    meet inside it (two constructions); serialised by the memo lock, the wait
    times out with one thread inside and the other gets the stored instance.

    Sabotage proof (executed): drop the ``with _MEMO_LOCK:`` around the
    check -> load -> store → two constructions and this fails; restored.
    """
    constructed: list[str] = []
    both_inside = threading.Barrier(2)

    class _SlowCrossEncoder:
        def __init__(self, model_name: str) -> None:
            constructed.append(model_name)
            try:
                both_inside.wait(timeout=0.5)  # passes only if a second constructor runs concurrently
            except threading.BrokenBarrierError:
                pass

    monkeypatch.setitem(sys.modules, "sentence_transformers", _stub_module(_SlowCrossEncoder))
    start = threading.Barrier(2)
    results: list[object] = []

    def _first_call() -> None:
        start.wait()
        results.append(get_cross_encoder())

    threads = [threading.Thread(target=_first_call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert constructed == [RERANK_MODEL]
    assert len(results) == 2
    assert results[0] is results[1]


@pytest.mark.unit
def test_get_cross_encoder_remembers_a_failed_load(fresh_memo: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed load is remembered: a broken install never retries per query."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert get_cross_encoder() is None
    monkeypatch.setitem(sys.modules, "sentence_transformers", _recording_module([]))
    assert get_cross_encoder() is None


# ---------------------------------------------------------------------------
# rerank() ↔ lazy-loader integration
#
# When ``encoder=None``, ``rerank()`` falls through to ``get_cross_encoder``.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rerank_falls_back_to_lazy_loader_when_encoder_is_none(
    fresh_memo: None,
    no_sentence_transformers: None,
) -> None:
    """``encoder=None`` triggers the lazy load; on import failure the function
    returns the input list unchanged."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    out = rerank("query", results)
    assert out == results
    # Scores are NOT touched (no rerank happened). Use approx — float
    # equality on the default sentinel still triggers S1244.
    assert all(r.rerank_score == pytest.approx(0.0) for r in out)


@pytest.mark.unit
def test_rerank_uses_lazy_loaded_encoder_when_encoder_arg_omitted(
    fresh_memo: None,
    stub_sentence_transformers: list[str],
) -> None:
    """With the ``encoder`` kwarg omitted, ``rerank()`` uses the lazily loaded
    encoder: every result gets the stub's 1.5 score (a broken short-circuit
    would leave rerank_score at 0.0)."""
    results = [_make_result("a.md", 0.9), _make_result("b.md", 0.5)]
    out = rerank("query", results)
    assert all(r.rerank_score == pytest.approx(1.5) for r in out)
    assert all(r.boosted_score == pytest.approx(1.5) for r in out)
    assert stub_sentence_transformers == [RERANK_MODEL]


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
