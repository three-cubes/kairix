"""Integration: per-query stage telemetry surfaces through the probe envelope.

Wires a real SearchPipeline (with canonical fakes at the I/O boundary —
FakeEmbeddingService, FakeVectorRepository, FakeDocumentRepository) through
the real probe runner, and asserts the documented shape:

  - Each ``per_query_stages`` record has case_id, category, latency_ms,
    stage_latency_ms.
  - Each ``stage_latency_ms`` includes both ``embed_http`` and ``vector_ann``
    (in addition to existing ``bm25``, ``dispatch``, ``vector``, etc.).
  - ``embed_http + vector_ann`` ≈ ``vector`` within a few ms of measurement
    noise.

The boundary chain is end-to-end real (pipeline + executor + runner +
backends) — only the I/O leaves (Azure / SQLite / Neo4j) are faked from
``tests/fakes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.pipeline import SearchPipeline
from kairix.quality.probe.runner import SampledQuery, run_probe_search
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Case:
    """Minimal BenchmarkCase stand-in (sampler reads .category, .query, .id)."""

    id: str
    category: str
    query: str
    agent: str | None = None


def _suite_loader(_suite: str) -> list[_Case]:
    """Cases across every default-weighted category so sampling fills the map."""
    out: list[_Case] = []
    for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"):
        for i in range(10):
            out.append(_Case(id=f"{cat}-{i}", category=cat, query=f"alpha bravo {cat} {i}"))
    return out


def _build_pipeline() -> SearchPipeline:
    """Real SearchPipeline with canonical fakes at every I/O boundary."""
    doc_repo = FakeDocumentRepository(
        documents=[
            {"path": "p.md", "title": "T", "content": "alpha bravo charlie", "collection": "c"},
            {"path": "q.md", "title": "U", "content": "alpha delta echo", "collection": "c"},
        ]
    )
    vec_repo = FakeVectorRepository(results=[{"path": "v.md", "distance": 0.1, "collection": "c"}])
    return SearchPipeline(
        classifier=FakeClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(FakeEmbeddingService(), vec_repo),
        graph=FakeGraphRepository(available=True),
        fusion=FakeFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )


def _make_searcher(pipeline: SearchPipeline):
    """Adapt SearchPipeline.search to the probe's ``Callable[[SampledQuery], Any]``."""

    def _search(q: SampledQuery):
        return pipeline.search(query=q.query, agent=q.agent)

    return _search


@pytest.mark.integration
def test_per_query_records_carry_documented_keys() -> None:
    """End-to-end: every per_query_stages record has the documented shape.

    The pipeline emits ``stage_latency_ms`` with ``classify`` / ``resolve``
    / ``dispatch`` / ``fuse`` / ``enrich`` / ``boost`` / ``budget`` /
    ``bm25`` / ``vector`` / ``embed_http`` / ``vector_ann`` keys (#282
    follow-up). The probe runner projects each into a per-query record;
    operators consume the projection via jq.

    Sabotage-proof: drop the ``per_query_stages=per_query_stages``
    forwarding into the ProbeResult constructor in run_probe_search and
    the list stays empty — this assertion fires.
    """
    pipeline = _build_pipeline()
    result = run_probe_search(
        suite="x",
        queries=8,
        concurrency=2,
        suite_loader=_suite_loader,
        searcher=_make_searcher(pipeline),
    )
    assert len(result.per_query_stages) == 8
    for record in result.per_query_stages:
        assert {"case_id", "category", "latency_ms", "stage_latency_ms"} <= set(record.keys())
        assert record["case_id"]
        assert record["category"]
        assert record["latency_ms"] >= 0.0
        assert isinstance(record["stage_latency_ms"], dict)


@pytest.mark.integration
def test_per_query_stage_latency_includes_embed_http_and_vector_ann() -> None:
    """Every per-query stage map has both embed_http and vector_ann.

    The split is what tells operators apart Azure HTTP tail latency from
    local ANN cost — the question that the single ``vector`` number can
    not answer.

    Sabotage-proof: remove the ``timings=stages`` plumbing from
    ``_dispatch_vector`` and the new keys never appear in
    stage_latency_ms; the existing ``vector`` total stays so coarser
    readers don't break.
    """
    pipeline = _build_pipeline()
    result = run_probe_search(
        suite="x",
        queries=5,
        concurrency=1,
        suite_loader=_suite_loader,
        searcher=_make_searcher(pipeline),
    )
    for record in result.per_query_stages:
        stage_map = record["stage_latency_ms"]
        assert "embed_http" in stage_map, f"missing embed_http in {stage_map!r}"
        assert "vector_ann" in stage_map, f"missing vector_ann in {stage_map!r}"
        # The parent ``vector`` total stays so coarser readers don't break.
        assert "vector" in stage_map


