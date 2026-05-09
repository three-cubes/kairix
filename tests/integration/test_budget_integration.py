"""
Integration tests for kairix.core.search.budget.apply_budget().

These tests wire ``apply_budget`` after a real ``SearchPipeline`` produces
``FusedResult``s, then assert the budget step trims to the expected token
count. The pipeline is built from ``tests.fakes`` (no @patch, no monkeypatch
on kairix code) and the Phase-2 summaries DB is built from the real on-disk
sqlite schema via ``tests.fakes.build_summaries_db`` and pointed at by the
``KAIRIX_SUMMARIES_DB`` env var (a configuration knob, not a code patch).

Coverage:

  - end-to-end pipeline produces FusedResults that apply_budget can consume;
  - summary-fallback path (Phase 2) is exercised when the summaries DB
    contains L0/L1 rows for the relevant paths;
  - the budget cap holds across the integrated pipeline output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.budget import (
    DEFAULT_BUDGET,
    apply_budget,
)
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
    build_summaries_db,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared pipeline factory
# ---------------------------------------------------------------------------


def _build_pipeline(
    docs: list[dict],
    vec_results: list[dict],
) -> SearchPipeline:
    """Construct a SearchPipeline from fakes plus the real RRF fusion.

    Using the real RRFFusion (not FakeFusion) is what makes this an
    integration test for the budget step: the upstream stage emits actual
    FusedResult objects with proper paths/snippets/scores, so apply_budget
    sees production-shaped input.
    """
    return SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=vec_results),
        ),
        graph=FakeGraphRepository(available=True),
        fusion=RRFFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )


# ---------------------------------------------------------------------------
# Integration: pipeline → apply_budget end-to-end
# ---------------------------------------------------------------------------


def test_integration_pipeline_to_budget_phase1() -> None:
    """A real pipeline produces FusedResults; apply_budget trims them.

    Phase 1 (no summaries DB on disk): every kept BudgetedResult must be L2.
    """
    # Build documents whose content matches the BM25 query.
    docs = [
        {
            # NOTE: BM25Result TypedDict uses ``file`` (not ``path``) as the
            # path key — RRF reads result["file"]. FakeDocumentRepository
            # returns docs verbatim from search_fts().
            "file": f"areas/topic-{i}.md",
            "path": f"areas/topic-{i}.md",
            "title": f"Topic {i}",
            "content": "architecture decision " + ("filler word " * 20),
            "snippet": "architecture decision body " + ("filler word " * 20),
            "score": 1.0 - i * 0.1,
            "collection": "vault-areas",
        }
        for i in range(5)
    ]
    vec_results = [
        {
            "path": f"areas/topic-{i}.md",
            "title": f"Topic {i}",
            "snippet": "architecture overview " + ("body " * 20),
            "distance": 0.1 + i * 0.05,
            "collection": "vault-areas",
        }
        for i in range(5)
    ]

    pipeline = _build_pipeline(docs, vec_results)

    # Drive the pipeline; capture fused results before apply_budget would have
    # already been invoked inside .search().
    result = pipeline.search("architecture", budget=10_000)

    # Pipeline ran successfully and produced budgeted output.
    assert result.fused_count >= 1
    assert len(result.results) >= 1

    # Phase 1 invariant: every kept result is L2.
    tiers = {r.tier for r in result.results}
    assert tiers == {"L2"}, f"expected L2-only in Phase 1, got {tiers}"

    # Budget cap respected.
    total = sum(r.token_estimate for r in result.results)
    assert total <= 10_000


def test_integration_apply_budget_after_pipeline_fused() -> None:
    """Run apply_budget directly on FusedResults emitted by the real
    fusion stage, asserting trimming to a tight budget.

    This exercises the integration boundary explicitly: pipeline → fused
    list → apply_budget.
    """
    docs = [
        {
            "file": f"areas/d{i}.md",
            "path": f"areas/d{i}.md",
            "title": f"D{i}",
            "content": "architecture overview " + ("alpha beta gamma delta " * 30),
            "snippet": "architecture overview " + ("alpha beta gamma delta " * 30),
            "score": 1.0 - i * 0.1,
            "collection": "vault-areas",
        }
        for i in range(4)
    ]
    vec_results = [
        {
            "path": f"areas/d{i}.md",
            "title": f"D{i}",
            "snippet": "architecture overview " + ("alpha beta gamma delta " * 30),
            "distance": 0.1,
            "collection": "vault-areas",
        }
        for i in range(4)
    ]

    pipeline = _build_pipeline(docs, vec_results)

    # Run with an absurdly large budget so SearchPipeline.search keeps every
    # fused result; we then re-apply apply_budget at a tight budget.
    big = pipeline.search("architecture", budget=10_000_000)
    fused_inputs = [br.result for br in big.results]
    assert len(fused_inputs) >= 2, "fused stage produced too few results"

    # Tight budget: must trim.
    tight_budget = 100
    out = apply_budget(fused_inputs, budget=tight_budget)
    total = sum(r.token_estimate for r in out)

    # Cap held (allowing the final-truncation soft-cap drift documented in
    # test_budget_contracts.py — bounded by 2x).
    assert total < tight_budget * 2, f"cap drifted >2x at integration: total={total}"
    # Non-empty.
    assert len(out) >= 1
    # Trimming evidence: at least one returned content is shorter than the
    # corresponding input snippet (proves truncation fired).
    by_path = {br.result.path: br for br in out}
    truncated_any = any(len(by_path[fr.path].content) < len(fr.snippet) for fr in fused_inputs if fr.path in by_path)
    assert truncated_any, "expected at least one result to be truncated under tight budget"


# ---------------------------------------------------------------------------
# Integration: Phase-2 summary-fallback path
# ---------------------------------------------------------------------------


def test_integration_budget_falls_back_to_snippet_when_summary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 2: when summaries DB exists but a path has no row, fall back
    to the snippet rather than emitting empty content.

    This is the partial-DB case: we have summaries for some paths, not
    others. Budget step must remain useful.
    """
    db_path = tmp_path / "summaries.db"
    build_summaries_db(
        db_path,
        rows={
            "areas/has-summary.md": {"l0": "abstract", "l1": None},
            # NOTE: 'areas/no-summary.md' has no row.
        },
    )
    monkeypatch.setenv("KAIRIX_SUMMARIES_DB", str(db_path))

    docs = [
        {
            "file": "areas/no-summary.md",
            "path": "areas/no-summary.md",
            "title": "NoSummary",
            "content": "real content " + ("filler " * 30),
            "snippet": "snippet body for no-summary doc",
            "score": 0.9,
            "collection": "vault-areas",
        },
    ]
    vec_results = [
        {
            "path": "areas/no-summary.md",
            "title": "NoSummary",
            "snippet": "snippet body for no-summary doc",
            "distance": 0.1,
            "collection": "vault-areas",
        },
    ]

    pipeline = _build_pipeline(docs, vec_results)
    big = pipeline.search("content", budget=10_000_000)
    fused_inputs = [br.result for br in big.results]
    assert len(fused_inputs) == 1

    out = apply_budget(fused_inputs, budget=DEFAULT_BUDGET)
    assert len(out) == 1
    # Content must NOT be empty — the snippet fallback fires.
    assert out[0].content, "summary missing → expected snippet fallback, got empty"
    # And it's the snippet (not an L0/L1 string we never set).
    assert "snippet body" in out[0].content


