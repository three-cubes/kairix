"""Integration tests: intent classifier wired into a real ``SearchPipeline``.

These tests do **not** use the integration DB fixture. They wire a real
``SearchPipeline`` with the production ``classify()`` function (via
``RealClassifierAdapter``), real ``RRFFusion``, and the production boost
strategies (``EntityBoost``, ``ProceduralBoost``, ``TemporalDateBoost``)
gated by ``IntentGatedBoost`` so that intent-routing is verifiable end to
end.

The "integration" mark covers the multi-component composition: classifier,
fusion, and boost chain interact through their real implementations against
canonical fakes for the data-bearing protocols (DocumentRepository,
VectorRepository, GraphRepository).

No monkeypatch, no @patch, no inline stubs. All test doubles live in
``tests/fakes.py``.
"""

from __future__ import annotations

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import EntityBoost, ProceduralBoost, TemporalDateBoost
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from tests.fakes import (
    CapturingBoost,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
    IntentGatedBoost,
    RealClassifierAdapter,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture data — documents shaped for both BM25 (FTS-style, "file" key) and
# vector ("path" key) result handlers in kairix.core.search.rrf.
# ---------------------------------------------------------------------------


def _bm25_doc(
    path: str,
    title: str,
    content: str,
    collection: str = "notes",
    snippet: str | None = None,
) -> dict:
    """Build a doc that satisfies both FakeDocumentRepository (uses ``path``
    for keying / content match) and rrf.rrf() (reads ``file``, ``title``,
    ``snippet``, ``collection`` from BM25 results)."""
    return {
        "path": path,
        "file": path,
        "title": title,
        "content": content,
        "snippet": snippet or content[:80],
        "collection": collection,
        "score": 1.0,
    }


def _vec_result(
    path: str,
    title: str,
    snippet: str = "vector hit",
    collection: str = "notes",
) -> dict:
    """Build a vector-search result shape consumed by rrf.rrf()."""
    return {
        "path": path,
        "title": title,
        "snippet": snippet,
        "collection": collection,
        "distance": 0.1,
    }


def _build_pipeline(
    *,
    docs: list[dict],
    vec_results: list[dict],
    boosts: list,
    graph: FakeGraphRepository | None = None,
    config: RetrievalConfig | None = None,
) -> tuple[SearchPipeline, RealClassifierAdapter, FakeGraphRepository]:
    """Construct a SearchPipeline with the real classifier and real fusion.

    Returns the pipeline, the classifier adapter (so tests can assert it was
    invoked), and the graph fake (so tests can inspect call tracking).
    """
    classifier = RealClassifierAdapter()
    graph_fake = graph if graph is not None else FakeGraphRepository(available=True)
    pipeline = SearchPipeline(
        classifier=classifier,
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository(results=vec_results)),
        graph=graph_fake,
        fusion=RRFFusion(k=60),
        boosts=boosts,
        logger=FakeSearchLogger(),
        config=config or RetrievalConfig.minimal(),
    )
    return pipeline, classifier, graph_fake


def _result_paths(pipeline_result) -> list[str]:
    """Extract the path of each result in pipeline order.

    SearchPipeline returns BudgetedResult instances (which wrap FusedResult)
    after ``apply_budget``. Reach through ``.result`` if present.
    """
    paths: list[str] = []
    for r in pipeline_result.results:
        inner = getattr(r, "result", r)
        paths.append(getattr(inner, "path", ""))
    return paths


# ---------------------------------------------------------------------------
# ENTITY intent → graph backend reached
# ---------------------------------------------------------------------------


def test_entity_intent_query_reaches_graph_backend():
    """``classify('tell me about Jordan Blake')`` returns ENTITY, and the
    SearchPipeline consults the GraphRepository (``available`` is checked,
    and the entity boost runs a cypher query)."""
    docs = [_bm25_doc("person/jordan-blake.md", "Jordan Blake", "person/jordan-blake.md profile")]
    vec_results = [_vec_result("person/jordan-blake.md", "Jordan Blake")]
    cypher_rows = [
        {
            "vault_path": "person/jordan-blake.md",
            "name": "Jordan Blake",
            "labels": ["Person"],
            "in_degree": 5,
        }
    ]
    graph = FakeGraphRepository(available=True, cypher_rows=cypher_rows)
    entity_boost = EntityBoost(graph=graph, config=EntityBoostConfig(enabled=True, factor=0.20, cap=2.0))
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[IntentGatedBoost(entity_boost, QueryIntent.ENTITY)],
        graph=graph,
    )

    out = pipeline.search("tell me about Jordan Blake")

    # Pipeline classified the query as ENTITY via the real classifier.
    assert classifier.calls == ["tell me about Jordan Blake"]
    assert out.intent == QueryIntent.ENTITY
    # Pipeline consulted graph.available (line 100 of pipeline.py: ENTITY guard).
    assert graph.available_checks >= 1
    # The intent-gated entity boost reached cypher() — i.e. the graph backend
    # was actually queried for entity in-degree data.
    assert len(graph.cypher_calls) == 1


