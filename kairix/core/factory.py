"""Factory for constructing the production SearchPipeline.

Called once at startup. Resolves configuration, builds all protocol
implementations, and composes them into a SearchPipeline instance.

Tests construct SearchPipeline directly with fakes — this factory is
only for production wiring.
"""

from __future__ import annotations

import logging
from typing import Any

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.pipeline import SearchPipeline

logger = logging.getLogger(__name__)


def select_boosts(cfg: RetrievalConfig, graph: Any) -> list[Any]:
    """Build the production boost chain from a RetrievalConfig.

    Public helper so tests can pin which boosts the production pipeline
    actually wires for a given config — without spinning up Azure/Neo4j/SQLite.
    Each boost adapter is intent-gated internally (see kairix.core.search.boosts);
    this function only decides which adapters are *registered*, not when they
    fire.

    Args:
        cfg:   ``RetrievalConfig``. Each ``*_enabled`` flag opts the matching
               adapter into the chain.
        graph: ``GraphRepository`` for ``EntityBoost``. Other boosts ignore it.

    Returns:
        List of boost-strategy instances in registration order:
        EntityBoost → ProceduralBoost → TemporalDateBoost → ChunkDateBoost.
    """
    from kairix.core.search.boosts import (
        ChunkDateBoost,
        EntityBoost,
        ProceduralBoost,
        TemporalDateBoost,
    )

    boosts: list[Any] = []
    if cfg.entity.enabled:
        boosts.append(EntityBoost(graph=graph, config=cfg.entity))
    if cfg.procedural.enabled:
        boosts.append(ProceduralBoost(config=cfg.procedural))
    if cfg.temporal.date_path_boost_enabled:
        boosts.append(TemporalDateBoost(config=cfg.temporal))
    if cfg.temporal.chunk_date_boost_enabled:
        boosts.append(ChunkDateBoost(config=cfg.temporal))
    return boosts


def _resolve_retrieval_config(config: RetrievalConfig | None) -> RetrievalConfig:
    """Pick the explicit config or fall back to ``load_config`` (which itself
    falls back to ``RetrievalConfig.defaults()`` when no YAML is present).

    Closes #112: factory previously ignored the YAML.
    """
    if config is not None:
        return config
    from kairix.core.search.config_loader import load_config

    return load_config()


def _build_vector_repo() -> Any:
    """Construct the usearch vector repo, falling back to an empty fake on failure."""
    from kairix.core.search.vector_repository import UsearchVectorRepository
    from tests.fakes import FakeVectorRepository

    try:
        from kairix.core.search.vec_index import get_vector_index

        index = get_vector_index()
        if index is not None:
            return UsearchVectorRepository(index=index)
        logger.warning("factory: usearch index not available — vector search disabled")
    except Exception as e:
        logger.warning("factory: failed to load vector index — %s", e)
    return FakeVectorRepository()


def _build_graph() -> Any:
    """Construct the Neo4j graph repo, falling back to an unavailable fake on failure."""
    try:
        from kairix.knowledge.graph.client import get_client
        from kairix.knowledge.graph.repository import Neo4jGraphRepository

        return Neo4jGraphRepository(client=get_client())
    except Exception as e:
        from tests.fakes import FakeGraphRepository

        logger.warning("factory: Neo4j unavailable — %s", e)
        return FakeGraphRepository(available=False)


def _build_fusion(cfg: RetrievalConfig) -> Any:
    """Pick the fusion strategy by config name."""
    from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion

    if cfg.fusion_strategy == "rrf":
        return RRFFusion(k=cfg.rrf_k)
    return BM25PrimaryFusion()


def _build_search_logger() -> Any:
    """Construct the JSONL search logger, honouring docker-vs-host log paths.

    Path resolution lives at the boundary so business logic never reads env vars
    (G4). Query log is privacy-gated via KAIRIX_LOG_QUERIES (off by default).
    Env reads route through kairix.paths (F4).
    """
    from pathlib import Path

    from kairix.core.search.logger import JsonlSearchLogger, default_search_log_paths
    from kairix.paths import is_docker_env, log_queries_enabled

    log_base = Path("/data/kairix/logs") if is_docker_env() else Path.home() / ".cache" / "kairix" / "logs"
    search_log_path, query_log_path = default_search_log_paths(base=log_base)
    enable_query_log = log_queries_enabled()
    return JsonlSearchLogger(
        search_log_path=search_log_path,
        query_log_path=query_log_path if enable_query_log else None,
    )


def _build_collection_resolver() -> Any:
    """Construct the DefaultCollectionResolver from the on-disk YAML config.

    KAIRIX_EXTRA_COLLECTIONS is still honoured for ad-hoc deployments without
    a full config file.
    """
    from kairix.core.search.config_loader import load_collections, resolve_config_path
    from kairix.core.search.registry import parse_agent_registry
    from kairix.core.search.resolver import DefaultCollectionResolver
    from kairix.paths import extra_collections as _extra_collections

    collections_config = None
    try:
        collections_config = load_collections()
    except Exception as e:
        logger.warning("factory: load_collections failed — %s", e)

    agent_registry = None
    config_path = resolve_config_path()
    if config_path is not None:
        try:
            import yaml

            with config_path.open(encoding="utf-8") as f:
                raw_yaml = yaml.safe_load(f) or {}
            pattern = collections_config.agent_pattern if collections_config else "{agent}-memory"
            agent_registry = parse_agent_registry(raw_yaml, default_pattern=pattern)
        except Exception as e:
            logger.warning("factory: parse_agent_registry failed — %s", e)

    return DefaultCollectionResolver(
        collections_config=collections_config,
        extra_collections=_extra_collections(),
        agent_registry=agent_registry,
    )


def build_search_pipeline(config: RetrievalConfig | None = None) -> SearchPipeline:
    """Construct the production search pipeline.

    Resolves all dependencies from the environment (DB paths, Azure credentials,
    Neo4j connection, usearch index). Each dependency is imported lazily to avoid
    hard dependency at module load.

    Args:
        config: Explicit retrieval config. When ``None``, the factory loads
                the top-level ``retrieval:`` section from
                ``kairix.config.yaml`` via :func:`load_config`. If no YAML is
                present, falls back to ``RetrievalConfig.defaults()``.

    Returns:
        A fully wired SearchPipeline ready for search() calls.
    """
    cfg = _resolve_retrieval_config(config)

    from kairix.core.search.intent import classify as _classify_fn

    class _RuleClassifier:
        def classify(self, query: str) -> Any:
            return _classify_fn(query)

    from kairix.core.db import get_db_path
    from kairix.core.db.repository import SQLiteDocumentRepository
    from kairix.core.search.backends import (
        AzureEmbeddingService,
        BM25SearchBackend,
        VectorSearchBackend,
    )

    doc_repo = SQLiteDocumentRepository(db_path=get_db_path())
    bm25 = BM25SearchBackend(doc_repo)
    vector = VectorSearchBackend(AzureEmbeddingService(), _build_vector_repo())
    graph = _build_graph()

    return SearchPipeline(
        classifier=_RuleClassifier(),
        bm25=bm25,
        vector=vector,
        graph=graph,
        fusion=_build_fusion(cfg),
        boosts=select_boosts(cfg, graph),
        logger=_build_search_logger(),
        resolver=_build_collection_resolver(),
        config=cfg,
    )
