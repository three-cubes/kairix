"""BenchmarkPipeline — orchestrator for retrieval quality benchmarking.

Wraps the procedural run_benchmark() function in a composable dataclass.
The search dependency is injectable (SearchPipeline or any callable matching
the SearchBackendProtocol) so tests can substitute fakes.

Production code uses build_benchmark_pipeline() from the factory;
tests construct BenchmarkPipeline directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.quality.benchmark.runner import BenchmarkResult, run_benchmark
from kairix.quality.benchmark.suite import BenchmarkSuite

logger = logging.getLogger(__name__)


def _make_default_search() -> Callable[..., Any]:
    """Default ``search_fn`` factory for :class:`BenchmarkPipeline`.

    Returns a callable that lazy-builds the production search pipeline.
    The retrieval layer (``runner.retrieve_case``) owns the live call
    today, so this default is dormant — exercised only when a future
    caller wires ``BenchmarkPipeline.search_fn`` into a custom flow.

    Mirrors ``SearchPipeline.search``'s signature via ``**kwargs`` so
    callers route ``query``/``budget``/``agent``/``scope`` through
    unchanged.
    """

    def search(**kwargs: Any) -> Any:  # pragma: no cover — dormant; runner.retrieve_case owns live retrieval today
        from kairix.core.factory import build_search_pipeline

        return build_search_pipeline().search(**kwargs)

    return search


@dataclass
class BenchmarkPipeline:
    """Composes benchmark dependencies into a runnable pipeline.

    Attributes:
        search_fn:        Callable matching the SearchPipeline.search signature.
                          Passed through to the retrieval layer. Defaults to
                          the production search pipeline via ``default_factory``.
        system:           Retrieval backend name (hybrid, bm25, mock, mock-reflib).
        agent:            Default agent for collection scoping.
        output_dir:       If set, write JSON result file here.
        db_path:          Optional path to a specific database.
        collection:       Single collection name.
        fusion_override:  Override fusion strategy.
    """

    search_fn: Callable[..., Any] = field(default_factory=_make_default_search)
    system: str = "hybrid"
    agent: str | None = None
    output_dir: str | None = None
    db_path: str | None = None
    collection: str | None = None
    fusion_override: str | None = None

    def run(self, suite: BenchmarkSuite) -> BenchmarkResult:
        """Run all benchmark cases and return results.

        Delegates to the procedural run_benchmark() with the
        configured dependencies.

        Args:
            suite: Loaded and validated BenchmarkSuite.

        Returns:
            BenchmarkResult with summary, category scores, and per-case data.
        """
        return run_benchmark(
            suite=suite,
            system=self.system,
            agent=self.agent,
            output_dir=self.output_dir,
            db_path=self.db_path,
            collection=self.collection,
            fusion_override=self.fusion_override,
        )
