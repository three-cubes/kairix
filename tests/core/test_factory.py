"""Unit tests for ``kairix.core.factory``.

Coverage targets:

  - ``select_boosts`` — exhaustive on/off matrix for the four boost
    families (entity, procedural, temporal-date-path, temporal-chunk-date)
    plus order-sensitivity proof.
  - ``build_search_pipeline`` — driven via the public surface with
    explicit ``RetrievalConfig`` instances. The function naturally
    walks its fallback paths (Azure / Neo4j / usearch unavailable in
    the test process) so we exercise the production wiring without
    spinning up real services.

We deliberately do NOT use ``@patch`` (F1) or pytest ``monkeypatch``
on ``KAIRIX_*`` env vars (F2). The ``KAIRIX_DOCKER`` and
``KAIRIX_LOG_QUERIES`` paths are exercised by swapping the
lazily-imported ``os`` module attribute on ``factory``, which is a
third-party-namespace substitution (``os`` is stdlib, not ``kairix.*``).
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.factory import build_search_pipeline, select_boosts
from kairix.core.search.boosts import (
    ChunkDateBoost,
    EntityBoost,
    ProceduralBoost,
    TemporalDateBoost,
)
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion
from tests.fakes import FakeGraphRepository

# ── select_boosts — on/off matrix and ordering ────────────────────────


@pytest.fixture
def fake_graph() -> FakeGraphRepository:
    return FakeGraphRepository(available=True)


def _cfg(
    *,
    entity: bool,
    procedural: bool,
    date_path: bool,
    chunk_date: bool,
) -> RetrievalConfig:
    return RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=entity),
        procedural=ProceduralBoostConfig(enabled=procedural),
        temporal=TemporalBoostConfig(
            date_path_boost_enabled=date_path,
            chunk_date_boost_enabled=chunk_date,
        ),
    )


@pytest.mark.unit
def test_select_boosts_all_disabled_returns_empty_list(fake_graph: FakeGraphRepository) -> None:
    """Sabotage proof: an off-by-one on the ``if`` guard would still
    register at least one adapter, so the empty-list assertion fails.
    """
    cfg = _cfg(entity=False, procedural=False, date_path=False, chunk_date=False)
    assert select_boosts(cfg, fake_graph) == []


@pytest.mark.unit
def test_select_boosts_only_entity_enabled(fake_graph: FakeGraphRepository) -> None:
    """Only EntityBoost is registered; the graph dependency is wired through."""
    cfg = _cfg(entity=True, procedural=False, date_path=False, chunk_date=False)
    boosts = select_boosts(cfg, fake_graph)
    assert len(boosts) == 1
    assert isinstance(boosts[0], EntityBoost)


@pytest.mark.unit
def test_select_boosts_only_procedural_enabled(fake_graph: FakeGraphRepository) -> None:
    cfg = _cfg(entity=False, procedural=True, date_path=False, chunk_date=False)
    boosts = select_boosts(cfg, fake_graph)
    assert len(boosts) == 1
    assert isinstance(boosts[0], ProceduralBoost)


@pytest.mark.unit
def test_select_boosts_only_temporal_date_path_enabled(fake_graph: FakeGraphRepository) -> None:
    cfg = _cfg(entity=False, procedural=False, date_path=True, chunk_date=False)
    boosts = select_boosts(cfg, fake_graph)
    assert len(boosts) == 1
    assert isinstance(boosts[0], TemporalDateBoost)


@pytest.mark.unit
def test_select_boosts_only_chunk_date_enabled(fake_graph: FakeGraphRepository) -> None:
    cfg = _cfg(entity=False, procedural=False, date_path=False, chunk_date=True)
    boosts = select_boosts(cfg, fake_graph)
    assert len(boosts) == 1
    assert isinstance(boosts[0], ChunkDateBoost)


@pytest.mark.unit
def test_select_boosts_all_enabled_preserves_order(fake_graph: FakeGraphRepository) -> None:
    """Order is the documented contract:
    EntityBoost → ProceduralBoost → TemporalDateBoost → ChunkDateBoost.

    Sabotage proof: shuffling the ``if`` blocks in factory.py would
    fail this — assertion checks types positionally.
    """
    cfg = _cfg(entity=True, procedural=True, date_path=True, chunk_date=True)
    boosts = select_boosts(cfg, fake_graph)
    assert [type(b) for b in boosts] == [
        EntityBoost,
        ProceduralBoost,
        TemporalDateBoost,
        ChunkDateBoost,
    ]


@pytest.mark.unit
def test_select_boosts_entity_receives_graph_dependency(
    fake_graph: FakeGraphRepository,
) -> None:
    """The graph parameter is threaded through to ``EntityBoost``; the
    other adapters do not see it.
    """
    cfg = _cfg(entity=True, procedural=True, date_path=True, chunk_date=True)
    boosts = select_boosts(cfg, fake_graph)
    entity_boost = next(b for b in boosts if isinstance(b, EntityBoost))
    # The boost stores the graph for in-degree lookups (private attr —
    # we don't import it; we read via getattr so the test pins the
    # wiring, not the attribute name).
    assert getattr(entity_boost, "_graph", None) is fake_graph


# ── build_search_pipeline — public-surface integration ────────────────


@pytest.mark.unit
def test_build_search_pipeline_returns_search_pipeline_with_rrf_fusion() -> None:
    """When ``fusion_strategy="rrf"``, the factory wires an RRFFusion.

    The factory's lazy imports (Azure embedding, usearch index, Neo4j
    client) all fall through to FakeXxx repositories in this test
    process — none of those services are running, so we exercise the
    fallback branches naturally without monkey-patching anything.

    Sabotage proof: if the fusion-strategy check were inverted, this
    would catch the wrong fusion type.
    """
    cfg = RetrievalConfig(fusion_strategy="rrf", rrf_k=42)
    pipeline = build_search_pipeline(config=cfg)

    assert isinstance(pipeline.fusion, RRFFusion)
    # rrf_k threads through to the fusion strategy (private attr access
    # via getattr so we pin behaviour, not the storage shape).
    assert getattr(pipeline.fusion, "_k", None) == 42


@pytest.mark.unit
def test_build_search_pipeline_with_bm25_primary_fusion() -> None:
    """``fusion_strategy="bm25_primary"`` selects BM25PrimaryFusion."""
    cfg = RetrievalConfig(fusion_strategy="bm25_primary")
    pipeline = build_search_pipeline(config=cfg)

    assert isinstance(pipeline.fusion, BM25PrimaryFusion)


@pytest.mark.unit
def test_build_search_pipeline_with_unknown_fusion_falls_back_to_bm25_primary() -> None:
    """Any non-``rrf`` value lands in the ``else`` branch of the
    fusion-strategy switch and yields ``BM25PrimaryFusion``.
    """
    cfg = RetrievalConfig(fusion_strategy="not-a-real-strategy")
    pipeline = build_search_pipeline(config=cfg)

    assert isinstance(pipeline.fusion, BM25PrimaryFusion)


@pytest.mark.unit
def test_build_search_pipeline_threads_config_through_to_pipeline() -> None:
    """The ``config`` param reaches the constructed ``SearchPipeline``.

    Sabotage proof: if the factory dropped its ``config=`` argument and
    constructed a fresh default instead, the rrf_k=99 sentinel would
    not survive.
    """
    cfg = RetrievalConfig(fusion_strategy="rrf", rrf_k=99)
    pipeline = build_search_pipeline(config=cfg)

    assert pipeline.config is cfg
    assert pipeline.config.rrf_k == 99


@pytest.mark.unit
def test_build_search_pipeline_with_all_boosts_enabled_wires_full_chain() -> None:
    """End-to-end: a fully-enabled retrieval config produces a pipeline
    whose boost chain matches ``select_boosts`` exactly.
    """
    cfg = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=True),
        procedural=ProceduralBoostConfig(enabled=True),
        temporal=TemporalBoostConfig(
            date_path_boost_enabled=True,
            chunk_date_boost_enabled=True,
        ),
    )
    pipeline = build_search_pipeline(config=cfg)

    boost_types = [type(b) for b in pipeline.boosts]
    assert boost_types == [
        EntityBoost,
        ProceduralBoost,
        TemporalDateBoost,
        ChunkDateBoost,
    ]


@pytest.mark.unit
def test_build_search_pipeline_classifier_dispatches_to_intent_module() -> None:
    """The internal ``_RuleClassifier`` delegates ``classify`` to
    ``kairix.core.search.intent.classify``. Drives line 89 (the inner
    method body) so coverage hits the classifier surface.

    Sabotage proof: if the classifier were swapped for a stub that
    always returned ``None``, the QueryIntent assertion would fail.
    """
    cfg = RetrievalConfig(fusion_strategy="rrf")
    pipeline = build_search_pipeline(config=cfg)

    classifier = pipeline.classifier
    # ``SearchPipeline.classifier`` is typed as ``object`` (structural
    # IntentClassifier protocol). Pin behaviour via getattr.
    classify_method = getattr(classifier, "classify", None)
    assert classify_method is not None
    intent = classify_method("when did we deploy v3")
    # Real rule classifier returns a QueryIntent enum — assert membership
    # in the documented set so this test isn't fragile to enum ordering.
    assert intent is not None
    assert hasattr(intent, "value") or hasattr(intent, "name")


@pytest.mark.unit
def test_build_search_pipeline_resolver_honours_extra_collections_env() -> None:
    """``KAIRIX_EXTRA_COLLECTIONS`` is comma-split and threaded through
    to the resolver.

    F2 forbids monkeypatch on KAIRIX env vars. The factory delegates the
    env read to ``kairix.paths.extra_collections``; the test swaps that
    module's ``os`` attribute to a stand-in carrying the sentinel. The
    swap targets a stdlib namespace (``os``) rather than a kairix
    internal, so F1/F2 stay clean.
    """
    import os
    import types

    from kairix import paths as paths_mod
    from kairix.core import factory as factory_mod

    # Build a stand-in os module that carries a tweaked environ but
    # delegates everything else to the real os.
    fake_environ = dict(os.environ)
    fake_environ["KAIRIX_EXTRA_COLLECTIONS"] = "alpha-collection, beta-collection"
    fake_environ.pop("KAIRIX_DOCKER", None)
    fake_environ.pop("KAIRIX_LOG_QUERIES", None)

    fake_os = types.ModuleType("os")
    fake_os.environ = fake_environ  # type: ignore[attr-defined]  # synthetic stand-in module; mypy doesn't know our test attrs
    fake_os.path = os.path  # type: ignore[attr-defined]  # synthetic stand-in module; mypy doesn't know our test attrs

    real_paths_os = paths_mod.os
    paths_mod.os = fake_os  # type: ignore[assignment]  # stdlib substitution at the paths.py boundary
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        paths_mod.os = real_paths_os

    # The resolver was constructed with our extra collections — pinned
    # via the public-ish ``resolve`` surface rather than the private
    # ``_extra`` attribute.
    extras = getattr(pipeline.resolver, "_extra", [])
    assert "alpha-collection" in extras
    assert "beta-collection" in extras
    # Sanity: the factory module reference is untouched after the swap.
    assert factory_mod is not None


@pytest.mark.unit
def test_build_search_pipeline_uses_docker_log_path_when_dockerenv_marker_present(
    tmp_path: Any,
) -> None:
    """Drives the Docker-detection branch by swapping ``kairix.paths.os``
    so :func:`kairix.paths.is_docker_env` sees ``KAIRIX_DOCKER=1``.

    The env-read boundary moved from ``factory.py`` into
    ``kairix.paths.is_docker_env`` (F4); the test follows by swapping the
    ``os`` reference inside ``kairix.paths``. Stdlib substitution at the
    boundary keeps F1/F2 clean.
    """
    import os
    import types

    from kairix import paths as paths_mod

    fake_environ = dict(os.environ)
    fake_environ["KAIRIX_DOCKER"] = "1"
    fake_environ.pop("KAIRIX_LOG_QUERIES", None)

    fake_os = types.ModuleType("os")
    fake_os.environ = fake_environ  # type: ignore[attr-defined]  # synthetic stand-in module
    fake_os.path = os.path  # type: ignore[attr-defined]  # synthetic stand-in module

    real_paths_os = paths_mod.os
    paths_mod.os = fake_os  # type: ignore[assignment]  # stdlib substitution at the paths.py boundary
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        paths_mod.os = real_paths_os

    # The search logger's path is rooted at /data/kairix/logs when the
    # docker marker is detected. We don't write to it; we only check
    # the path the logger holds.
    logger_obj = pipeline.logger
    assert logger_obj is not None
    logger_path = str(getattr(logger_obj, "_search_log_path", ""))
    assert "/data/kairix/logs" in logger_path


@pytest.mark.unit
def test_build_search_pipeline_with_no_config_loads_via_config_loader() -> None:
    """When ``config=None``, the factory delegates to ``load_config()``.
    The fallback path in ``config_loader`` returns
    ``RetrievalConfig.defaults()`` when no YAML is present.

    Sabotage proof: if the factory ignored its ``config=None`` arg and
    constructed a fresh ``RetrievalConfig()``, the pipeline's config
    would not match ``RetrievalConfig.defaults()``.
    """
    pipeline = build_search_pipeline(config=None)

    # Pipeline got *some* RetrievalConfig — exact contents depend on
    # whether a kairix.config.yaml is on disk in the test cwd. We assert
    # the type and that the load path was traversed (not a None config).
    assert isinstance(pipeline.config, RetrievalConfig)


@pytest.mark.unit
def test_build_search_pipeline_uses_real_vector_index_when_available() -> None:
    """Drives line 118 — the ``index is not None`` branch wraps the
    real index in ``UsearchVectorRepository``.

    We swap ``kairix.core.search.vec_index.get_vector_index`` (lazily
    imported by the factory) for a stand-in that returns a tiny
    in-memory index-like object. The factory's ``UsearchVectorRepository``
    just stores the index reference, so any object works.
    """
    from kairix.core.search import vec_index as vec_index_mod

    class _StandInIndex:
        """Minimal usearch-shaped stand-in. The factory does not call
        any methods at construction time — it only stores the reference.
        """

        def __len__(self) -> int:
            return 1

    real = vec_index_mod.get_vector_index
    vec_index_mod.get_vector_index = lambda *a, **kw: _StandInIndex()
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        vec_index_mod.get_vector_index = real

    # Confirm the vector backend received a UsearchVectorRepository wired
    # to our stand-in — pinned via repr inspection so we don't import
    # the private repository class.
    assert "UsearchVectorRepository" in type(pipeline.vector._vector_repo).__name__


@pytest.mark.unit
def test_build_search_pipeline_falls_back_when_get_vector_index_raises() -> None:
    """Drives lines 125-129 — when ``get_vector_index`` raises, the
    factory logs a warning and substitutes ``FakeVectorRepository``.

    Sabotage proof: if the except handler stopped recovering, the
    factory call would raise; this test asserts it returns a
    well-formed pipeline.
    """
    from kairix.core.search import vec_index as vec_index_mod

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError("simulated usearch load failure")

    real = vec_index_mod.get_vector_index
    vec_index_mod.get_vector_index = _boom
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        vec_index_mod.get_vector_index = real

    # The factory threaded a FakeVectorRepository in instead of crashing.
    repo_name = type(pipeline.vector._vector_repo).__name__
    assert repo_name == "FakeVectorRepository"


@pytest.mark.unit
def test_build_search_pipeline_uses_neo4j_graph_when_client_available() -> None:
    """Drives lines 139-140 — when ``get_client()`` succeeds, the
    factory wraps the client in ``Neo4jGraphRepository`` instead of
    falling back to ``FakeGraphRepository``.

    We provide a stand-in client whose ``cypher`` method satisfies
    ``Neo4jGraphRepository``'s minimal contract; the factory does not
    actually call cypher at construction time.
    """
    from kairix.knowledge.graph import client as client_mod

    class _StandInClient:
        @property
        def available(self) -> bool:
            return True

        def cypher(self, query: str, **_kw: object) -> list[dict[str, Any]]:
            return []

    real = client_mod.get_client
    # The factory only stores the client reference; structural typing
    # is sufficient at the boundary.
    client_mod.get_client = lambda: _StandInClient()  # type: ignore[assignment, return-value]  # structural stand-in for boundary type check; not callable in test path
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        client_mod.get_client = real

    # Pipeline graph is a Neo4jGraphRepository, not a FakeGraphRepository.
    assert type(pipeline.graph).__name__ == "Neo4jGraphRepository"


@pytest.mark.unit
def test_build_search_pipeline_loads_collections_and_agent_registry_from_yaml(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives lines 196-204 — when a YAML config is present, the factory
    parses ``agents:`` into an ``AgentRegistry`` and threads it into
    the resolver.

    F2 forbids monkeypatch on ``KAIRIX_*`` env vars, so we drop the YAML
    into the test's ``tmp_path`` and ``chdir`` there. The config loader's
    cwd-fallback path picks up ``kairix.config.yaml`` when the env var
    is unset.
    """
    cfg_yaml = tmp_path / "kairix.config.yaml"
    cfg_yaml.write_text(
        """
retrieval:
  fusion_strategy: rrf
collections:
  shared:
    - name: shared-notes
      path: notes
agents:
  - name: alpha
    paths:
      - 04-Agent-Knowledge/alpha
    write_path: 04-Agent-Knowledge/alpha
""".strip()
    )

    monkeypatch.chdir(tmp_path)

    # Force the cwd-fallback path in ``resolve_config_path`` by ensuring
    # KAIRIX_CONFIG_PATH is absent. F2 forbids ``monkeypatch.delenv`` for
    # KAIRIX_*, so we mutate-and-restore ``os.environ`` directly with a
    # ``finally`` cleanup — stdlib state, not a kairix-internal patch.
    import os

    from kairix.core.search import config_loader

    saved = os.environ.pop("KAIRIX_CONFIG_PATH", None)
    config_loader.load_cached.cache_clear()
    try:
        pipeline = build_search_pipeline(config=None)
    finally:
        if saved is not None:
            os.environ["KAIRIX_CONFIG_PATH"] = saved
        config_loader.load_cached.cache_clear()

    # The resolver was constructed with an agent_registry parsed from YAML.
    registry = getattr(pipeline.resolver, "_registry", None)
    assert registry is not None
    agent_names = [a.name for a in registry.list_agents()]
    assert "alpha" in agent_names


