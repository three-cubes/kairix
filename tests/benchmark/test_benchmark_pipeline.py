"""Unit tests for BenchmarkPipeline orchestrator."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.quality.benchmark.pipeline import BenchmarkPipeline
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite


def _make_mock_suite() -> BenchmarkSuite:
    """Build a minimal BenchmarkSuite with mock-backend cases."""
    return BenchmarkSuite(
        meta={"name": "test-pipeline", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="t1",
                category="recall",
                query="test query",
                gold_path="docs/test.md",
                score_method="exact",
            ),
        ],
    )


@pytest.mark.unit
class TestBenchmarkPipeline:
    def test_run_returns_benchmark_result(self) -> None:
        """Pipeline.run returns a BenchmarkResult with expected structure."""
        from kairix.quality.benchmark.runner import BenchmarkResult

        pipeline = BenchmarkPipeline(system="mock")
        suite = _make_mock_suite()
        result = pipeline.run(suite)

        assert isinstance(result, BenchmarkResult)
        assert "weighted_total" in result.summary
        assert "category_scores" in result.summary
        assert len(result.cases) == 1

    def test_run_uses_configured_system(self) -> None:
        """Pipeline passes the configured system through to runner."""
        pipeline = BenchmarkPipeline(system="mock")
        suite = _make_mock_suite()
        result = pipeline.run(suite)

        assert result.meta["system"] == "mock"

    def test_run_propagates_agent(self) -> None:
        """Pipeline passes agent to the runner."""
        pipeline = BenchmarkPipeline(system="mock", agent="builder")
        suite = _make_mock_suite()
        result = pipeline.run(suite)

        assert result.meta["agent"] == "builder"


# ---------------------------------------------------------------------------
# Default search_fn is dormant — runner.retrieve_case owns live retrieval
# today, so the production default has a ``pragma: no cover`` body. The
# only contract worth pinning is that BenchmarkPipeline() construction
# yields a callable (F6: no ``Callable | None = None``).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benchmark_pipeline_default_search_fn_is_callable() -> None:
    """``BenchmarkPipeline()`` wires ``search_fn`` to the module's default
    factory output — a callable, not ``None``.

    Sabotage-prove for F6: if a future refactor flipped the field to
    ``Callable | None = None`` the assertion would fail because the
    default-factory wiring is the entire reason F6 exists.
    """
    instance = BenchmarkPipeline()
    assert callable(instance.search_fn)


@pytest.mark.unit
def test_benchmark_pipeline_search_fn_override_is_used() -> None:
    """Caller-provided ``search_fn`` overrides the dormant default.

    Sabotage-prove: if the dataclass dropped the field or wired the
    default in a way that masked the override, the captured kwargs
    would be empty and this would fail.
    """
    captured: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> dict[str, str]:
        captured.append(kwargs)
        return {"ok": "yes"}

    instance = BenchmarkPipeline(search_fn=_fake)
    result = instance.search_fn(query="hello", budget=1500)

    assert result == {"ok": "yes"}
    assert captured == [{"query": "hello", "budget": 1500}]
