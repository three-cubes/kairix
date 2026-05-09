"""Step definitions for search_rerank.feature.

Verifies the operator-visible contract: enabling the cross-encoder re-ranker
promotes the semantically relevant document to top-1 for ambiguous queries
where BM25/RRF prefers a less-relevant document.

Hard discipline rules:
  - No @patch / monkeypatch / module singleton mutation.
  - No private-fn imports — drives only the public ``rerank()`` surface.
  - The inline ``_StubEncoder`` is the project's sanctioned exception
    because there is no kairix Protocol for ``CrossEncoder``.
"""

from __future__ import annotations

from pytest_bdd import given, then, when

from kairix.core.search.backends import BM25SearchBackend
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.rerank import rerank
from tests.fakes import FakeDocumentRepository

_state: dict = {}


# ---------------------------------------------------------------------------
# Stub cross-encoder — sanctioned inline stub (no kairix Protocol exists for
# the third-party CrossEncoder type).
# ---------------------------------------------------------------------------


class _StubEncoderScores:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def tolist(self) -> list[float]:
        return list(self._scores)


class _StubEncoder:
    """Score (query, doc) by overlapping ASCII tokens. Deterministic."""

    def predict(self, pairs: list[tuple[str, str]]) -> _StubEncoderScores:
        scores: list[float] = []
        for query, doc in pairs:
            q = set(query.lower().split())
            d = set(doc.lower().split())
            scores.append(float(len(q & d)))
        return _StubEncoderScores(scores)


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------


@given("I have two documents that both match a BM25 query but differ in semantic relevance")
def two_docs_with_differing_relevance() -> None:
    _state.clear()
    _state["bm25_query"] = "guide"
    # Doc A: mentions 'guide' but unrelated topic (BM25 prefers it).
    # Doc B: mentions 'guide' alongside the more specific terms operators
    #        will search for ('snowflake ingest guide').
    _state["docs"] = [
        {
            "path": "a.md",
            "file": "a.md",
            "title": "guide overview",
            "snippet": "guide topics generic intro",
            "content": "guide topics generic intro",
            "collection": "notes",
            "score": 1.0,
        },
        {
            "path": "b.md",
            "file": "b.md",
            "title": "Doc B",
            "snippet": "snowflake ingest guide step by step",
            "content": "snowflake ingest guide step by step",
            "collection": "notes",
            "score": 1.0,
        },
    ]


@when("I run BM25-then-RRF fusion without re-ranking")
def run_bm25_rrf() -> None:
    backend = BM25SearchBackend(FakeDocumentRepository(documents=_state["docs"]))
    bm25_hits = backend.search(_state["bm25_query"], collections=None, limit=20)
    fusion = RRFFusion()
    _state["fused"] = list(fusion.fuse(bm25_hits, []))


@then("the BM25-preferred document is at top-1")
def bm25_top_is_a() -> None:
    fused = _state["fused"]
    assert len(fused) == 2, f"expected 2 fused results, got {len(fused)}"
    # BM25 iteration order in the fake repo is insertion order, so a.md
    # ranks first. Capture this for the rerank assertion below.
    assert fused[0].path == "a.md", f"expected a.md at top-1 from BM25/RRF, got {fused[0].path}"
    _state["pre_rerank_top"] = fused[0].path


@when("I apply the cross-encoder re-ranker with a more specific query")
def apply_rerank() -> None:
    encoder = _StubEncoder()
    _state["reranked"] = rerank(
        query="snowflake ingest guide",
        results=_state["fused"],
        encoder=encoder,
    )


@then("the semantically relevant document is now at top-1")
def reranked_top_is_b() -> None:
    reranked = _state["reranked"]
    pre_top = _state["pre_rerank_top"]
    assert reranked[0].path == "b.md", (
        f"expected b.md at top-1 after rerank, got {reranked[0].path}; pre-rerank top was {pre_top}"
    )
    # Sabotage-proof: top-1 must have actually changed (it was a.md before).
    assert reranked[0].path != pre_top, f"top-1 did not change after rerank — was {pre_top}, still {reranked[0].path}"
    # And the rerank_score field is populated and ordered.
    assert reranked[0].rerank_score >= reranked[1].rerank_score
    assert reranked[0].rerank_score > 0.0