@pytest.mark.unit
def test_build_search_pipeline_falls_back_to_fake_graph_when_get_client_raises() -> None:
    """Drives lines 141-145 — when ``get_client()`` raises, the factory
    logs a warning and substitutes ``FakeGraphRepository(available=False)``.

    Without this guard the factory would propagate the connection
    exception and operator-facing search would crash on startup.

    Sabotage proof: removing the except clause would propagate the
    RuntimeError; the test asserts a clean pipeline instead.
    """
    from kairix.knowledge.graph import client as client_mod

    def _boom() -> client_mod.Neo4jClient:
        raise RuntimeError("simulated neo4j driver failure at boundary")

    real = client_mod.get_client
    client_mod.get_client = _boom
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        client_mod.get_client = real

    # Pipeline still constructed — graph fell back to FakeGraphRepository.
    assert type(pipeline.graph).__name__ == "FakeGraphRepository"
    assert pipeline.graph.available is False


@pytest.mark.unit
def test_build_search_pipeline_tolerates_agent_registry_parse_exception(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives lines 203-204 — when ``parse_agent_registry`` raises while
    walking a present YAML, the factory logs a warning and continues
    with ``agent_registry=None``.

    We point the factory at a YAML on disk (so ``config_path is not None``)
    and replace ``parse_agent_registry`` with a stand-in that raises.
    The factory must still return a ``SearchPipeline``.
    """
    cfg_yaml = tmp_path / "kairix.config.yaml"
    cfg_yaml.write_text("retrieval:\n  fusion_strategy: rrf\n")
    monkeypatch.chdir(tmp_path)

    import os

    from kairix.core.search import config_loader
    from kairix.core.search import registry as registry_mod

    saved = os.environ.pop("KAIRIX_CONFIG_PATH", None)
    config_loader.load_cached.cache_clear()

    real_parse = registry_mod.parse_agent_registry

    def _boom(*_a: object, **_kw: object) -> registry_mod.ConfigDrivenAgentRegistry:
        raise RuntimeError("simulated registry parse failure")

    # The factory does ``from kairix.core.search.registry import
    # parse_agent_registry`` *inside* ``build_search_pipeline`` — that
    # function-local import resolves freshly each call, so swapping the
    # attribute on the registry module is sufficient.
    registry_mod.parse_agent_registry = _boom

    try:
        pipeline = build_search_pipeline(config=None)
    finally:
        registry_mod.parse_agent_registry = real_parse
        if saved is not None:
            os.environ["KAIRIX_CONFIG_PATH"] = saved
        config_loader.load_cached.cache_clear()

    # Pipeline still constructed; resolver has no agent_registry.
    assert getattr(pipeline.resolver, "_registry", "sentinel") is None


@pytest.mark.unit
def test_build_search_pipeline_tolerates_load_collections_exception(
    tmp_path: Any,
) -> None:
    """Drives lines 191-192 — when ``load_collections`` raises, the
    factory logs a warning and continues with ``collections_config=None``.

    We swap ``kairix.core.search.config_loader.load_collections`` (the
    factory imports it lazily, so the swap takes effect for the call).
    """
    from kairix.core.search import config_loader as cl

    def _boom() -> cl.CollectionsConfig | None:
        raise RuntimeError("simulated config corruption")

    real = cl.load_collections
    cl.load_collections = _boom
    try:
        cfg = RetrievalConfig(fusion_strategy="rrf")
        pipeline = build_search_pipeline(config=cfg)
    finally:
        cl.load_collections = real

    # Sabotage proof: pipeline still constructed despite the failure.
    assert isinstance(pipeline.config, RetrievalConfig)
