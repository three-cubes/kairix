"""Step definitions for probe_per_query_telemetry.feature.

Closes the diagnostic-instrument gap on the conc=10 latency tail (#282
follow-up). The BDD layer pins the operator-visible envelope shape:

  - ``per_query_stages`` is a list of records, one per query.
  - Each record exposes case_id, category, latency_ms, stage_latency_ms.
  - ``stage_latency_ms`` decomposes the ``vector`` stage into
    ``embed_http`` (Azure embed HTTP call) + ``vector_ann`` (local
    usearch ANN cost), so an operator can attribute tail latency to the
    right root cause.

Pattern: canonical fakes from ``tests/fakes.py`` for I/O boundaries;
the SearchPipeline and probe runner are real.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.pipeline import SearchPipeline
from kairix.quality.probe.runner import ProbeResult, SampledQuery, run_probe_search
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.bdd


_SLOW_MARKER = "SLOW_QUERY_MARKER"


@dataclass(frozen=True)
class _Case:
    """Minimal BenchmarkCase stand-in (sampler reads .category, .query, .id)."""

    id: str
    category: str
    query: str
    agent: str | None = None


def _default_cases() -> list[_Case]:
    """Cases across every default-weighted category for weighted sampling."""
    out: list[_Case] = []
    for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"):
        for i in range(5):
            out.append(_Case(id=f"{cat}-{i}", category=cat, query=f"alpha bravo {cat} {i}"))
    return out


def _default_loader(_suite: str) -> list[_Case]:
    return _default_cases()


def _cases_with_one_slow() -> list[_Case]:
    """Default suite plus exactly one case carrying the slow-query marker."""
    out = _default_cases()
    out.append(_Case(id="slow-1", category="recall", query=_SLOW_MARKER))
    return out


def _slow_loader(_suite: str) -> list[_Case]:
    return _cases_with_one_slow()


@dataclass
class _StagedFakeResult:
    """Minimal stand-in for SearchResult — carries stage_latency_ms.

    The probe runner reads stage_latency_ms via getattr; this fake is the
    minimum surface that exercises the per_query_stages projection
    without spinning up a full SearchPipeline. The "real pipeline"
    scenario uses the actual SearchPipeline for end-to-end coverage.
    """

    stage_latency_ms: dict[str, float]


class _StageAwareFakeClient:
    """Returns per-call stage maps with embed_http / vector_ann split.

    Mimics the SearchResult shape the probe runner projects per-query
    records from — used to keep the per_query_stages BDD scenario fast
    without building a full pipeline.
    """

    def __init__(self) -> None:
        self.calls = 0

    def search(self, _q: SampledQuery) -> _StagedFakeResult:
        self.calls += 1
        # Per-call variance so the per_query_stages records differ.
        return _StagedFakeResult(
            stage_latency_ms={
                "classify": 1.0,
                "embed_http": 10.0 + self.calls,
                "vector_ann": 2.0,
                "vector": 12.0 + self.calls,
                "fuse": 0.5,
            }
        )


class _LatencyInjectingClient:
    """One slow query + many fast queries — pins tail-surfacing behaviour."""

    def search(self, q: SampledQuery) -> dict[str, str]:
        if _SLOW_MARKER in q.query:
            time.sleep(0.08)  # 80ms slow query — well above fast-path noise
        return {"results": "ok"}


def _build_real_pipeline_searcher() -> Any:
    """Real SearchPipeline composed from canonical fakes; adapt to runner shape."""
    doc_repo = FakeDocumentRepository(
        documents=[{"path": "p.md", "title": "T", "content": "alpha bravo charlie", "collection": "c"}]
    )
    pipeline = SearchPipeline(
        classifier=FakeClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=[{"path": "v.md", "distance": 0.1, "collection": "c"}]),
        ),
        graph=FakeGraphRepository(available=True),
        fusion=FakeFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )

    def _search(q: SampledQuery) -> Any:
        return pipeline.search(query=q.query, agent=q.agent)

    return _search


@pytest.fixture
def _state() -> dict[str, Any]:
    """Per-scenario fresh state — no cross-scenario bleed."""
    return {
        "searcher": None,
        "loader": _default_loader,
        "result": None,
    }


# ---------------------------------------------------------------------------
# Given — wire up the fake / real pipeline
# ---------------------------------------------------------------------------


@given("a stage-aware fake search client returning a per-query stage map")
def _given_stage_aware_client(_state: dict[str, Any]) -> None:
    _state["searcher"] = _StageAwareFakeClient().search


@given("a real search pipeline composed from canonical fakes")
def _given_real_pipeline(_state: dict[str, Any]) -> None:
    _state["searcher"] = _build_real_pipeline_searcher()


@given("a fake search client that returns one slow query and many fast queries")
def _given_latency_injecting_client(_state: dict[str, Any]) -> None:
    _state["searcher"] = _LatencyInjectingClient().search
    _state["loader"] = _slow_loader


# ---------------------------------------------------------------------------
# When — run the probe
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs the probe with {n:d} queries at concurrency {c:d}"))
def _when_run_probe(_state: dict[str, Any], n: int, c: int) -> None:
    _state["result"] = run_probe_search(
        suite="fake",
        queries=n,
        concurrency=c,
        suite_loader=_state["loader"],
        searcher=_state["searcher"],
    )


# ---------------------------------------------------------------------------
# Then — assertions
# ---------------------------------------------------------------------------


@then("the probe envelope contains a per_query_stages list")
def _then_envelope_has_per_query_stages(_state: dict[str, Any]) -> None:
    result: ProbeResult = _state["result"]
    env = result.to_envelope()
    # Sabotage: drop ``per_query_stages`` from the envelope projection
    # and operators lose the slow-query surfacing.
    assert "per_query_stages" in env, f"envelope missing per_query_stages; keys={sorted(env.keys())}"
    assert isinstance(env["per_query_stages"], list), f"expected list; got {type(env['per_query_stages']).__name__}"
    assert len(env["per_query_stages"]) > 0, "expected per_query_stages to be non-empty after a probe run"


@then("each per_query_stages record carries case_id and category and latency_ms and stage_latency_ms")
def _then_records_have_documented_keys(_state: dict[str, Any]) -> None:
    result: ProbeResult = _state["result"]
    required = {"case_id", "category", "latency_ms", "stage_latency_ms"}
    # Sabotage: drop any one of these keys from ``_per_query_stages`` and
    # an operator can't filter the slow-query list to a useful subset.
    for record in result.per_query_stages:
        missing = required - set(record.keys())
        assert not missing, f"record {record!r} missing keys {missing!r}"
        assert isinstance(record["case_id"], str) and record["case_id"]
        assert isinstance(record["category"], str) and record["category"]
        assert isinstance(record["latency_ms"], float)
        assert isinstance(record["stage_latency_ms"], dict)


@then("every per-query stage map contains embed_http and vector_ann")
def _then_records_have_split_stages(_state: dict[str, Any]) -> None:
    result: ProbeResult = _state["result"]
    # Sabotage: remove the ``timings=stages`` forwarding in
    # ``_dispatch_vector`` and these keys never appear — the parent
    # ``vector`` stage stays, so a coarser reader (mean latency
    # bottleneck heuristic) wouldn't notice; only this BDD scenario
    # catches the regression.
    for record in result.per_query_stages:
        stage_map = record["stage_latency_ms"]
        assert "embed_http" in stage_map, f"missing embed_http in {stage_map!r}"
        assert "vector_ann" in stage_map, f"missing vector_ann in {stage_map!r}"


@then("the sum of embed_http and vector_ann approximates the vector total")
def _then_split_sums_to_total(_state: dict[str, Any]) -> None:
    result: ProbeResult = _state["result"]
    # Sabotage: stop the embed timer before the embed call returns and
    # the split sum would diverge from the parent vector total by more
    # than measurement noise.
    for record in result.per_query_stages:
        stage_map = record["stage_latency_ms"]
        split_sum = stage_map["embed_http"] + stage_map["vector_ann"]
        vector_total = stage_map["vector"]
        assert abs(split_sum - vector_total) <= 2.0, (
            f"split sum {split_sum} diverged from vector total {vector_total} "
            f"beyond noise tolerance (record={record!r})"
        )


@then("the slow query latency is visibly higher than every fast query latency")
def _then_slow_query_dominates(_state: dict[str, Any]) -> None:
    result: ProbeResult = _state["result"]
    slow_records = [r for r in result.per_query_stages if r["case_id"] == "slow-1"]
    if not slow_records:
        # Sampler didn't pick the slow case this run — assert latency
        # variance is non-trivial so a sort-by-latency would still be
        # meaningful (the operator's primary use case).
        latencies = [r["latency_ms"] for r in result.per_query_stages]
        assert len(set(latencies)) > 1, "expected per-query latency variance even without slow case"
        return
    fast_max = max(r["latency_ms"] for r in result.per_query_stages if r["case_id"] != "slow-1")
    slow_latency = slow_records[0]["latency_ms"]
    # Sabotage: average durations into latency_ms (instead of recording
    # per-task duration_ms) and the slow query disappears into the mean,
    # which is exactly the tail-hiding pathology this whole change is
    # designed to fix.
    assert slow_latency > fast_max, (
        f"slow query latency ({slow_latency}ms) did not exceed fastest non-slow "
        f"latency ({fast_max}ms) — tail signal lost"
    )