@pytest.mark.integration
def test_per_query_embed_http_plus_vector_ann_sums_to_vector() -> None:
    """embed_http + vector_ann ≈ vector within a few ms of measurement noise.

    The brief specifies "within a few ms of measurement noise" — the inner
    timers wrap the embed call and the ANN call directly, the outer
    ``vector`` timer wraps the dispatch-vector helper. Difference is the
    helper's own bookkeeping overhead (round, dict write, try/except).

    Sabotage-proof: misorder the ``time.monotonic()`` calls so the inner
    timers don't bracket their respective calls (e.g. start the
    ``vector_ann`` timer before the ``embed_http`` timer was stopped) and
    the sum dramatically exceeds the parent total.
    """
    pipeline = _build_pipeline()
    result = run_probe_search(
        suite="x",
        queries=6,
        concurrency=1,
        suite_loader=_suite_loader,
        searcher=_make_searcher(pipeline),
    )
    for record in result.per_query_stages:
        stage_map = record["stage_latency_ms"]
        split_sum = stage_map["embed_http"] + stage_map["vector_ann"]
        vector_total = stage_map["vector"]
        assert abs(split_sum - vector_total) <= 2.0, (
            f"embed_http({stage_map['embed_http']}) + vector_ann({stage_map['vector_ann']}) "
            f"= {split_sum}; expected ~{vector_total} (vector total)"
        )


@pytest.mark.integration
def test_envelope_per_query_stages_is_json_shaped_list_of_records() -> None:
    """to_envelope projects per_query_stages as plain dicts ready for jq.

    Sabotage-proof: project per_query_stages as a list of objects with
    non-serialisable fields (e.g. forget to call ``dict(stage_map)`` and
    leave a defaultdict or a Mapping subclass) and downstream JSON
    serialisation in the CLI breaks.
    """
    pipeline = _build_pipeline()
    result = run_probe_search(
        suite="x",
        queries=4,
        concurrency=2,
        suite_loader=_suite_loader,
        searcher=_make_searcher(pipeline),
    )
    env = result.to_envelope()
    assert "per_query_stages" in env
    assert isinstance(env["per_query_stages"], list)
    for record in env["per_query_stages"]:
        assert isinstance(record, dict)
        assert isinstance(record["stage_latency_ms"], dict)
        # Every stage value must be a plain float for JSON serialisation.
        for stage_name, ms in record["stage_latency_ms"].items():
            assert isinstance(ms, (int, float)), (
                f"stage {stage_name!r} value {ms!r} (type {type(ms).__name__}) is not JSON-serialisable"
            )


@pytest.mark.integration
def test_slow_query_surfaces_in_per_query_records() -> None:
    """A slow query stands out in per_query_stages — the operator can rank it.

    A latency-injecting fake searcher returns one slow + many fast queries.
    The slow one's ``latency_ms`` is visibly higher than the others, so an
    operator running ``jq '.per_query_stages | sort_by(-.latency_ms)'`` lands
    on the slow query first.

    Sabotage-proof: average the durations into latency_ms instead of
    recording per-task duration_ms and the slow query disappears into
    the mean — exactly the tail-hiding pathology this whole change is
    designed to fix.
    """
    import time

    slow_marker_query = "SLOW_QUERY"

    def latency_injecting_searcher(q: SampledQuery) -> dict[str, str]:
        if slow_marker_query in q.query:
            time.sleep(0.08)  # 80ms slow query
        return {"results": "ok"}

    # Build a custom suite where one case carries the slow marker.
    def _custom_loader(_suite: str) -> list[_Case]:
        out: list[_Case] = []
        for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"):
            for i in range(5):
                out.append(_Case(id=f"{cat}-{i}", category=cat, query=f"fast q for {cat} {i}"))
        out.append(_Case(id="slow-1", category="recall", query=slow_marker_query))
        return out

    result = run_probe_search(
        suite="x",
        queries=30,
        concurrency=4,
        suite_loader=_custom_loader,
        searcher=latency_injecting_searcher,
    )
    # At least the slow query made it into the sample (high probability with
    # 30 queries pulled from ~31 cases; the test asserts conditionally on it
    # to stay deterministic — falling back to a tolerance assertion when the
    # weighted sampler doesn't pick the slow query in this run).
    slow_records = [r for r in result.per_query_stages if r["case_id"] == "slow-1"]
    if not slow_records:
        # Sampler didn't pick the slow case this run. The slow query is one
        # of ~31 cases and 30 are sampled, so it's possible. We still
        # assert per_query_stages is populated and the latency_ms field is
        # per-query (not all equal) — that's the signal a sort-by-latency
        # would key off.
        latencies = [r["latency_ms"] for r in result.per_query_stages]
        assert len(set(latencies)) > 1, "expected per-query latency variance even without slow case"
        return
    slow_record = slow_records[0]
    # The slow query's latency_ms must dominate; otherwise the mean would
    # hide it (the exact bug this whole change is designed to fix).
    fast_max = max(r["latency_ms"] for r in result.per_query_stages if r["case_id"] != "slow-1")
    assert slow_record["latency_ms"] > fast_max, (
        f"slow query latency ({slow_record['latency_ms']}ms) did not exceed "
        f"the fastest non-slow latency ({fast_max}ms) — tail signal lost"
    )