def test_non_entity_intent_does_not_route_to_entity_boost():
    """A SEMANTIC query passes the same pipeline configuration but the
    intent-gated entity boost does NOT call cypher (sabotage check for the
    ENTITY routing test)."""
    docs = [_bm25_doc("docs/architecture.md", "Architecture", "system architecture overview")]
    vec_results = [_vec_result("docs/architecture.md", "Architecture")]
    graph = FakeGraphRepository(available=True, cypher_rows=[])
    entity_boost = EntityBoost(graph=graph, config=EntityBoostConfig(enabled=True))
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[IntentGatedBoost(entity_boost, QueryIntent.ENTITY)],
        graph=graph,
    )

    out = pipeline.search("explain the architecture of the kairix memory system")

    assert classifier.calls == ["explain the architecture of the kairix memory system"]
    assert out.intent == QueryIntent.SEMANTIC
    # No entity boost dispatch → no cypher call.
    assert graph.cypher_calls == []


# ---------------------------------------------------------------------------
# TEMPORAL intent → date-path boost reorders FusedResults
# ---------------------------------------------------------------------------


def test_temporal_intent_query_reorders_results_via_date_path_boost():
    """For an ISO-dated TEMPORAL query, the temporal date boost moves the
    document whose path contains the matching date string above another
    doc that ranked higher under plain RRF."""
    # Both docs match the query "completed" (substring). BM25 + vec both
    # rank general.md first → plain RRF puts general.md above the dated doc.
    # The temporal boost should flip that order for the TEMPORAL intent.
    docs = [
        _bm25_doc("notes/general.md", "General", "completed work overview"),
        _bm25_doc("notes/2026-03-22-release.md", "Release log", "completed milestone summary"),
    ]
    vec_results = [
        _vec_result("notes/general.md", "General"),
        _vec_result("notes/2026-03-22-release.md", "Release log"),
    ]
    temporal_boost = TemporalDateBoost(
        config=TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=2.5)
    )
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[IntentGatedBoost(temporal_boost, QueryIntent.TEMPORAL)],
    )

    out = pipeline.search("what was completed on 2026-03-22")
    paths = _result_paths(out)

    assert classifier.calls == ["what was completed on 2026-03-22"]
    assert out.intent == QueryIntent.TEMPORAL
    # The dated path is reordered above the non-dated one.
    assert paths.index("notes/2026-03-22-release.md") < paths.index("notes/general.md")


def test_temporal_intent_baseline_without_boost_keeps_rrf_order():
    """Sabotage check: same data, no temporal boost → dated path stays
    in plain RRF position (general.md first, since both inputs rank it first)."""
    docs = [
        _bm25_doc("notes/general.md", "General", "completed work overview"),
        _bm25_doc("notes/2026-03-22-release.md", "Release log", "completed milestone summary"),
    ]
    vec_results = [
        _vec_result("notes/general.md", "General"),
        _vec_result("notes/2026-03-22-release.md", "Release log"),
    ]
    pipeline, _classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[],
    )

    out = pipeline.search("what was completed on 2026-03-22")
    paths = _result_paths(out)

    assert out.intent == QueryIntent.TEMPORAL
    assert paths.index("notes/general.md") < paths.index("notes/2026-03-22-release.md")


# ---------------------------------------------------------------------------
# PROCEDURAL intent → procedural boost reorders matching paths
# ---------------------------------------------------------------------------


def test_procedural_intent_query_reorders_runbook_path():
    """For a PROCEDURAL query, the procedural boost lifts a runbook-pattern
    path above a non-procedural path."""
    docs = [
        _bm25_doc("notes/random.md", "Random", "random notes about deploys"),
        _bm25_doc("ops/runbooks/deploy.md", "Deploy runbook", "ops/runbooks/deploy.md steps"),
    ]
    vec_results = [
        _vec_result("notes/random.md", "Random"),
        _vec_result("ops/runbooks/deploy.md", "Deploy runbook"),
    ]
    procedural_boost = ProceduralBoost(config=ProceduralBoostConfig(enabled=True, factor=2.0))
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[IntentGatedBoost(procedural_boost, QueryIntent.PROCEDURAL)],
    )

    out = pipeline.search("how do I deploy the service")
    paths = _result_paths(out)

    assert classifier.calls == ["how do I deploy the service"]
    assert out.intent == QueryIntent.PROCEDURAL
    assert paths.index("ops/runbooks/deploy.md") < paths.index("notes/random.md")


