"""
Integration test: cross-encoder re-rank wired into the real BM25 -> RRF
fusion path, then re-ranked via the public ``rerank()`` surface.

Why a stub encoder rather than the real model:
  - The real ``cross-encoder/ms-marco-MiniLM-L-6-v2`` weights are ~22 MB and
    require sentence-transformers + torch — too heavy for CI integration.
  - There is no kairix Protocol for ``CrossEncoder`` (it is a third-party
    library type), so the inline ``_StubEncoder`` here is the project's
    sanctioned exception to the no-inline-stubs rule (see CLAUDE.md and
    the rerank task brief).

Why not drive everything through ``SearchPipeline.search()``:
  - ``SearchPipeline.search()`` calls ``apply_budget`` as its terminal step
    and returns ``BudgetedResult`` instances, not the ``FusedResult`` list
    that ``rerank()`` consumes. The real production rerank wiring lives
    in ``hybrid._apply_reranking``, which sits between fusion and budget.
  - This integration test mirrors that placement: real ``BM25SearchBackend``
    delegates to a canonical fake document repo, real ``RRFFusion`` produces
    the ``FusedResult`` list, and ``rerank()`` runs over it. That covers
    the same code path operators see in production, minus the heavyweight
    cross-encoder model.

Hard discipline: no @patch, no monkeypatch, no module singleton mutation,
no private-fn imports — every test drives the public ``rerank()`` surface.
"""

from __future__ import annotations

import pytest

from kairix.core.search.backends import BM25SearchBackend
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.rerank import rerank
from kairix.core.search.rrf import FusedResult
from tests.fakes import FakeDocumentRepository

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Stub cross-encoder (the only sanctioned inline stub — see module docstring).
# ---------------------------------------------------------------------------


