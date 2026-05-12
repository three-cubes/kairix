"""Tests for benchmark --collection and --fusion CLI flags (Sprint 17 Track C1)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent

pytestmark = pytest.mark.unit


@dataclass
class _FakeSearchResult:
    """Minimal fake for SearchResult used in retrieve() tests."""

    results: list = field(default_factory=list)
    intent: QueryIntent = QueryIntent.SEMANTIC
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
    vec_failed: bool = False
    latency_ms: float = 1.0


class _CapturingSearch:
    """Callable that captures its call kwargs and returns a fixed result."""

    def __init__(self, result: _FakeSearchResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or _FakeSearchResult()

    def __call__(self, **kwargs: Any) -> _FakeSearchResult:
        self.calls.append(kwargs)
        return self._result


class TestFusionOverride:
    """Verify that fusion_override creates a new RetrievalConfig with the correct strategy."""

    def test_replace_frozen_dataclass(self) -> None:
        cfg = RetrievalConfig.defaults()
        assert cfg.fusion_strategy == "rrf"

        overridden = replace(cfg, fusion_strategy="bm25_primary")
        assert overridden.fusion_strategy == "bm25_primary"
        # Original unchanged
        assert cfg.fusion_strategy == "rrf"

    def test_replace_preserves_other_fields(self) -> None:
        cfg = RetrievalConfig.defaults()
        overridden = replace(cfg, fusion_strategy="rrf")
        assert overridden.entity == cfg.entity
        assert overridden.procedural == cfg.procedural
        assert overridden.rrf_k == cfg.rrf_k


class TestRetrieveCollectionWiring:
    """Verify that _retrieve passes collection and fusion_override to search()."""

    def test_collection_passed_to_search(self) -> None:
        from kairix.quality.benchmark.runner import retrieve

        search = _CapturingSearch()
        retrieve(
            "test query",
            "hybrid",
            "shape",
            collection="reference-library",
            searcher=search,
        )

        assert len(search.calls) == 1
        assert search.calls[0]["collections"] == ["reference-library"]

    def test_no_collection_passes_none(self) -> None:
        from kairix.quality.benchmark.runner import retrieve

        search = _CapturingSearch()
        retrieve("test query", "hybrid", "shape", searcher=search)

        assert search.calls[0]["collections"] is None

    def test_fusion_override_calls_search(self) -> None:
        """fusion_override still triggers a search call (config handled at pipeline construction)."""
        from kairix.quality.benchmark.runner import retrieve

        search = _CapturingSearch()
        retrieve("test query", "hybrid", "shape", fusion_override="rrf", searcher=search)

        assert len(search.calls) == 1
        assert search.calls[0]["query"] == "test query"


class TestRunBenchmarkMetadata:
    """Verify that collection and fusion_override appear in result metadata."""

    def test_metadata_includes_collection(self) -> None:
        from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
        from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

        def _fake_retrieve(**kwargs):
            return (["path/a.md"], ["snippet"], {"system": "hybrid"})

        suite = BenchmarkSuite(
            meta={"name": "test", "version": "1.0"},
            cases=[
                BenchmarkCase(
                    id="R01",
                    category="recall",
                    query="test",
                    gold_path="path/a.md",
                    score_method="exact",
                ),
            ],
        )

        result = run_benchmark(
            suite,
            collection="reference-library",
            fusion_override="rrf",
            deps=BenchmarkDeps(retrieve=_fake_retrieve),
        )
        assert result.meta["collection"] == "reference-library"
        assert result.meta["fusion_override"] == "rrf"

    def test_metadata_none_when_unset(self) -> None:
        from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
        from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

        def _fake_retrieve(**kwargs):
            return (["path/a.md"], ["snippet"], {"system": "hybrid"})

        suite = BenchmarkSuite(
            meta={"name": "test", "version": "1.0"},
            cases=[
                BenchmarkCase(
                    id="R01",
                    category="recall",
                    query="test",
                    gold_path="path/a.md",
                    score_method="exact",
                ),
            ],
        )

        result = run_benchmark(suite, deps=BenchmarkDeps(retrieve=_fake_retrieve))
        assert result.meta["collection"] is None
        assert result.meta["fusion_override"] is None