def test_procedural_boost_skipped_for_non_procedural_intent():
    """Sabotage check: an IntentGatedBoost wrapping ProceduralBoost does
    NOT fire for SEMANTIC intent. The runbook path stays in RRF order."""
    docs = [
        _bm25_doc("notes/random.md", "Random", "random tradeoffs notes"),
        _bm25_doc("ops/runbooks/deploy.md", "Deploy runbook", "ops/runbooks/deploy.md tradeoffs"),
    ]
    vec_results = [
        _vec_result("notes/random.md", "Random"),
        _vec_result("ops/runbooks/deploy.md", "Deploy runbook"),
    ]
    procedural_boost = ProceduralBoost(config=ProceduralBoostConfig(enabled=True, factor=10.0))
    gated = IntentGatedBoost(procedural_boost, QueryIntent.PROCEDURAL)
    pipeline, _, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[gated],
    )

    out = pipeline.search("explain the architecture of the kairix memory system")
    paths = _result_paths(out)

    assert out.intent == QueryIntent.SEMANTIC
    # Gated boost was not invoked (intent didn't match).
    assert gated.invocations == 0
    assert gated.skipped == 1
    # RRF order preserved — both lists ranked notes/random.md first.
    assert paths.index("notes/random.md") < paths.index("ops/runbooks/deploy.md")


# ---------------------------------------------------------------------------
# SEMANTIC intent → no intent-specific boost fires
# ---------------------------------------------------------------------------


def test_semantic_intent_no_intent_boost_fires_results_in_rrf_order():
    """For a SEMANTIC query, with all three intent-gated boosts wired,
    none of them fires (skipped counters tick) and the result order is the
    plain RRF order."""
    docs = [
        _bm25_doc("docs/a.md", "A", "alpha content semantic"),
        _bm25_doc("docs/b.md", "B", "beta content semantic"),
        _bm25_doc("ops/runbooks/c.md", "C", "ops/runbooks/c.md content semantic"),
    ]
    vec_results = [
        _vec_result("docs/a.md", "A"),
        _vec_result("docs/b.md", "B"),
        _vec_result("ops/runbooks/c.md", "C"),
    ]
    graph = FakeGraphRepository(available=True, cypher_rows=[])
    entity_g = IntentGatedBoost(EntityBoost(graph=graph, config=EntityBoostConfig(enabled=True)), QueryIntent.ENTITY)
    procedural_g = IntentGatedBoost(
        ProceduralBoost(config=ProceduralBoostConfig(enabled=True, factor=10.0)), QueryIntent.PROCEDURAL
    )
    temporal_g = IntentGatedBoost(
        TemporalDateBoost(config=TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=10.0)),
        QueryIntent.TEMPORAL,
    )
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[entity_g, procedural_g, temporal_g],
        graph=graph,
    )

    out = pipeline.search("explain the architecture of the kairix memory system")
    paths = _result_paths(out)

    assert classifier.calls == ["explain the architecture of the kairix memory system"]
    assert out.intent == QueryIntent.SEMANTIC
    # No intent-gated boost dispatched.
    assert entity_g.invocations == 0
    assert procedural_g.invocations == 0
    assert temporal_g.invocations == 0
    # All three were offered the call (skipped counters confirm dispatch loop ran).
    assert entity_g.skipped == 1
    assert procedural_g.skipped == 1
    assert temporal_g.skipped == 1
    # Pure RRF order: BM25 + vec both rank a, b, c → fused order is a, b, c.
    assert paths == ["docs/a.md", "docs/b.md", "ops/runbooks/c.md"]
    # Graph cypher() was not called because the entity-gated boost was skipped.
    assert graph.cypher_calls == []


def test_semantic_intent_classifier_is_called_exactly_once():
    """Sabotage check: the SearchPipeline calls classifier.classify exactly
    once per search() — not zero, not twice. Guards against regressions
    where intent is recomputed inside boosts or skipped entirely."""
    docs = [_bm25_doc("docs/a.md", "A", "alpha content semantic")]
    vec_results = [_vec_result("docs/a.md", "A")]
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[],
    )

    out = pipeline.search("why does hybrid search outperform pure vector")

    assert out.intent == QueryIntent.SEMANTIC
    assert len(classifier.calls) == 1
    assert classifier.calls[0] == "why does hybrid search outperform pure vector"


# ---------------------------------------------------------------------------
# Cross-cutting: classifier output is propagated into the boost context
# ---------------------------------------------------------------------------


def test_classified_intent_is_propagated_to_boost_context():
    """The intent value supplied to each BoostStrategy.boost() context is
    exactly the value returned by the real classifier — i.e. the intent
    that drives gating IS the classifier's output, not a recomputation."""
    capturer = CapturingBoost()

    docs = [_bm25_doc("docs/a.md", "A", "deploy how to alpha")]
    vec_results = [_vec_result("docs/a.md", "A")]
    pipeline, classifier, _ = _build_pipeline(
        docs=docs,
        vec_results=vec_results,
        boosts=[capturer],
    )

    pipeline.search("how do I deploy the service")  # PROCEDURAL
    pipeline.search("when did we ship v1.1.2")  # TEMPORAL ("when did")
    pipeline.search("tell me about Jordan Blake")  # ENTITY

    intents = [intent for _q, intent in capturer.captured]
    assert intents == [QueryIntent.PROCEDURAL, QueryIntent.TEMPORAL, QueryIntent.ENTITY]
    # Real classifier was the source of every captured intent.
    assert classifier.calls == [
        "how do I deploy the service",
        "when did we ship v1.1.2",
        "tell me about Jordan Blake",
    ]
