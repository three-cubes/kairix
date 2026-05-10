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
    if config is not None:
        cfg = config
    else:
        # Honour kairix.config.yaml's top-level retrieval section. Falls back
        # to RetrievalConfig.defaults() when no YAML is present (load_config's
        # own fallback). Closes #112: factory previously ignored the YAML.
        from kairix.core.search.config_loader import load_config

        cfg = load_config()

    # Intent classifier — rule-based
    from kairix.core.search.intent import classify as _classify_fn

    class _RuleClassifier:
        def classify(self, query: str) -> Any:
            return _classify_fn(query)

    # Document repository (SQLite FTS)
    from kairix.core.db import get_db_path
    from kairix.core.db.repository import SQLiteDocumentRepository

    doc_repo = SQLiteDocumentRepository(db_path=get_db_path())

    # BM25 backend
    from kairix.core.search.backends import (
        AzureEmbeddingService,
        BM25SearchBackend,
        VectorSearchBackend,
    )

    bm25 = BM25SearchBackend(doc_repo)

    # Embedding service
    embedding = AzureEmbeddingService()

    # Vector repository (usearch)
    from kairix.core.search.vector_repository import UsearchVectorRepository

    vector_repo: Any
    try:
        from kairix.core.search.vec_index import get_vector_index

        index = get_vector_index()
        if index is not None:
            vector_repo = UsearchVectorRepository(index=index)
        else:
            # Fallback: empty vector repo that returns no results
            from tests.fakes import FakeVectorRepository

            vector_repo = FakeVectorRepository()
            logger.warning("factory: usearch index not available — vector search disabled")
    except Exception as e:
        from tests.fakes import FakeVectorRepository

        vector_repo = FakeVectorRepository()
        logger.warning("factory: failed to load vector index — %s", e)

    vector = VectorSearchBackend(embedding, vector_repo)

    # Graph repository (Neo4j)
    graph: Any
    try:
        from kairix.knowledge.graph.client import get_client
        from kairix.knowledge.graph.repository import Neo4jGraphRepository

        neo4j_client = get_client()
        graph = Neo4jGraphRepository(client=neo4j_client)
    except Exception as e:
        from tests.fakes import FakeGraphRepository

        graph = FakeGraphRepository(available=False)
        logger.warning("factory: Neo4j unavailable — %s", e)

    # Fusion strategy
    from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion

    fusion: RRFFusion | BM25PrimaryFusion
    if cfg.fusion_strategy == "rrf":
        fusion = RRFFusion(k=cfg.rrf_k)
    else:
        fusion = BM25PrimaryFusion()

    # Boost chain
    boosts = select_boosts(cfg, graph)

    # Search logger — JSONL adapter writing to /data/kairix/logs/ in Docker,
    # ~/.cache/kairix/logs/ otherwise. Path resolution lives at the boundary
    # so business logic never reads env vars (G4). Query log is privacy-gated
    # via KAIRIX_LOG_QUERIES (off by default).
    import os
    from pathlib import Path

    from kairix.core.search.logger import JsonlSearchLogger, default_search_log_paths

    if Path("/.dockerenv").exists() or os.environ.get("KAIRIX_DOCKER") == "1":
        log_base = Path("/data/kairix/logs")
    else:
        log_base = Path.home() / ".cache" / "kairix" / "logs"

    search_log_path, query_log_path = default_search_log_paths(base=log_base)
    enable_query_log = os.environ.get("KAIRIX_LOG_QUERIES") == "1"
    search_logger = JsonlSearchLogger(
        search_log_path=search_log_path,
        query_log_path=query_log_path if enable_query_log else None,
    )

    # CollectionResolver + AgentRegistry — load YAML once at the boundary,
    # pass typed Adapters through. KAIRIX_EXTRA_COLLECTIONS still honoured
    # for ad-hoc deployments without a full config file.
    from kairix.core.search.config_loader import _resolve_config_path, load_collections
    from kairix.core.search.registry import parse_agent_registry
    from kairix.core.search.resolver import DefaultCollectionResolver

    collections_config = None
    agent_registry = None
    try:
        collections_config = load_collections()
    except Exception as e:
        logger.warning("factory: load_collections failed — %s", e)

    config_path = _resolve_config_path()
    if config_path is not None:
        try:
            import yaml

            with config_path.open(encoding="utf-8") as f:
                raw_yaml = yaml.safe_load(f) or {}
            pattern = collections_config.agent_pattern if collections_config else "{agent}-memory"
            agent_registry = parse_agent_registry(raw_yaml, default_pattern=pattern)
        except Exception as e:
            logger.warning("factory: parse_agent_registry failed — %s", e)

    extra_raw = os.environ.get("KAIRIX_EXTRA_COLLECTIONS", "")
    extra_collections = [c.strip() for c in extra_raw.split(",") if c.strip()]

    collection_resolver = DefaultCollectionResolver(
        collections_config=collections_config,
        extra_collections=extra_collections,
        agent_registry=agent_registry,
    )

    return SearchPipeline(
        classifier=_RuleClassifier(),
        bm25=bm25,
        vector=vector,
        graph=graph,
        fusion=fusion,
        boosts=boosts,
        logger=search_logger,
        resolver=collection_resolver,
        config=cfg,
    )