# ---------------------------------------------------------------------------
# Integration: cap holds across the full pipeline
# ---------------------------------------------------------------------------


def test_integration_pipeline_total_tokens_under_budget() -> None:
    """SearchResult.total_tokens (computed by SearchPipeline from
    apply_budget output) must respect the requested budget.

    This is the user-visible budget contract.
    """
    docs = [
        {
            "file": f"areas/d{i}.md",
            "path": f"areas/d{i}.md",
            "title": f"D{i}",
            "content": "english prose " + ("alpha bravo charlie " * 50),
            "snippet": "english prose " + ("alpha bravo charlie " * 50),
            "score": 1.0 - i * 0.05,
            "collection": "vault-areas",
        }
        for i in range(8)
    ]
    vec_results = [
        {
            "path": f"areas/d{i}.md",
            "title": f"D{i}",
            "snippet": "english prose " + ("alpha bravo charlie " * 50),
            "distance": 0.1 + i * 0.02,
            "collection": "vault-areas",
        }
        for i in range(8)
    ]

    pipeline = _build_pipeline(docs, vec_results)
    requested_budget = 500
    result = pipeline.search("prose", budget=requested_budget)

    # Cap respected — soft-cap drift bounded by 2x (see contract test).
    assert result.total_tokens < requested_budget * 2
    # And we did keep at least one result.
    assert len(result.results) >= 1
