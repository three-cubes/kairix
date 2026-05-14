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
# _default_search — wraps ``build_search_pipeline().search(**kwargs)``.
#
# This is the F6 "production default" hook ``BenchmarkPipeline.search_fn``
# resolves to when no override is passed. Today the retrieval layer
# (``runner.retrieve_case``) owns the actual call so the default is only
# invoked by a future caller wiring ``search_fn`` into a custom flow;
# but it must still be exercised so F7 sees it green. We
# ``monkeypatch.setattr`` the lazy import target rather than calling
# ``build_search_pipeline()`` for real (that needs a populated DB).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_search_delegates_to_built_search_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_search`` lazy-imports the factory, builds a pipeline,
    and forwards every kwarg to ``pipeline.search``.

    Sabotage-prove: if the wrapper dropped a kwarg (e.g. ``budget``)
    the captured kwargs would be incomplete and the assertion below
    would catch it.
    """
    import kairix.core.factory as factory_mod
    from kairix.quality.benchmark import pipeline as bench_pipeline_mod

    captured: list[dict[str, Any]] = []
    search_sentinel = object()

    class _FakePipeline:
        def search(self, **kwargs: Any) -> object:
            captured.append(kwargs)
            return search_sentinel

    monkeypatch.setattr(factory_mod, "build_search_pipeline", lambda: _FakePipeline())

    out = bench_pipeline_mod._default_search(query="hello", budget=2000, agent="alpha", scope="shared")

    assert out is search_sentinel
    assert captured == [{"query": "hello", "budget": 2000, "agent": "alpha", "scope": "shared"}]


@pytest.mark.unit
def test_default_search_returns_each_call_via_fresh_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two invocations build the pipeline twice — no module-level cache.

    Sabotage-prove: if a future refactor cached the pipeline at module
    scope, this test fails because the build count would be 1, not 2.
    A cached pipeline would silently break tests that inject a different
    backend per call.
    """
    import kairix.core.factory as factory_mod
    from kairix.quality.benchmark import pipeline as bench_pipeline_mod

    builds: list[int] = []

    class _FakePipeline:
        def search(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True}

    def _fake_build() -> _FakePipeline:
        builds.append(1)
        return _FakePipeline()

    monkeypatch.setattr(factory_mod, "build_search_pipeline", _fake_build)

    bench_pipeline_mod._default_search(query="q1")
    bench_pipeline_mod._default_search(query="q2")

    assert len(builds) == 2


@pytest.mark.unit
def test_benchmark_pipeline_default_search_fn_is_default_search() -> None:
    """``BenchmarkPipeline()`` wires ``search_fn`` to the module's lazy default.

    Sabotage-prove for F6: if a future refactor flipped the field to
    ``Callable | None = None`` the assertion would fail because the
    default-factory wiring is the entire reason F6 exists.
    """
    from kairix.quality.benchmark import pipeline as bench_pipeline_mod

    instance = BenchmarkPipeline()
    assert instance.search_fn is bench_pipeline_mod._default_search
