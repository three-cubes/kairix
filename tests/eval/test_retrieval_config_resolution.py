"""Contract probes for kairix.quality.eval.retrieval — config resolution wiring.

Pins the eval-tooling contract that the *resolved* RetrievalConfig
(per-collection overrides + fusion_override layered on the global YAML
config) flows through to the pipeline factory before the pipeline is
built. Closes #112 for the eval/benchmark path: ``--collection X`` now
receives X's tuned overrides, and the historical ``fusion_override``
ordering bug (config reassigned *after* the pipeline was built) is gone.

The tests substitute the production ``build_search_pipeline`` with a
``_PipelineBuilderSpy`` via ``RetrievalDeps(pipeline_builder=...)`` so
the resolved config can be observed without spinning up Azure / Neo4j /
usearch. ``RetrievalDeps`` (issue #199) replaced the F6-violating
``search_fn=`` / ``pipeline_builder=`` test-only kwargs.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kairix.core.search import config_loader
from kairix.core.search.config import RetrievalConfig
from kairix.quality.eval.retrieval import RetrievalDeps, retrieve

pytestmark = pytest.mark.contract


@dataclass
class _CapturedSearchResult:
    """Minimal stand-in for SearchResult — only the fields _retrieve_hybrid reads."""

    results: list[Any] = field(default_factory=list)
    intent: Any = field(default_factory=lambda: type("Intent", (), {"value": "semantic"})())
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
    vec_failed: bool = False
    latency_ms: float = 0.0


class _PipelineSpy:
    """Records the search call args and returns an empty SearchResult-shaped object."""

    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config
        self.search_calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> _CapturedSearchResult:
        self.search_calls.append(kwargs)
        return _CapturedSearchResult()


def _builder_spy() -> tuple[Any, list[RetrievalConfig]]:
    """Return (builder_fn, captured_configs). The builder records every config
    it sees and returns a _PipelineSpy bound to that config.
    """
    captured: list[RetrievalConfig] = []

    def _builder(config: RetrievalConfig | None = None) -> _PipelineSpy:
        assert config is not None, "factory must receive a non-None config from _retrieve_hybrid"
        captured.append(config)
        return _PipelineSpy(config)

    return _builder, captured


@pytest.fixture(autouse=True)
def _isolated_cache_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each probe gets a fresh load-cache and a clean cwd."""
    config_loader._load_cached.cache_clear()
    monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    config_loader._load_cached.cache_clear()


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body))
    return path


def test_per_collection_override_flows_into_pipeline_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``retrieve(collection="X")`` is called and the YAML carries a
    ``retrieval:`` override under collection X, the *resolved* config that
    reaches ``build_search_pipeline`` reflects that override.

    This is the core #112 contract: ``--collection reference-library``
    must receive reflib's tuned config, not ``RetrievalConfig.defaults()``.
    """
    cfg_file = _write_yaml(
        tmp_path / "kairix.config.yaml",
        """
        retrieval:
          fusion_strategy: bm25_primary
          rrf_k: 60
        collections:
          shared:
            - name: reflib-test
              path: docs
              retrieval:
                fusion_strategy: rrf
                rrf_k: 10
        """,
    )
    monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))

    builder, captured = _builder_spy()
    retrieve(
        query="anything",
        system="hybrid",
        collection="reflib-test",
        deps=RetrievalDeps(pipeline_builder=builder),
    )

    assert len(captured) == 1, f"expected exactly one pipeline build; got {len(captured)}"
    cfg = captured[0]
    # Override applied: fusion strategy + rrf_k come from the per-collection block.
    assert cfg.fusion_strategy == "rrf", f"expected per-collection override 'rrf', got {cfg.fusion_strategy!r}"
    assert cfg.rrf_k == 10, f"expected per-collection rrf_k=10, got {cfg.rrf_k}"


def test_global_config_used_when_no_per_collection_override_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage check: a different collection (or none) gets the global
    config, not the override. Proves the per-collection lookup is keyed
    on the collection name, not "always wins".
    """
    cfg_file = _write_yaml(
        tmp_path / "kairix.config.yaml",
        """
        retrieval:
          fusion_strategy: bm25_primary
          rrf_k: 60
        collections:
          shared:
            - name: reflib-test
              path: docs
              retrieval:
                fusion_strategy: rrf
                rrf_k: 10
            - name: vault-areas
              path: areas
        """,
    )
    monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))

    builder, captured = _builder_spy()
    # vault-areas has no per-collection retrieval block → global wins.
    retrieve(
        query="anything",
        system="hybrid",
        collection="vault-areas",
        deps=RetrievalDeps(pipeline_builder=builder),
    )

    cfg = captured[0]
    assert cfg.fusion_strategy == "bm25_primary", (
        f"expected global 'bm25_primary' for vault-areas (no override), got {cfg.fusion_strategy!r}"
    )
    assert cfg.rrf_k == 60