class _StubEncoderScores:
    """Array-like wrapper exposing only ``.tolist()`` — the ndarray surface
    that ``rerank()`` depends on."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def tolist(self) -> list[float]:
        return list(self._scores)


class _StubEncoder:
    """Stub cross-encoder. Scores each (query, snippet) pair by counting
    overlapping ASCII tokens. Deterministic and sabotage-survivable: a real
    semantic preference signal that disagrees with BM25/RRF order.

    Tracks the pairs it was called with so tests can verify the encoder
    actually ran (sabotage-proof).
    """

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]]) -> _StubEncoderScores:
        self.calls.append(list(pairs))
        scores: list[float] = []
        for query, doc in pairs:
            q_tokens = set(query.lower().split())
            d_tokens = set(doc.lower().split())
            overlap = len(q_tokens & d_tokens)
            scores.append(float(overlap))
        return _StubEncoderScores(scores)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bm25_doc(
    *,
    path: str,
    title: str,
    snippet: str,
    content: str,
    collection: str = "notes",
    score: float = 1.0,
) -> dict:
    """Build a fake BM25 document satisfying both:

    - ``FakeDocumentRepository``'s internal storage (uses ``path`` key,
      matches on ``content``/``title`` substring).
    - ``rrf()``'s expected ``BM25Result`` shape (uses ``file``/``title``/
      ``snippet``/``score``/``collection`` keys).
    """
    return {
        "path": path,
        "file": path,
        "title": title,
        "snippet": snippet,
        "content": content,
        "collection": collection,
        "score": score,
    }


def _bm25_then_fuse(query: str, docs: list[dict]) -> list[FusedResult]:
    """Run the real production BM25 -> RRF fusion path against fake docs,
    returning the ``FusedResult`` list that ``rerank()`` consumes in
    production (see ``hybrid._apply_reranking``)."""
    backend = BM25SearchBackend(FakeDocumentRepository(documents=docs))
    bm25_hits = backend.search(query, collections=None, limit=20)
    fusion = RRFFusion()
    return list(fusion.fuse(bm25_hits, []))


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_rerank_reorders_pipeline_results_when_encoder_disagrees():
    """BM25 -> RRF -> rerank: a doc with strong query overlap moves to
    top-1 after rerank even when BM25 ranked another doc first.

    This is the operator-visible contract: enabling rerank flips the top
    result for an ambiguous query."""
    # Both docs match the BM25 substring 'guide'. Doc A has higher BM25
    # rank (it appears first in the iteration). The rerank query
    # 'snowflake ingest guide' overlaps strongly with doc B's snippet.
    docs = [
        _bm25_doc(
            path="a.md",
            title="guide overview",
            snippet="guide topics generic intro",
            content="guide topics generic intro",
        ),
        _bm25_doc(
            path="b.md",
            title="Doc B",
            snippet="snowflake ingest guide step by step",
            content="snowflake ingest guide step by step",
        ),
    ]
    fused = _bm25_then_fuse("guide", docs)
    assert len(fused) == 2, "BM25 + RRF should fuse both substring matches"

    encoder = _StubEncoder()
    reranked = rerank(
        query="snowflake ingest guide",
        results=fused,
        encoder=encoder,
    )

    # Sabotage check: the encoder MUST have been called with both pairs.
    assert len(encoder.calls) == 1
    assert len(encoder.calls[0]) == 2

    # Doc B has 3 token overlaps with 'snowflake ingest guide'; doc A has
    # 1 ('guide'). Rerank promotes doc B to top-1.
    assert reranked[0].path == "b.md"
    assert reranked[1].path == "a.md"
    # rerank_score field populated, sorted descending
    assert reranked[0].rerank_score >= reranked[1].rerank_score
    assert reranked[0].rerank_score > 0.0


@pytest.mark.integration
def test_rerank_preserves_order_when_encoder_agrees_with_rrf():
    """When the cross-encoder signal aligns with BM25/RRF order, rerank
    is order-preserving. Sabotage check that the test above isn't
    accidentally always-reordering."""
    # Both docs contain the BM25 query 'banana' (so both fuse). The
    # rerank query 'banana banana' overlaps with both snippets but the
    # first doc has a stronger overlap, matching BM25's order.
    docs = [
        _bm25_doc(
            path="strong.md",
            title="strong banana",
            snippet="banana banana yellow ripe",
            content="banana banana yellow ripe",
        ),
        _bm25_doc(
            path="weak.md",
            title="weak banana",
            snippet="banana once mentioned in passing about apples",
            content="banana once mentioned in passing about apples",
        ),
    ]
    fused = _bm25_then_fuse("banana", docs)
    assert len(fused) == 2
    pre_top = fused[0].path

    encoder = _StubEncoder()
    reranked = rerank(query="banana yellow ripe", results=fused, encoder=encoder)

    # Encoder was called (sabotage-proof: scores recorded).
    assert len(encoder.calls) == 1
    # Order preserved because the encoder agrees with BM25 ranking
    # (strong.md has 3 query-token overlaps, weak.md has 1).
    assert reranked[0].path == pre_top
    assert reranked[0].path == "strong.md"
    # Top result has a higher rerank_score than the second.
    assert reranked[0].rerank_score > reranked[1].rerank_score
    # And the top score is strictly positive (encoder actually ran).
    assert reranked[0].rerank_score > 0.0


@pytest.mark.integration
def test_rerank_returns_unchanged_when_encoder_inference_fails():
    """When the encoder raises during ``predict()`` (e.g. model failed to
    load, OOM, malformed input), ``rerank()`` returns the fused list
    unchanged — never raises. This is the never-raise contract."""
    docs = [
        _bm25_doc(
            path="x.md",
            title="X",
            snippet="alpha beta gamma",
            content="alpha beta gamma",
        ),
        _bm25_doc(
            path="y.md",
            title="Y",
            snippet="alpha delta epsilon",
            content="alpha delta epsilon",
        ),
    ]
    fused = _bm25_then_fuse("alpha", docs)
    assert len(fused) == 2
    pre_paths = [r.path for r in fused]
    pre_boosted = [r.boosted_score for r in fused]

    class _BrokenEncoder:
        def predict(self, pairs: list[tuple[str, str]]) -> _StubEncoderScores:
            raise RuntimeError("model load failed (simulated)")

    out = rerank(query="alpha", results=fused, encoder=_BrokenEncoder())

    # Same paths in same order, identical boosted_score values: rerank
    # did not partially mutate the list before failing.
    assert [r.path for r in out] == pre_paths
    assert [r.boosted_score for r in out] == pre_boosted


@pytest.mark.integration
def test_rerank_overwrites_boosted_score_so_apply_budget_respects_new_order():
    """``rerank()`` overwrites ``boosted_score`` with the rerank score so
    the downstream ``apply_budget`` (which sorts by ``boosted_score``)
    sees the new order. This is the production wiring contract."""
    docs = [
        _bm25_doc(
            path="alpha.md",
            title="A",
            snippet="alpha apple",
            content="alpha apple",
        ),
        _bm25_doc(
            path="beta.md",
            title="B",
            snippet="beta banana cherry",
            content="beta banana cherry",
        ),
    ]
    fused = _bm25_then_fuse("alpha", docs)
    # Sanity: only alpha.md matched the BM25 query for 'alpha'.
    assert len(fused) == 1
    pre_boosted = fused[0].boosted_score

    encoder = _StubEncoder()
    # Re-rank with a query that produces a clearly different score from
    # the BM25 RRF score (~ 0.0163). The stub returns 1.0 for one overlap.
    reranked = rerank(query="alpha", results=fused, encoder=encoder)

    assert len(reranked) == 1
    # boosted_score is overwritten with rerank_score.
    assert reranked[0].boosted_score == reranked[0].rerank_score
    # And it is materially different from the pre-rerank RRF-derived
    # boosted_score — sabotage-proves the overwrite actually happened.
    assert reranked[0].boosted_score != pytest.approx(pre_boosted, abs=1e-9)
    assert reranked[0].rerank_score == pytest.approx(1.0)


@pytest.mark.integration
def test_rerank_empty_results_short_circuits_without_calling_encoder():
    """Edge case: empty fused list — rerank returns immediately without
    consulting the encoder. Verified by an encoder that records calls."""
    encoder = _StubEncoder()
    out = rerank(query="anything", results=[], encoder=encoder)

    assert out == []
    # Sabotage-proof: encoder was never called.
    assert encoder.calls == []
