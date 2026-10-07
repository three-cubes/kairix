"""Factory for constructing the production SearchPipeline.

Called once at startup. Resolves configuration, builds all protocol
implementations, and composes them into a SearchPipeline instance.

Tests construct SearchPipeline directly with fakes — this factory is
only for production wiring.

Process-lifetime memoisation: ``build_search_pipeline()`` caches its
result keyed by the resolved retrieval-config identity, so repeat calls
within the same process return instantly. Each rebuild costs ~2.3s +
~120 MB; memoising drops the second call to <1ms (#279).

Tests that need fresh state call ``reset_search_pipeline_cache()`` to clear
the cache between cases.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.query_cache import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_ENTRIES,
    QueryResultCache,
)
from kairix.core.search.rrf import FusedResult

if TYPE_CHECKING:
    from kairix.paths import KairixPaths
    from kairix.providers import ProviderRegistry

logger = logging.getLogger(__name__)


# Process-lifetime cache for build_search_pipeline. Key: the resolved
# RetrievalConfig (frozen dataclass → hashable by field values), so
# distinct callers passing equal config values share one pipeline.
#
# Concurrency model: ``_PIPELINE_CACHE_LOCK`` guards the build path so
# two threads landing the first MCP request after cold-start can't both
# miss + both rebuild the 2.3s pipeline. Cache reads stay lock-free —
# ``dict.get`` is GIL-atomic so the steady-state hit path pays no
# contention. The lock only serialises the rare miss path (first-call
# coordination + cache mutation under write).
_PIPELINE_CACHE: dict[RetrievalConfig, SearchPipeline] = {}
_PIPELINE_CACHE_LOCK = threading.Lock()


# Per-config QueryResultCache instances, keyed by the resolved
# RetrievalConfig's cfg_hash (#281, #554). A single shared instance
# returned config #1's cached results for every other config evaluated
# in the same process — the hybrid-sweep "identical scores for every
# config" bug (#554). Keying by cfg_hash gives each config its own cache
# (exactly one in production, N during a sweep) and matches the on-disk
# cache schema, which is already cfg_hash-scoped. Lazy-initialised so the
# env-var bounds are read once per cfg_hash at first use.
_QUERY_CACHES: dict[str, QueryResultCache] = {}
_QUERY_CACHE_LOCK = threading.Lock()


def reset_search_pipeline_cache() -> None:
    """Clear the memoised pipeline cache. Tests use this between cases.

    Also clears the process-shared query cache so cached results from a
    previous fixture-built pipeline don't bleed across tests.

    Acquires ``_PIPELINE_CACHE_LOCK`` so a concurrent first-call build
    can't interleave with a reset and re-populate the cache with the
    instance the test was about to discard.
    """
    with _PIPELINE_CACHE_LOCK:
        _PIPELINE_CACHE.clear()
    with _QUERY_CACHE_LOCK:
        for cache in _QUERY_CACHES.values():
            cache.clear()


def _lookup_cached_pipeline(cfg: RetrievalConfig, flag_reader: Any) -> SearchPipeline | None:
    """Return the cached pipeline for ``cfg`` or ``None`` if not present.

    Cache is bypassed when ``flag_reader`` is supplied — callers are
    wiring an explicit resolver branch so the cached pipeline (built
    against the production flag default) would be wrong.

    Memoisation is keyed on the resolved config alone (frozen dataclass
    → hashable by field values); the registry / paths / fact_retriever
    seams are test-only and reused per-test. Tests that need a fresh
    build with different seams call :func:`reset_search_pipeline_cache`.
    """
    if flag_reader is not None:
        return None
    return _PIPELINE_CACHE.get(cfg)


def _auto_wire_fact_retriever(db_path: Any, embedder: Any = None) -> Any:
    """Return a SQLiteFactStore when the DB has a ``facts`` table; else None.

    Plan B-parity Capability #5 — opt-in fact federation. Vault-only
    operators have no facts table → returns None → today's chunk-only
    behaviour is preserved. When the operator has run
    ``kairix ingest-chat``, the table exists and federation activates
    automatically.

    ``embedder`` is the production ``EmbeddingService`` the factory has
    already built for the chunk vector backend. Threading the SAME
    instance into the fact store lets ``FactStore.search`` embed the query
    for vector/RRF-fused recall (PLA-262, #340) — previously the auto-wired
    store got no embedder, so the fused path was dead even where the code
    existed. The fact vector INDEX is the remaining wiring (PLA-262
    follow-up); until it is injected the embedder-equipped store still runs
    BM25-only, so passing the embedder is a no-op for current deployments.

    Best-effort — any exception (missing DB, schema mismatch) returns
    None so the chunk-only path keeps shipping.
    """
    if not db_path.exists():
        return None
    try:
        import sqlite3 as _sqlite3

        from kairix.core.facts.store import SQLiteFactStore

        with _sqlite3.connect(str(db_path)) as conn:
            has_facts = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts' LIMIT 1").fetchone()
            )
        if has_facts:
            return SQLiteFactStore(db_path=db_path, embedder=embedder)
    except Exception:
        return None
    return None


def _resolve_query_cache_path() -> Any:
    """Resolve the persistent query-cache path, or ``None`` under pytest.

    Mirrors :func:`kairix.transport.cache.embed_cache._resolve_embed_cache_path`.
    F4-clean: env reads stay at the paths boundary.
    """
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:  # pragma: no cover  # F4 test-bypass: production path resolution only fires outside pytest
        from kairix.paths import query_cache_path

        return query_cache_path()
    except Exception as exc:  # pragma: no cover  # same — production-only branch
        logger.warning(
            "QueryResultCache: failed to resolve persistence path — degrading to in-memory-only. cause: %s",
            exc,
        )
        return None


def _get_or_create_query_cache(cfg_hash: str = "") -> QueryResultCache:
    """Return the :class:`QueryResultCache` for ``cfg_hash``, building it lazily.

    One cache instance per distinct ``cfg_hash`` (#554) — production runs
    a single config so it has one cache; a hybrid-sweep evaluates many
    configs in one process and each gets its own cache, so config #1's
    cached results never collide with config #2's.

    Bounds are read from env vars on first construction (#281):
      - ``KAIRIX_QUERY_CACHE_MAX_ENTRIES`` (int, default 500)
      - ``KAIRIX_QUERY_CACHE_MAX_AGE_S`` (float seconds, default 300)

    F4-clean: env reads route through :mod:`kairix.paths`.

    ``cfg_hash`` also scopes the persistent cache rows to the pipeline
    configuration (#411 Phase 2) — matching the in-memory keying above.
    """
    with _QUERY_CACHE_LOCK:
        cache = _QUERY_CACHES.get(cfg_hash)
        if cache is None:
            from kairix.paths import read_float_env, read_int_env

            max_entries = read_int_env("KAIRIX_QUERY_CACHE_MAX_ENTRIES", default=DEFAULT_MAX_ENTRIES)
            max_age_s = read_float_env("KAIRIX_QUERY_CACHE_MAX_AGE_S", default=DEFAULT_MAX_AGE_S)
            path = _resolve_query_cache_path()
            cache = QueryResultCache(
                max_entries=max_entries,
                max_age_s=max_age_s,
                path=path,
                cfg_hash=cfg_hash,
            )
            _QUERY_CACHES[cfg_hash] = cache
        return cache


def get_query_cache() -> QueryResultCache:
    """Return the query cache for the production retrieval config.

    Public accessor for the onboard check + any other diagnostic that
    wants to read :meth:`QueryResultCache.stats`. Resolves the top-level
    production ``RetrievalConfig`` and returns its cfg_hash-scoped cache —
    the same instance the production pipeline uses — so the stats reflect
    real hit rates rather than an empty default bucket (#554).
    """
    cfg = _resolve_retrieval_config(None)
    return _get_or_create_query_cache(_compute_cfg_hash(cfg))


def select_boosts(
    cfg: RetrievalConfig,
    graph: Any,
    *,
    tier_map: dict[str, str] | None = None,
    entity_first_routing_on: bool = False,
) -> list[Any]:
    """Build the production boost chain from a RetrievalConfig.

    Public helper so tests can pin which boosts the production pipeline
    actually wires for a given config — without spinning up Azure/Neo4j/SQLite.
    Each boost adapter is intent-gated internally (see kairix.core.search.boosts);
    this function only decides which adapters are *registered*, not when they
    fire.

    Args:
        cfg:      ``RetrievalConfig``. Each ``*_enabled`` flag opts the matching
                  adapter into the chain.
        graph:    ``GraphRepository`` for ``EntityBoost``. Other boosts ignore it.
        tier_map: ``dict[collection_name, tier_name]`` for
                  :class:`SourceTierBoost` (Issue #432). When ``None``
                  (the default) the boost has no per-collection tier
                  mapping — every result falls back to
                  ``cfg.source_tier_boost.default_tier`` (x1.0). The factory
                  derives this map from the operator's collections config
                  before calling this helper.
        entity_first_routing_on: when True (Issue #429 — resolved from the
                  ``entity_first_routing_enabled`` feature flag by
                  :func:`build_search_pipeline`), append
                  :class:`EntityFirstRoutingBoost` LAST so its multiplier
                  composes on top of the tier de-boost. Default False keeps
                  the chain byte-for-byte identical for every deployment
                  that hasn't cut over.

    Returns:
        List of boost-strategy instances in registration order:
        EntityBoost → ProceduralBoost → TemporalDateBoost → ChunkDateBoost
        → SourceTierBoost (Issue #432 — runs last so tier multipliers
        apply on top of any intent-gated boost output) → ContentQualityBoost
        → EntityFirstRoutingBoost (Issue #429 — appended only when the flag
        is on; runs last so entity-summaries are routed first on ENTITY
        intent after every other boost has scored).
    """
    from kairix.core.search.boosts import (
        ChunkDateBoost,
        ContentQualityBoost,
        EntityBoost,
        EntityFirstRoutingBoost,
        ProceduralBoost,
        SourceTierBoost,
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
    # Issue #432 — source-tier-aware ranking. Registered before
    # ContentQualityBoost so the final ranking is
    # ``tier_multiplier x content_quality_multiplier`` (orthogonal
    # composition). No-op when cfg.source_tier_boost.enabled is False
    # (the default), preserving pre-#432 ranking byte-for-byte.
    if cfg.source_tier_boost.enabled:
        boosts.append(SourceTierBoost(tier_map=tier_map, config=cfg.source_tier_boost))
    # Issue #458 — content-quality (enrichment-derived) authority.
    # Composes multiplicatively with #432's operator-declared tier
    # so canonical-tier content with a stub body doesn't beat
    # reference-tier content with a substantive body.
    if cfg.content_quality_boost.enabled:
        boosts.append(ContentQualityBoost(config=cfg.content_quality_boost))
    # Issue #429 — entity-first routing. Appended LAST so the multiplier
    # composes on top of every prior boost (especially SourceTierBoost's
    # reference x0.6 de-boost). Wired only when the
    # ``entity_first_routing_enabled`` flag is on (resolved by
    # build_search_pipeline); the boost ALSO self-gates on the flag at
    # query time so an operator can roll the cutover back instantly
    # without a rebuild.
    if entity_first_routing_on:
        boosts.append(EntityFirstRoutingBoost(config=cfg.entity_first_routing))
    return boosts


def derive_tier_map(mapping: dict[str, Any] | None = None) -> dict[str, str]:
    """Derive ``{collection_name: tier_name}`` from the canonical
    topology collections (Issue #432, canonical-collapse Task 6).

    Reads ``topology.collections[*].tier`` from the operator's MERGED
    config mapping (base + overlay) via
    :func:`kairix.config_layers.load_merged_mapping` — the same
    overlay-aware read path the setup wizard writes through (#492), so a
    wizard-saved topology block is honoured. Returns an empty dict when
    no topology collections are present, when no collection declares a
    ``tier:`` field, or when the parse fails — in all cases the
    :class:`SourceTierBoost` falls back to its default tier (x1.0)
    and preserves pre-#432 ranking byte-for-byte.

    DB-free: this is a config-parse, not a read of the materialised
    ``topology_collections`` rows, so it stays safe to call at
    pipeline-construction time before any DB is open. Pass ``mapping=``
    to inject an explicit merged mapping (mirrors the
    ``_resolve_retrieval_config(config=...)`` seam); the default resolves
    the live merged config.
    """
    try:
        if mapping is None:
            from kairix.config_layers import load_merged_mapping

            mapping = load_merged_mapping()
        from kairix.config.topology import parse_topology

        topology = parse_topology(mapping)
    except Exception:
        return {}
    return {c.name: c.tier for c in topology.collections if c.tier}


def derive_collection_overrides(mapping: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Derive ``{collection_name: retrieval_override_dict}`` from the
    canonical topology collections (canonical-collapse).

    Reads ``topology.collections[*].retrieval`` from the operator's
    MERGED config mapping (base + overlay) via
    :func:`kairix.config_layers.load_merged_mapping` — the same
    overlay-aware read path the setup wizard writes through (#492), so a
    wizard-saved topology block is honoured. Returns an empty dict when no
    topology collections are present, when no collection declares a
    ``retrieval:`` block, or when the parse fails — in all cases
    :func:`kairix.core.search.config_loader.resolve_retrieval_config`
    falls back to the global retrieval config for the collection.

    DB-free: this is a config-parse, not a read of the materialised
    ``topology_collections`` rows, so it stays safe to call at
    pipeline-construction time before any DB is open. Pass ``mapping=`` to
    inject an explicit merged mapping (mirrors the
    ``_resolve_retrieval_config(config=...)`` seam); the default resolves
    the live merged config. Each override is materialised as a plain
    ``dict`` (the parser stores it as a read-only ``MappingProxyType``)
    so the consumer can splat it into ``merge_retrieval_config``.
    """
    try:
        if mapping is None:
            from kairix.config_layers import load_merged_mapping

            mapping = load_merged_mapping()
        from kairix.config.topology import parse_topology

        topology = parse_topology(mapping)
    except Exception:
        return {}
    return {c.name: dict(c.retrieval_overrides) for c in topology.collections if c.retrieval_overrides}


def _resolve_retrieval_config(config: RetrievalConfig | None) -> RetrievalConfig:
    """Pick the explicit config or fall back to ``load_config`` (which itself
    falls back to ``RetrievalConfig.defaults()`` when no YAML is present).
    """
    if config is not None:
        return config
    from kairix.core.search.config_loader import load_config

    return load_config()


class _NullVectorRepository:
    """No-op VectorRepository fallback for degraded deployments.

    Production fallback when the usearch index is missing or fails to load.
    Returns empty result sets so the rest of the SearchPipeline can continue
    in BM25-only mode. Defined inline (not in ``tests/fakes.py``) because
    production code must never import from ``tests/`` — the directory is
    not shipped in the installable wheel.
    """

    def search(
        self,
        query_vec: list[float],
        k: int,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # Reference args so F19 (unused-params-named) sees them as used.
        # Names are fixed by the production caller (backends.py:94 uses
        # ``k=`` and ``collections=`` keyword arguments).
        _ = (query_vec, k, collections)
        return []

    def add_vectors(self, items: list[tuple[str, list[float]]]) -> int:
        _ = items
        return 0

    def count(self) -> int:
        return 0


class _NullGraphRepository:
    """No-op GraphRepository fallback for degraded deployments.

    Production fallback when Neo4j is unreachable or its driver fails.
    Reports ``available=False`` so entity-boost callers route around it.
    Defined inline for the same reason as :class:`_NullVectorRepository`.
    """

    @property
    def available(self) -> bool:
        return False

    def find_entity(self, name: str) -> dict[str, Any] | None:
        _ = name
        return None

    def entity_in_degrees(self) -> list[dict[str, Any]]:
        return []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        _ = (query, params)
        return []


def _default_vec_index_factory() -> Any:
    """Production default for ``FactoryDeps.vec_index_factory``."""
    from kairix.core.search.vec_index import get_vector_index

    return get_vector_index()


def _default_graph_client_factory() -> Any:
    """Production default for ``FactoryDeps.graph_client_factory``."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _default_bootstrap_secrets(*args: Any, **kwargs: Any) -> Any:
    """Production default for ``FactoryDeps.bootstrap_secrets_fn``."""
    from kairix.secrets.bootstrap import bootstrap_secrets

    return bootstrap_secrets(*args, **kwargs)


@dataclass
class FactoryDeps:
    """Injectable dependencies for the factory entry points.

    Replaces the F6-violating per-kwarg test seams on
    ``build_search_pipeline`` / ``build_connector_pipeline`` /
    ``build_neo4j_drainer``. Production code calls the entry points
    without ``deps`` and the dataclass's ``default_factory`` wires the
    real callables. Tests construct
    ``FactoryDeps(vec_index_factory=lambda: _StandInIndex())`` (etc.)
    and pass it through.

    Each callable field is non-Optional with a ``default_factory`` (per
    CLAUDE.md F6 guidance — same shape as ``WorkerDeps``) so mypy sees
    the production callable directly. The fields:

    - ``vec_index_factory`` returns the usearch vector-index handle (or
      ``None`` when the index is not available); raising falls back to a
      null vector repo.
    - ``graph_client_factory`` returns the Neo4j client; raising falls
      back to a null graph repo.
    - ``bootstrap_secrets_fn`` runs the secrets-bundle hydration once
      before any credential resolution; raising is logged and swallowed
      so a missing bundle in local dev doesn't crash the factory.

    F47 paydown — pipeline-component overrides:
    Integration tests that previously constructed ``SearchPipeline(...)``
    directly with canonical fakes now thread their fakes via the
    ``*_override`` fields below. ``None`` means "build the production
    default"; any non-``None`` value short-circuits the corresponding
    production wiring and uses the override instead. Setting any
    override flips :func:`_is_default_deps` to False, which bypasses the
    process-shared pipeline cache so tests get a fresh wiring per call.

    - ``classifier_override``: replace the rule-based intent classifier
      (typically with ``FakeClassifier(intent=...)`` or
      ``RealClassifierAdapter()``).
    - ``doc_repo_override``: replace the SQLite-backed
      ``DocumentRepository`` (typically with ``FakeDocumentRepository``).
      Wraps in :class:`BM25SearchBackend` automatically.
    - ``vec_repo_override``: replace the usearch-backed
      ``VectorRepository`` (typically with ``FakeVectorRepository``).
      Wraps in :class:`VectorSearchBackend` with the embed service.
    - ``embed_service_override``: replace the
      :class:`ProviderEmbeddingService` (typically with
      ``FakeEmbeddingService``). When set, ``cfg.provider`` is not
      consulted and ``registry`` is ignored — the override is used
      verbatim.
    - ``graph_override``: replace the ``GraphRepository`` (typically
      with ``FakeGraphRepository(available=...)``). When set, the
      ``graph_client_factory`` is not called.
    - ``fusion_override``: replace the fusion strategy (typically with
      ``RRFFusion(k=60)`` or ``FakeFusion()``); when set, ``cfg.fusion_strategy``
      is ignored.
    - ``boosts_override``: replace the boost chain. ``None`` builds the
      production chain from ``cfg``; any list (including ``[]``) is
      used verbatim.
    - ``logger_override``: replace the JSONL search logger (typically
      with ``FakeSearchLogger`` or a tmp-path :class:`JsonlSearchLogger`).
    - ``resolver_override``: replace the topology collection
      resolver. When set, no SQLite Connection is opened for resolution.
    - ``query_cache_override``: replace (or disable) the
      process-shared :class:`QueryResultCache`. Use the sentinel
      :data:`QUERY_CACHE_DISABLED` to wire ``query_cache=None`` on the
      pipeline; pass an explicit ``QueryResultCache`` instance to wire
      a specific cache.
    - ``reranker_override``: replace the cross-encoder rerank closure.
      ``None`` builds the production default (the
      ``sentence-transformers`` cross-encoder closure, gated per-call by
      ``pipeline._maybe_rerank``). Use the sentinel
      :data:`RERANK_DISABLED` to wire ``reranker=None`` (the rerank stage
      becomes a structural no-op — ``_maybe_rerank`` short-circuits on
      ``reranker is None``). Pass any ``Callable[[str, list[FusedResult]],
      list[FusedResult]]`` to inject a no-op / fake reranker that skips
      the ~5s torch import in integration tests whose assertions never
      read the reranked order.
    """

    vec_index_factory: Callable[[], Any] = field(default_factory=lambda: _default_vec_index_factory)
    graph_client_factory: Callable[[], Any] = field(default_factory=lambda: _default_graph_client_factory)
    bootstrap_secrets_fn: Callable[..., Any] = field(default_factory=lambda: _default_bootstrap_secrets)

    # F47 paydown — pipeline-component overrides. ``None`` means "use the
    # production default". Production callers never set these; tests
    # thread Fake* implementations from ``tests/fakes.py`` through them.
    classifier_override: Any = None
    doc_repo_override: Any = None
    vec_repo_override: Any = None
    embed_service_override: Any = None
    graph_override: Any = None
    fusion_override: Any = None
    boosts_override: Any = None  # list[BoostStrategy] | None
    logger_override: Any = None
    resolver_override: Any = None
    query_cache_override: Any = None  # QueryResultCache | QUERY_CACHE_DISABLED | None
    reranker_override: Any = None  # reranker closure | RERANK_DISABLED | None
    # PLA-270 — tiered-context summary loader. ``None`` keeps Phase-1
    # behaviour (every hit served as the full L2 snippet — default-safe).
    # Integration tests inject ``FakeSummaryLoader`` here to exercise the
    # composed L0/L1 tiering + ``max_tier`` ceiling through the factory;
    # production stays None until summaries are generated + activated.
    summary_loader_override: Any = None  # SummaryLoader | None
    # ADR-020 — connector tick budget + disk-watermark gate. ``None`` keeps
    # the ConnectorPipeline production default (queries ``/data``; falls
    # back to ``sys.maxsize`` when ``/data`` isn't mounted). Operators on
    # non-/data layouts and integration / BDD tests pass a
    # ``Callable[[], int]`` free-bytes probe through
    # :func:`build_connector_pipeline`. Connector-pipeline only — does not
    # affect the search-pipeline cache key.
    disk_free_override: Any = None  # Callable[[], int] | None


class _QueryCacheDisabledSentinel:
    """Sentinel for ``FactoryDeps.query_cache_override`` meaning ``query_cache=None``.

    A bare ``None`` would be indistinguishable from "use the production
    default cache" — this sentinel disambiguates "wire no cache at all"
    (e.g. tests asserting backend-call counts without dedup).
    """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "QUERY_CACHE_DISABLED"


QUERY_CACHE_DISABLED = _QueryCacheDisabledSentinel()


class _RerankDisabledSentinel:
    """Sentinel for ``FactoryDeps.reranker_override`` meaning ``reranker=None``.

    A bare ``None`` is indistinguishable from "use the production default
    cross-encoder closure" — this sentinel disambiguates "wire no reranker
    at all" so the pipeline's ``_maybe_rerank`` short-circuits without
    importing ``sentence-transformers`` / torch. Used by integration tests
    that assert pre-rerank pipeline behaviour and never read the reranked
    order.
    """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "RERANK_DISABLED"


RERANK_DISABLED = _RerankDisabledSentinel()


def _build_vector_repo(vec_index_factory: Callable[[], Any]) -> Any:
    """Construct the usearch vector repo, falling back to a null repo on failure."""
    from kairix.core.search.vector_repository import UsearchVectorRepository

    try:
        index = vec_index_factory()
        if index is not None:
            return UsearchVectorRepository(index=index)
        logger.warning("factory: usearch index not available — vector search disabled")
    except Exception as e:
        logger.warning("factory: failed to load vector index — %s", e)
    return _NullVectorRepository()


def _build_graph(graph_client_factory: Callable[[], Any]) -> Any:
    """Construct the Neo4j graph repo, falling back to a null repo on failure."""
    try:
        from kairix.knowledge.graph.repository import Neo4jGraphRepository

        return Neo4jGraphRepository(client=graph_client_factory())
    except Exception as e:
        logger.warning("factory: Neo4j unavailable — %s", e)
        return _NullGraphRepository()


def _build_fusion(cfg: RetrievalConfig) -> Any:
    """Pick the fusion strategy by config name."""
    from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion

    if cfg.fusion_strategy == "rrf":
        return RRFFusion(k=cfg.rrf_k)
    return BM25PrimaryFusion()


def _build_search_logger(env: Mapping[str, str] | None = None) -> Any:
    """Construct the JSONL search logger, honouring docker-vs-host log paths.

    Path resolution lives at the boundary so business logic never reads env vars
    (G4). Query log is privacy-gated via KAIRIX_LOG_QUERIES (off by default).
    Env reads route through kairix.paths (F4).

    Args:
        env: Optional env mapping (F2-clean test seam). Threaded into
            :func:`kairix.paths.is_docker_env` and
            :func:`kairix.paths.log_queries_enabled`. Production callers
            leave this ``None``.
    """
    from kairix.core.search.logger import JsonlSearchLogger, default_search_log_paths
    from kairix.paths import log_dir, log_queries_enabled

    # log_dir() honours the full resolution chain: KAIRIX_LOG_DIR env, then
    # config file, then docker/server/XDG defaults. Replaces the previous
    # docker-only Path("/data/kairix/logs") hardcode that ignored
    # operator overrides — see #447.
    search_log_path, query_log_path = default_search_log_paths(base=log_dir())
    enable_query_log = log_queries_enabled(env)
    return JsonlSearchLogger(
        search_log_path=search_log_path,
        query_log_path=query_log_path if enable_query_log else None,
    )


def build_collection_resolver(
    db_path: Any = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Construct the production ``CollectionResolver``.

    Always returns :class:`TopologyCollectionResolver`, which reads
    the ``topology_scope_profiles`` + ``topology_scope_entries`` v2
    tables; default search returns the superset of every collection
    the agent's scope_profile grants read access to, filtered by the
    per-scope-entry ``default_in_scope`` column.

    ``topology_collection_resolver`` + ``topology_default_in_scope``
    retired post-cutover (task #132).

    ``db_path`` is the resolved SQLite path threaded from
    :func:`build_search_pipeline`; the resolver opens a connection
    against it.

    ``env`` is an optional F2-clean test seam threaded into
    :func:`kairix.paths.extra_collections` so callers can drive the
    ``KAIRIX_EXTRA_COLLECTIONS`` parsing without mutating ``os.environ``.
    """
    return _build_topology_collection_resolver(
        db_path,
        default_in_scope_filter_enabled=True,
        env=env,
    )


class _SerializingSqliteConnection:
    """Thread-serialising proxy around a shared :class:`sqlite3.Connection`.

    Production wiring for the topology collection resolver opens one
    Connection (with ``check_same_thread=False``) and reuses it for the
    process lifetime — see :func:`_build_topology_collection_resolver`.
    ``check_same_thread=False`` lets multiple threads call into the
    connection, but the Python sqlite3 driver still uses a single
    underlying cursor state per connection: if thread A's
    ``conn.execute(...)`` is mid-fetch when thread B calls
    ``conn.execute(...)``, the cursor's internal pointer is clobbered
    and the second call raises ``sqlite3.InterfaceError: bad parameter
    or other API misuse``.

    The ScopeCollectionCache deliberately drops its lock around the
    inner ``resolve()`` call so an in-flight SELECT doesn't block cache
    reads on other ``(agent, scope)`` keys — which means concurrent
    cache-miss callers reach the shared connection simultaneously. This
    proxy puts the missing serialisation back at the connection
    boundary: every ``execute(...)`` runs under one shared lock. The
    SELECTs are tiny (≤10ms each), so single-lock serialisation costs
    sub-millisecond contention even at conc=10.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def execute(self, sql: str, params: Any = ()) -> Any:
        """Serialised ``Connection.execute`` — materialises rows under the lock.

        The resolver call sites uniformly read every row from the
        returned cursor in one shot, so we eagerly materialise here
        while the lock is held and return a pre-fetched stand-in. This
        keeps the lock-hold time bounded to the SQL roundtrip rather
        than spanning a lazy iteration that downstream code might fan
        out before completing. The stand-in supports the same row-set
        consumption shape sqlite3 would have yielded.
        """
        with self._lock:
            cursor = self._conn.execute(sql, params)
            # F63-bounded: this proxy is wired only to the topology collection
            # resolver Connection (_build_topology_collection_resolver). Every
            # call site there issues per-actor or operator-config-sized SELECTs
            # against topology_scope_entries / topology_collections /
            # topology_cc_pairs — bounded by the actor's profile (≤O(collections),
            # typically ≤100) and by operator-config size (≤O(100) public collections).
            rows = cursor.fetchall()
        return _MaterialisedCursor(rows)

    def close(self) -> None:
        """Close the wrapped Connection under the serialisation lock.

        Avoids closing a Connection while another thread is mid-execute,
        which would surface as the same InterfaceError race.
        """
        with self._lock:
            self._conn.close()


class _MaterialisedCursor:
    """Lightweight stand-in for a sqlite3 cursor that has already fetched.

    The topology + scope-profile resolver call sites only consume
    rows by eager batch read (or by direct iteration in a single
    comprehension). This stand-in supports both shapes without holding
    any database state, so the serialising connection above can release
    its lock before the caller iterates.
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


def _build_topology_collection_resolver(
    db_path: Any,
    *,
    default_in_scope_filter_enabled: bool = False,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Construct :class:`TopologyCollectionResolver` against ``db_path``.

    Opens a sqlite3 Connection so the resolver can query
    ``topology_scope_profiles`` + ``topology_scope_entries`` +
    ``topology_collections`` + ``topology_cc_pairs``. The connection
    stays open for the lifetime of the cached pipeline (matches the
    SearchPipeline's lifecycle).

    ``default_in_scope_filter_enabled`` (GH #373) gates the new
    default-in-scope filter on the collections=None path. The factory
    reads the ``topology_default_in_scope`` feature flag and threads
    its value here.

    Reads ``KAIRIX_EXTRA_COLLECTIONS`` at the factory boundary (F4)
    and passes the parsed list into the resolver — preserves the
    operator-facing env-var contract that the retired legacy resolver
    used to honour.
    """
    import sqlite3

    from kairix.core.search.scope_collection_cache import ScopeCollectionCache
    from kairix.core.search.topology_resolver import TopologyCollectionResolver
    from kairix.paths import extra_collections as _extra_collections

    # ``check_same_thread=False`` is mandatory here: build_search_pipeline
    # memoises its result for the process lifetime, so the same Connection
    # gets reused across every uvicorn worker thread that lands an MCP
    # search request. Without this flag, every request that lands on a
    # thread other than the one that warmed the pipeline raises
    # ``sqlite3.ProgrammingError`` and the search returns no hits. SQLite
    # serialises internal access; the resolver only issues SELECTs so the
    # serialisation cost is negligible.
    raw_db = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
    # InterfaceError fix (#399 Workstream E): ``check_same_thread=False``
    # alone is not sufficient — it disables Python's thread-affinity check
    # but the sqlite3 driver still serialises through a single cursor
    # state per Connection. Concurrent ``conn.execute(...)`` calls from
    # MCP worker threads (uncovered by ScopeCollectionCache because it
    # drops its lock around the inner resolver call) interleave and
    # surface as ``sqlite3.InterfaceError: bad parameter or other API
    # misuse``. The proxy puts the missing serialisation back at the
    # connection boundary so the resolver call sites need no change.
    db = _SerializingSqliteConnection(raw_db)
    # Rationale for the type: ignore below: the proxy exposes the
    # execute()+close() subset the resolver actually uses; widening the
    # resolver signature to Any would weaken the type contract for
    # every legitimate sqlite3.Connection caller, so we deliberately
    # bypass the structural-mismatch check at this one call site.
    inner = TopologyCollectionResolver(
        db=db,  # type: ignore[arg-type]  # see rationale above
        default_in_scope_filter_enabled=default_in_scope_filter_enabled,
        extra_collections=_extra_collections(env),
    )
    # R2 (#388) — wrap in a TTL cache so the SQLite SELECT on
    # topology_scope_profiles + topology_scope_entries doesn't run on
    # every search request. Scope-profile changes are minute-scale
    # operator actions, not query-scale events; the 10-minute default
    # TTL is a deliberate consistency tradeoff documented on the cache.
    return ScopeCollectionCache(inner)


def _resolve_provider_name(cfg: RetrievalConfig) -> str | None:
    """Return the configured provider plugin name or ``None``.

    Resolution order:

      1. ``cfg.provider`` — the value threaded through ``RetrievalConfig``
         (SWAP v2 added this field; ``load_config`` parses ``provider:``
         from ``kairix.config.yaml`` into it).
      2. :func:`kairix.paths.provider_name` fallback — re-reads the YAML
         directly. Covers ad-hoc test paths that construct
         ``RetrievalConfig`` instances by hand without going through
         ``load_config`` (so ``cfg.provider`` is ``None``) but still
         have a ``kairix.config.yaml`` on disk that names a plugin.
      3. ``None`` — no provider configured. The caller raises a typed
         ``ValueError`` so operators see a misconfiguration immediately
         rather than silently degrading to a legacy code path.
    """
    if cfg.provider:
        return cfg.provider
    from kairix.paths import provider_name

    return provider_name()


def _build_embedding_service(
    cfg: RetrievalConfig,
    registry: ProviderRegistry | None = None,
) -> Any:
    """Construct the production ``EmbeddingService`` for the pipeline.

    Plugin-driven path (v2026.5.17 onward): resolves the configured
    provider via :func:`kairix.providers.get_provider` and wraps the
    plugin in
    :class:`kairix.transport.embed_service.ProviderEmbeddingService` —
    the Protocol-shaped adapter that owns the cache + coalescer
    routing.

    When no provider is configured (no ``provider:`` in YAML, no
    ``cfg.provider``), raises a typed ``ValueError`` listing the
    installed plugins. Operators see the misconfiguration at
    pipeline-build time rather than discovering an inert embed surface
    at query time.

    F26 carve-out: this is the one core/ file allowed to import
    ``kairix.transport.embed_service`` and ``kairix.providers``. The
    factory is the wiring point named in
    ``docs/architecture/provider-plugin-architecture.md``; the
    domain Protocols still live in ``kairix.core.protocols``.
    See ``.architecture/baseline/f26-files.txt`` for the grandfathered
    entry covering this file.

    Args:
        cfg:       resolved ``RetrievalConfig`` carrying the
                   configured provider name (if any).
        registry:  optional ``ProviderRegistry`` for tests; production
                   resolves via the default ``EntryPointRegistry``.

    Returns:
        An object satisfying the ``EmbeddingService`` Protocol.
    """
    from kairix.providers import EntryPointRegistry, get_provider
    from kairix.transport.embed_service import ProviderEmbeddingService

    name = _resolve_provider_name(cfg)
    if name is None:
        available_registry = registry if registry is not None else EntryPointRegistry()
        try:
            available = sorted(available_registry.available())
        except Exception:  # pragma: no cover - registry pathologies surface via the message below
            available = []
        installed = ", ".join(available) if available else "<none>"
        raise ValueError(
            "kairix.config.yaml is missing the required 'provider:' field. "
            "fix: add 'provider: <name>' to kairix.config.yaml. "
            "run: kairix probe-config to see installed plugins. "
            f"installed plugins: {installed}."
        )

    provider = get_provider(name, registry=registry)
    return ProviderEmbeddingService(provider)


def build_embedding_service(
    config: RetrievalConfig | None = None,
    *,
    registry: ProviderRegistry | None = None,
) -> Any:
    """Public seam: construct the production ``EmbeddingService``.

    Resolves ``config`` (``None`` → the layered ``RetrievalConfig`` via
    :func:`load_config`, falling back to ``RetrievalConfig.defaults()``) and
    returns an object satisfying the ``EmbeddingService`` Protocol. This is the
    public entry point for non-``core`` callers that need a bare embedding
    service (e.g. the capability-corpus builder) — they embed through this
    rather than reaching into the private :func:`_build_embedding_service`.

    Args:
        config:   Explicit retrieval config, or ``None`` to resolve it from
                  ``kairix.config.yaml`` (same resolution as
                  :func:`build_search_pipeline`).
        registry: Optional ``ProviderRegistry`` for tests — pass a
                  ``FakeProviderRegistry``; production resolves via the default
                  ``EntryPointRegistry``.

    Returns:
        An object satisfying the ``EmbeddingService`` Protocol. Raises
        ``ValueError`` when no provider is configured.
    """
    return _build_embedding_service(_resolve_retrieval_config(config), registry)


def _run_bootstrap_secrets(deps: FactoryDeps, *, caller: str) -> None:
    """Run the secrets bootstrap once, swallowing any exception.

    Args:
        deps: ``FactoryDeps`` whose ``bootstrap_secrets_fn`` is invoked.
        caller: Name of the factory entry point — used in the debug log
            message so soft-failures are attributable.
    """
    try:
        deps.bootstrap_secrets_fn()
    except Exception as exc:
        # Best-effort: a missing/unreadable bundle isn't fatal — the loader
        # falls back to env-only resolution. Production deploys always have
        # the bundle; local dev may not.
        logger.debug("factory: bootstrap_secrets soft-failed in %s: %s", caller, exc)


def build_search_pipeline(
    config: RetrievalConfig | None = None,
    *,
    registry: ProviderRegistry | None = None,
    fact_retriever: Any = None,
    paths: KairixPaths | None = None,
    flag_reader: Any = None,
    deps: FactoryDeps | None = None,
    env: Mapping[str, str] | None = None,
) -> SearchPipeline:
    """Construct the production search pipeline.

    Memoised per-config for the process lifetime. The first call pays the
    factory cost (~2.3s, ~120 MB); subsequent calls with the same config
    *value* return the cached instance instantly. Tests that need fresh state
    call ``reset_search_pipeline_cache()``.

    Cache key is the resolved RetrievalConfig itself (frozen dataclass →
    hashable by field values), not Python object identity. Two callers
    passing freshly-constructed RetrievalConfig instances with the same
    field values share one cached pipeline — the case the benchmark path
    hits when ``_retrieve_hybrid`` constructs a new config object per case.

    Resolves all dependencies from the environment (DB paths, configured
    provider plugin, Neo4j connection, usearch index). Each dependency is
    imported lazily to avoid hard dependency at module load.

    Args:
        config:    Explicit retrieval config. When ``None``, the factory
                   loads the top-level ``retrieval:`` section from
                   ``kairix.config.yaml`` via :func:`load_config`. If no
                   YAML is present, falls back to
                   ``RetrievalConfig.defaults()``.
        registry:  Optional ``ProviderRegistry`` for tests — pass a
                   ``FakeProviderRegistry`` from ``tests/fakes.py`` to
                   resolve plugin names against an in-memory mapping.
                   Production passes ``None``; the default
                   ``EntryPointRegistry`` is constructed inside
                   :func:`kairix.providers.get_provider`.
        fact_retriever:
                   Optional :class:`kairix.core.protocols.FactStore` for
                   Plan B-parity Capability #5 federation. When ``None``
                   the pipeline runs today's chunk-only behaviour
                   (regression-pinned). Callers wiring the fact layer
                   (ingest-chat → SQLiteFactStore) pass the same store
                   instance here so the SearchPipeline can federate
                   retrieval across chunks + facts.
        paths:     Optional :class:`kairix.paths.KairixPaths` for E2E /
                   integration tests that need to construct the pipeline
                   against a tmp-path SQLite database without setting
                   ``KAIRIX_DB_PATH`` (which F2 prohibits in tests). When
                   provided, ``paths.db_path`` is used in place of
                   :func:`kairix.core.db.get_db_path`. Production passes
                   ``None`` and the default resolution chain runs.
        flag_reader:
                   Optional ``Callable[[str], bool]`` for tests to drive
                   the feature-flag branch deterministically. Threaded
                   straight to :func:`build_collection_resolver` so the
                   v2 vs legacy resolver choice is reproducible without
                   monkey-patching the registry (F2-clean). Production
                   passes ``None``; the default flag resolver
                   (:func:`kairix.core.features.flag`) is consulted.

    Returns:
        A fully wired SearchPipeline ready for search() calls.
    """
    # Resolve deps once — production callers pass None and the default
    # factories wire the real implementations. Tests pass a FactoryDeps
    # with Fake* callables.
    deps = deps if deps is not None else FactoryDeps()

    # Auto-hydrate secrets from the bundle file before any provider/credential
    # resolution. Idempotent (bootstrap_secrets has its own once-guard). CLI
    # and MCP entry points already call this at startup; Python-API consumers
    # (eval harnesses, probe runners, notebooks, ad-hoc scripts) used to fail
    # with SecretNotFoundError because secrets were only on disk in
    # /run/secrets/kairix.env not in env. Centralising here means every
    # consumer of build_search_pipeline gets the same hydration contract.
    _run_bootstrap_secrets(deps, caller="build_search_pipeline")

    cfg = _resolve_retrieval_config(config)

    # When ``flag_reader`` is supplied, callers are wiring an explicit
    # resolver branch — skip the cache so the chosen branch is honoured
    # per call. Non-default ``deps`` (test stand-ins for vec_index /
    # graph_client / bootstrap) also bypass the cache; the cache key is
    # the resolved RetrievalConfig only, so tests asserting wiring
    # expect a fresh build each call. An explicit ``env`` likewise
    # bypasses the cache so KAIRIX_DOCKER / KAIRIX_EXTRA_COLLECTIONS
    # branches are exercised cleanly.
    bypass_cache = flag_reader is not None or not _is_default_deps(deps) or env is not None
    cached = _lookup_cached_pipeline(cfg, flag_reader) if not bypass_cache else None
    if cached is not None:
        return cached

    # Double-checked locking for the build path. The lock-free read above
    # is the fast path (every steady-state call hits a populated cache).
    # When the cache misses, threads queue at the lock so only the first
    # arrival pays the 2.3s pipeline construction; subsequent arrivals
    # re-check inside the lock and observe the freshly-cached pipeline.
    if not bypass_cache:
        with _PIPELINE_CACHE_LOCK:
            cached = _PIPELINE_CACHE.get(cfg)
            if cached is not None:
                return cached
            built = _build_search_pipeline_uncached(cfg, registry, fact_retriever, paths, deps, env)
            _PIPELINE_CACHE[cfg] = built
            return built

    return _build_search_pipeline_uncached(cfg, registry, fact_retriever, paths, deps, env)


def _is_default_deps(deps: FactoryDeps) -> bool:
    """Return True iff ``deps`` is structurally the production default.

    The dataclass holds ``default_factory``-bound callables that differ
    in identity per ``FactoryDeps()`` instance; equality of the three
    factory references against the production defaults is the
    structural test. Production callers omit ``deps`` and the entry
    point constructs ``FactoryDeps()`` — :func:`build_search_pipeline`
    then treats it as "default" and honours the process-shared pipeline
    cache.

    Any non-``None`` pipeline-component override (classifier, doc_repo,
    vec_repo, embed_service, graph, fusion, boosts, logger, resolver,
    query_cache, reranker) also flips this to ``False`` so tests get a
    fresh pipeline per call — the cache key is the resolved
    RetrievalConfig alone, so caching a fake-wired pipeline would leak
    across cases.
    """
    overrides_clean = (
        deps.classifier_override is None
        and deps.doc_repo_override is None
        and deps.vec_repo_override is None
        and deps.embed_service_override is None
        and deps.graph_override is None
        and deps.fusion_override is None
        and deps.boosts_override is None
        and deps.logger_override is None
        and deps.resolver_override is None
        and deps.query_cache_override is None
        and deps.reranker_override is None
        and deps.summary_loader_override is None
    )
    return (
        deps.vec_index_factory is _default_vec_index_factory
        and deps.graph_client_factory is _default_graph_client_factory
        and deps.bootstrap_secrets_fn is _default_bootstrap_secrets
        and overrides_clean
    )


def _build_search_pipeline_uncached(
    cfg: RetrievalConfig,
    registry: ProviderRegistry | None,
    fact_retriever: Any,
    paths: KairixPaths | None,
    deps: FactoryDeps,
    env: Mapping[str, str] | None = None,
) -> SearchPipeline:
    """Build a fresh ``SearchPipeline`` for ``cfg`` — never reads the cache.

    Extracted from :func:`build_search_pipeline` so the cache miss path
    runs the construction sequence under the cache lock without
    duplicating the wiring. Callers that have already verified the cache
    is cold (e.g. inside the double-checked-locking critical section)
    invoke this directly.

    F47 paydown — each pipeline component (classifier, doc_repo,
    vec_repo, embed_service, graph, fusion, boosts, logger, resolver,
    query_cache) honours the corresponding ``*_override`` on ``deps``
    when set. Production callers leave them ``None`` and the production
    defaults below fire; integration tests pass Fake* instances so they
    can construct the pipeline through this factory rather than via
    direct ``SearchPipeline(...)`` construction.
    """
    from kairix.core.search.intent import classify as _classify_fn

    class _RuleClassifier:
        def classify(self, query: str) -> Any:
            return _classify_fn(query)

    from kairix.core.search.backends import (
        BM25SearchBackend,
        VectorSearchBackend,
    )

    # Explicit ``paths`` (test DI seam) wins over the env-driven default
    # (F2: keeps tmp_path injection out of monkeypatch.setenv). Production
    # passes ``paths=None`` and the existing resolution chain runs.
    resolved_db_path = _resolve_db_path(paths)

    # Components: each starts at the override (when set) and falls back
    # to the production wiring otherwise. Production callers leave every
    # override ``None``.
    classifier = deps.classifier_override if deps.classifier_override is not None else _RuleClassifier()

    doc_repo = deps.doc_repo_override if deps.doc_repo_override is not None else _default_doc_repo(resolved_db_path)
    bm25 = BM25SearchBackend(doc_repo)

    embed_service = (
        deps.embed_service_override
        if deps.embed_service_override is not None
        else _build_embedding_service(cfg, registry=registry)
    )
    vec_repo = (
        deps.vec_repo_override if deps.vec_repo_override is not None else _build_vector_repo(deps.vec_index_factory)
    )
    vector = VectorSearchBackend(embed_service, vec_repo)

    graph = deps.graph_override if deps.graph_override is not None else _build_graph(deps.graph_client_factory)

    fusion = deps.fusion_override if deps.fusion_override is not None else _build_fusion(cfg)

    # Issue #432 — derive {collection_name: tier_name} from the
    # operator's kairix.config.yaml so SourceTierBoost knows which tier
    # to apply per result. Empty dict (no tier declarations) means the
    # boost falls back to the default tier (x1.0) for every result.
    tier_map = derive_tier_map() if cfg.source_tier_boost.enabled else None
    # Issue #429 — resolve the entity-first-routing cutover flag once at
    # build time. When ON, select_boosts appends EntityFirstRoutingBoost so
    # ENTITY-intent queries route entity-summaries first. Default OFF keeps
    # the production chain unchanged.
    from kairix.core.search.boosts import default_entity_first_routing_flag_reader

    entity_first_routing_on = default_entity_first_routing_flag_reader()
    boosts = (
        deps.boosts_override
        if deps.boosts_override is not None
        else select_boosts(cfg, graph, tier_map=tier_map, entity_first_routing_on=entity_first_routing_on)
    )

    pipeline_logger = deps.logger_override if deps.logger_override is not None else _build_search_logger(env)

    resolver = (
        deps.resolver_override
        if deps.resolver_override is not None
        else build_collection_resolver(db_path=resolved_db_path, env=env)
    )

    # Auto-wire the fact retriever when the operator's data dir contains a
    # facts table. The SQLiteFactStore uses the same SQLite database file as
    # the chunk store; if the operator has called ``kairix ingest-chat``,
    # the table exists and federation activates automatically. Vault-only
    # operators have no facts table → fact_retriever stays None → today's
    # chunk-only behaviour preserved. Explicit ``fact_retriever=`` kwarg
    # still wins (tests + future config-driven opt-in).
    #
    # PLA-262 (#340) — thread the SAME ``embed_service`` built above into the
    # fact store so ``FactStore.search`` can embed the query for vector/RRF
    # fusion. The auto-wired store previously got no embedder, so the fused
    # recall path was structurally dead.
    resolved_fact_retriever = (
        fact_retriever if fact_retriever is not None else _auto_wire_fact_retriever(resolved_db_path, embed_service)
    )

    # Query cache: ``QUERY_CACHE_DISABLED`` sentinel wires ``None``;
    # an explicit ``QueryResultCache`` is used verbatim; default wires
    # the process-shared LRU.
    query_cache = _resolve_query_cache(deps.query_cache_override, cfg)

    # #411 Phase 2 — record the pipeline-build marker BEFORE constructing
    # the query cache, so the marker's cfg_hash is the one scoping the
    # persistent cache rows. Marker writes are best-effort (defensive
    # against disk failures inside the marker module itself). Skip the
    # marker write when query_cache_override is set so test-wired caches
    # don't contaminate the production marker file.
    if deps.query_cache_override is None:
        _record_pipeline_build_marker(cfg)

    # Cache writes are owned by the caller (build_search_pipeline) under
    # ``_PIPELINE_CACHE_LOCK``; this helper just builds + returns.
    # Issue 2 — wire cross-encoder rerank into production. The reranker
    # is a closure that calls kairix.core.search.rerank.rerank with the
    # config-derived model + candidate_limit. Gating (intent + enabled
    # flag) lives in pipeline._maybe_rerank — the closure is unconditional.
    # When sentence-transformers isn't installed (kairix[rerank] extra
    # not pulled), the rerank function returns the input unchanged and
    # logs WARNING once. None-out the reranker only when the operator
    # has explicitly disabled rerank AND no intents are registered for
    # it — saves a closure allocation per build for the disabled path.
    #
    # F47 paydown — ``reranker_override`` short-circuits the production
    # closure: the ``RERANK_DISABLED`` sentinel wires ``reranker=None``
    # (no torch import); any other non-``None`` value is used verbatim
    # (a no-op / fake reranker for tests). Production callers leave the
    # override ``None`` and the cross-encoder closure below is built.
    pipeline_reranker = _resolve_reranker(deps.reranker_override, cfg)

    return SearchPipeline(
        classifier=classifier,
        bm25=bm25,
        vector=vector,
        graph=graph,
        fusion=fusion,
        boosts=boosts,
        logger=pipeline_logger,
        resolver=resolver,
        config=cfg,
        # #281 — wire the process-shared LRU so repeat queries from
        # teaming agents skip the Azure embed roundtrip. #411 Phase 2
        # — pass the resolved cfg_hash so persistent rows are scoped
        # to the current pipeline configuration.
        query_cache=query_cache,
        # Plan B-parity Capability #5 — opt-in fact federation. ``None``
        # preserves today's chunk-only behaviour for vault-only deployments.
        # Auto-wired above when the operator's data dir contains a facts table.
        fact_retriever=resolved_fact_retriever,
        # Issue 2 — production cross-encoder rerank closure. The pipeline
        # decides per-call whether to invoke it based on config + intent.
        reranker=pipeline_reranker,
        # PLA-270 — tiered-context summary source. ``None`` (production
        # default) serves the full L2 snippet for every hit; integration
        # tests inject FakeSummaryLoader so the composed path exercises
        # L0/L1 tiering + the ``max_tier`` ceiling.
        tier_summaries=deps.summary_loader_override,
    )


def _resolve_db_path(paths: KairixPaths | None) -> Any:
    """Pick ``paths.db_path`` (test seam) over the env-driven default."""
    if paths is not None:
        return paths.db_path
    from kairix.core.db import get_db_path

    return get_db_path()


def _default_doc_repo(db_path: Any) -> Any:
    """Construct the production ``SQLiteDocumentRepository`` for ``db_path``."""
    from kairix.core.db.repository import SQLiteDocumentRepository

    return SQLiteDocumentRepository(db_path=db_path)


def _resolve_query_cache(override: Any, cfg: RetrievalConfig) -> Any:
    """Return the wired ``query_cache`` for the pipeline.

    Three resolution branches:
      * ``override is QUERY_CACHE_DISABLED`` → ``None`` (no caching).
      * ``override`` is any other non-``None`` value (e.g. a
        ``QueryResultCache``) → use it verbatim.
      * ``override is None`` → the process-shared LRU keyed on the
        resolved cfg_hash (production default).
    """
    if override is QUERY_CACHE_DISABLED:
        return None
    if override is not None:
        return override
    return _get_or_create_query_cache(cfg_hash=_compute_cfg_hash(cfg))


def _resolve_reranker(override: Any, cfg: RetrievalConfig) -> Any:
    """Return the wired ``reranker`` closure for the pipeline.

    Three resolution branches (mirrors :func:`_resolve_query_cache`):
      * ``override is RERANK_DISABLED`` → ``None`` (the pipeline's
        ``_maybe_rerank`` short-circuits; no ``sentence-transformers`` /
        torch import).
      * ``override`` is any other non-``None`` value (a no-op / fake
        reranker closure) → use it verbatim.
      * ``override is None`` → the production cross-encoder closure
        keyed on the config-derived model + candidate_limit, unless the
        operator has fully disabled rerank (``rerank.enabled`` False and
        no ``rerank_intents``), in which case ``None`` is wired to save
        a per-build closure allocation.

    The production closure is built lazily — the
    ``kairix.core.search.rerank`` import (and its transitive
    ``sentence-transformers`` load on first call) only happens when the
    real closure is actually wired.
    """
    if override is RERANK_DISABLED:
        return None
    if override is not None:
        return override
    rerank_disabled = not cfg.rerank.enabled and not cfg.rerank_intents
    if rerank_disabled:
        return None
    from kairix.core.search.rerank import rerank as _rerank_impl

    def pipeline_reranker(query: str, fused: list[FusedResult]) -> list[FusedResult]:
        return _rerank_impl(
            query,
            fused,
            model=cfg.rerank.model,
            candidate_limit=cfg.rerank.candidate_limit,
        )

    return pipeline_reranker


def _compute_cfg_hash(cfg: RetrievalConfig) -> str:
    """Lazy proxy for ``pipeline_cache_marker.compute_cfg_hash`` (#411 Phase 2).

    Local helper so this module doesn't pay the marker-module import
    cost when persistence isn't wired (e.g. tests passing in fakes).
    """
    from kairix.core.pipeline_cache_marker import compute_cfg_hash

    return compute_cfg_hash(cfg)


def _record_pipeline_build_marker(cfg: RetrievalConfig) -> None:
    """Record the pipeline-build marker on disk (#411 Phase 2).

    Best-effort — disk failures inside the marker module are swallowed.
    Skipped under pytest so test runs don't write the marker file into
    the developer's real data dir (same guard pattern as the embed
    cache; see :func:`_resolve_query_cache_path`).
    """
    import os
    import time as _time

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:  # pragma: no cover  # F4 test-bypass: marker write only fires outside pytest
        from kairix.core.pipeline_cache_marker import PipelineCacheMarker
        from kairix.paths import pipeline_cache_path

        marker = PipelineCacheMarker(path=pipeline_cache_path())
        try:
            marker.record(_compute_cfg_hash(cfg), _time.time())
        finally:
            marker.close()
    except Exception as exc:  # pragma: no cover  # same — production-only branch
        logger.debug(
            "factory: pipeline-build marker write skipped — %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Connector pipeline factory — F46 sanctioned entry point for BDD step impls
# that exercise the connector framework's per-batch composition.
# ---------------------------------------------------------------------------


def build_connector_pipeline(
    *,
    db: Any,
    bronze_root: Any = None,
    collection: str,
    silver: Any = None,
    chunk_writer: Any = None,
    entity_graph_sink: Any = None,
    deps: FactoryDeps | None = None,
) -> Any:
    """Construct a production-shape ConnectorPipeline against ``db``.

    Composes the shipped Bronze + Silver + chunk-writer + entity-graph-sink
    + cursor + dead-letter stores using the same wiring the worker uses
    via ``run_connector_sync_pipeline``. F46/F47-sanctioned entry point so
    BDD step impls + integration tests exercising connector-ingest
    behaviour don't construct ``ConnectorPipeline(...)`` directly.

    Phase 7 of streaming-bronze: the pipeline uses StreamingBronzeStore
    exclusively — no on-disk blobs. The ``bronze_root`` parameter is
    accepted for backward-compat signature but ignored; new code should
    omit it.

    Optional ``silver`` / ``chunk_writer`` / ``entity_graph_sink``
    overrides let integration tests inject scripted-failure stand-ins.

    ``deps.disk_free_override`` injects a deterministic free-bytes
    resolver so integration / BDD tests can exercise the ADR-020
    watermark gate without touching the host filesystem. Default ``None``
    keeps the pipeline's production default (queries ``/data``; falls
    back to ``sys.maxsize`` when ``/data`` isn't mounted).
    """
    # Auto-hydrate secrets at the factory boundary so Python-API consumers
    # (eval harnesses, integration tests outside the worker, ad-hoc scripts)
    # don't hit SecretNotFoundError. Worker entry points already call this
    # at startup, but the factory is the universal entry — centralising
    # bootstrap here closes the structural gap surfaced by 3874bf7e.
    deps = deps if deps is not None else FactoryDeps()
    _run_bootstrap_secrets(deps, caller="build_connector_pipeline")

    # Phase 7: streaming bronze writes no files; bronze_root is accepted
    # for backward-compat call-signature but unused. New callers should
    # omit it. Logged at debug if passed so the deprecation is visible.
    if bronze_root is not None:
        logger.debug(
            "build_connector_pipeline: bronze_root parameter is unused since "
            "Phase 7 (streaming bronze writes no files); omit it from the call."
        )
    from kairix.core.connectors import (
        ConnectorPipeline,
        CursorStore,
        DeadLetterStore,
        DefaultSilverProcessor,
        SqliteDocumentPagesWriter,
        SqliteDocumentsMediaWriter,
        SqliteSilverSourceWriter,
        StreamingBronzeStore,
    )
    from kairix.core.connectors.collection_router import legacy_chunk_writer
    from kairix.worker import _SqliteEntityGraphSink

    # GH #336 (ADR-024 Bundle B) — documents_media writer.
    # GH #338 (F70 paydown) — document_pages writer; per-page rows
    # enable retrieval citation paths back to the source page/slide.
    # ADR-028 Wave F.4 — silver_source writer; the per-document source
    # markdown lets the re-chunk sweep re-chunk without remote re-fetch.
    # Caller-supplied silvers opt out by passing their own ``silver=``.
    default_silver = DefaultSilverProcessor(
        documents_media_writer=SqliteDocumentsMediaWriter(db),
        document_pages_writer=SqliteDocumentPagesWriter(db),
        silver_source_writer=SqliteSilverSourceWriter(db),
    )
    return ConnectorPipeline(
        db=db,
        bronze=StreamingBronzeStore(db),
        silver=silver if silver is not None else default_silver,
        chunk_writer=chunk_writer if chunk_writer is not None else legacy_chunk_writer(db, collection=collection),
        entity_graph_sink=entity_graph_sink if entity_graph_sink is not None else _SqliteEntityGraphSink(db),
        cursor_store=CursorStore(db),
        dead_letter=DeadLetterStore(db),
        disk_free_resolver=deps.disk_free_override,
    )


# ---------------------------------------------------------------------------
# Neo4j entity-graph drain factory — GH #334, F46/F47 sanctioned entry point
# for BDD step impls + integration tests exercising the Curator-coupling
# boundary that drains ``entity_signals`` into Neo4j.
# ---------------------------------------------------------------------------


def build_neo4j_drainer(
    *,
    db: Any,
    repo: Any,
    batch_size: int | None = None,
    deps: FactoryDeps | None = None,
) -> Any:
    """Construct a :class:`kairix.core.curator.drain.Neo4jDrainer`.

    The drain wraps the SQLite ``entity_signals`` staging table and the
    Neo4j graph backend. Production callers pass a writable sqlite
    Connection + a :class:`kairix.knowledge.graph.repository.Neo4jGraphRepository`;
    tests pass an in-memory DB + a Fake repo (see
    ``tests/fakes.py::FakeDrainGraphRepository``).

    ``batch_size`` defaults to the module's
    :data:`~kairix.core.curator.drain.DEFAULT_DRAIN_BATCH_SIZE` (500).
    F47-sanctioned entry point so integration tests can compose the
    drain without importing :class:`Neo4jDrainer` directly.
    """
    # Auto-hydrate secrets at the factory boundary so Python-API consumers
    # don't hit SecretNotFoundError when the Neo4j repo lazily resolves its
    # connection credentials. See build_search_pipeline for the same pattern.
    deps = deps if deps is not None else FactoryDeps()
    _run_bootstrap_secrets(deps, caller="build_neo4j_drainer")

    from kairix.core.curator.drain import DEFAULT_DRAIN_BATCH_SIZE, Neo4jDrainer

    effective_batch = batch_size if batch_size is not None else DEFAULT_DRAIN_BATCH_SIZE
    return Neo4jDrainer(db, repo, batch_size=effective_batch)