def test_fusion_override_layered_on_top_of_resolved_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-fix bug: ``fusion_override`` was reassigned to ``config`` AFTER
    the pipeline was already built, so the override never reached the
    pipeline. The fix resolves config first, applies override, THEN
    builds the pipeline.

    This test pins the corrected order: when ``fusion_override='rrf'`` is
    passed, the pipeline receives a config with ``fusion_strategy=='rrf'``
    regardless of what the YAML or per-collection override said.
    """
    cfg_file = _write_yaml(
        tmp_path / "kairix.config.yaml",
        """
        retrieval:
          fusion_strategy: bm25_primary
        collections:
          shared:
            - name: docs
              path: docs
              retrieval:
                fusion_strategy: bm25_primary
        """,
    )
    monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))

    builder, captured = _builder_spy()
    retrieve(
        query="x",
        system="hybrid",
        collection="docs",
        fusion_override="rrf",
        deps=RetrievalDeps(pipeline_builder=builder),
    )

    cfg = captured[0]
    # Override beats both global and per-collection settings.
    assert cfg.fusion_strategy == "rrf", (
        f"fusion_override='rrf' did not reach pipeline; got fusion_strategy={cfg.fusion_strategy!r}. "
        "This is the historical reorder bug (config reassigned after pipeline already built)."
    )


def test_explicit_config_bypasses_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``config=`` argument is identity-passed through to the
    pipeline builder — no merge, no resolution, no override layering.

    Sabotage check: the resolved-config path should NOT consume YAML when
    the caller has already done the work.
    """
    cfg_file = _write_yaml(
        tmp_path / "kairix.config.yaml",
        """
        retrieval:
          fusion_strategy: rrf
        """,
    )
    monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))

    explicit = RetrievalConfig.minimal()
    builder, captured = _builder_spy()
    retrieve(
        query="x",
        system="hybrid",
        config=explicit,
        deps=RetrievalDeps(pipeline_builder=builder),
    )

    # Identity-passed: same object, no merge.
    assert captured[0] is explicit


def test_retrieval_deps_default_factory_binds_callable_pipeline_builder() -> None:
    """``RetrievalDeps()`` with no overrides constructs a deps bag whose
    ``pipeline_builder`` is a callable, not ``None``.

    Sabotage proof: the issue calls out ``Optional[Callable] = None``
    self-resolving in ``__post_init__`` as the rejected pattern that
    "just landed a mypy bug". ``default_factory`` must bind a real
    callable or this assertion fires. The complementary ``searcher``
    field stays Optional because the two seams are mutually exclusive
    (a pre-bound searcher means "skip pipeline construction").
    """
    deps = RetrievalDeps()
    assert callable(deps.pipeline_builder), (
        f"default_factory must bind a callable; got {deps.pipeline_builder!r}. "
        "Regressing to ``pipeline_builder: Callable | None = None`` would leave this None."
    )
    assert deps.searcher is None, "searcher defaults to None — no pre-bound searcher"


def test_retrieval_deps_searcher_takes_precedence_over_pipeline_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``RetrievalDeps(searcher=fn)`` is provided, the pipeline builder is
    NOT invoked. Sabotage proof: the builder is a callable that raises if
    called — the test only passes when the searcher path bypasses it.
    """

    @dataclass
    class _CallableSearchResult:
        results: list[Any] = field(default_factory=list)
        intent: Any = field(default_factory=lambda: type("Intent", (), {"value": "semantic"})())
        bm25_count: int = 0
        vec_count: int = 0
        fused_count: int = 0
        vec_failed: bool = False
        latency_ms: float = 0.0

    def _exploding_builder(*, config: Any) -> Any:
        raise AssertionError(
            "pipeline_builder must NOT be called when searcher= is provided. "
            "_retrieve_hybrid bypassed the searcher seam."
        )

    captured: list[dict[str, Any]] = []

    def _spy_searcher(**kwargs: Any) -> _CallableSearchResult:
        captured.append(kwargs)
        return _CallableSearchResult()

    cfg_file = tmp_path / "kairix.config.yaml"
    cfg_file.write_text("retrieval:\n  fusion_strategy: rrf\n")
    monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))

    retrieve(
        query="hello",
        system="hybrid",
        deps=RetrievalDeps(searcher=_spy_searcher, pipeline_builder=_exploding_builder),
    )

    assert len(captured) == 1, "searcher should be called exactly once"
    assert captured[0]["query"] == "hello"
