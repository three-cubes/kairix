"""SearchPipeline — the orchestrator that composes protocols into a search pipeline.

Replaces the procedural search() function in hybrid.py with a composed object.
Each stage is a protocol implementation injected at construction time, so tests
can swap any component with a fake — no monkey-patching needed.

Pipeline stages:
  1. Classify query intent
  2. Fuse BM25 + vector results
  3. Apply boost chain
  4. Apply token budget
  5. Log search event
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from kairix.core.facts.records import resolve_fact_source_uri
from kairix.core.protocols import (
    BoostStrategy,
    CollectionResolver,
    FactStore,
    FusionStrategy,
    GraphRepository,
    SearchLogger,
)
from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.budget import BudgetDeps, SummaryLoader, apply_budget, coerce_tier
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.query_cache import QueryResultCache, make_cache_key
from kairix.core.search.rrf import FusedResult
from kairix.core.search.scope import Scope

logger = logging.getLogger(__name__)

# Process-shared thread pool for parallel BM25 + vector dispatch (#perf-dispatch).
# Profile data (post-#409): dispatch=2384ms = bm25 (~605ms) + vector (~1780ms)
# strictly sequential. Both legs are I/O-bound (SQLite FTS5 + Azure embed HTTP)
# and have no inter-dependency — running them on the same thread serialises
# wall-clock for no good reason. Overlapping them cuts the per-search dispatch
# floor to ~max(bm25, vector); on warm-cache miss that's ~600ms saved off the
# critical path (≈25% of dispatch budget) with no result-set change.
#
# Pool SIZE (PLA-272). Each in-flight search submits exactly two futures (one
# BM25 + one vector). The pool was previously fixed at 2 workers, which capped
# the WHOLE process at a single search's worth of dispatch parallelism: under
# teaming load (#436 benchmarked post-warm p95 ~640ms at --concurrency 5 — ten
# dispatch tasks contending for two slots) the surplus tasks queued and the
# pool became the bottleneck the parallel-dispatch optimisation was meant to
# remove. Size from the expected concurrent-search load instead
# (``_FUTURES_PER_SEARCH * KAIRIX_MAX_CONCURRENCY``) so every concurrent
# search's two legs overlap rather than serialising behind the ceiling.
#
# Built LAZILY (not at import) so the size resolves from config/env at first
# use — and so tests can size + inject a pool via the ``dispatch_pool`` seam on
# SearchPipeline without touching process env (F2-clean). Threads keep the
# ``search-dispatch`` prefix so the pool stays identifiable in py-spy / stack
# dumps, distinct from coalescer, embed-batch, or factory-build threads. The
# per-worker thread-init cost (~0.5-1ms) is still paid once and amortised
# across every subsequent search because the singleton is reused.
_FUTURES_PER_SEARCH = 2

# CPU-aware default concurrency (PLA-272 follow-up). When
# ``KAIRIX_MAX_CONCURRENCY`` is UNSET, the expected concurrent-search load —
# and therefore the dispatch pool size — is derived from the host core count
# so a bigger box (e.g. an 8-core D8as_v5) auto-uses more dispatch
# parallelism without an operator env tweak, while a 1-2 core CI box stays
# bounded. Each concurrent search still submits ``_FUTURES_PER_SEARCH``
# futures, so the resolved concurrency multiplies into the pool size via
# :func:`dispatch_workers_for`.
#
# Derivation: ``clamp(_CONCURRENCY_PER_CPU * cpu_count, floor, ceiling)``.
# Both legs are I/O-bound (SQLite FTS5 + Azure embed HTTP), so a modest 2x
# over-subscription of cores keeps several concurrent searches' two legs in
# flight without pinning the box. The floor keeps a single-core box able to
# overlap a few searches (pool never below ``2 * _MIN_DISPATCH_CONCURRENCY``);
# the ceiling stops a 32/64-core host allocating an unbounded pool that just
# adds thread-switch overhead the I/O-bound legs never cash in (pool never
# above ``2 * _MAX_DISPATCH_CONCURRENCY``).
#
# The clamp applies ONLY to the auto-derived default. An explicit
# ``KAIRIX_MAX_CONCURRENCY`` stays authoritative and un-clamped — an operator
# who names a value owns its pool-size consequence (behaviour preserved from
# PLA-272, where the env value fed ``dispatch_workers_for`` verbatim).
_CONCURRENCY_PER_CPU = 2
_MIN_DISPATCH_CONCURRENCY = 4
_MAX_DISPATCH_CONCURRENCY = 32

# Fallback used only when ``os.cpu_count()`` can't determine the core count
# (it returns ``None`` on some constrained / containerised hosts). Equals a
# 4-core box under the 2x rule — the historical fixed default (PLA-272) — so
# an unknowable host keeps the prior behaviour rather than collapsing to the
# floor.
DEFAULT_DISPATCH_CONCURRENCY = 8

_DISPATCH_POOL: ThreadPoolExecutor | None = None
_DISPATCH_POOL_LOCK = threading.Lock()


def dispatch_workers_for(concurrency: int) -> int:
    """Pool workers needed to dispatch ``concurrency`` concurrent searches.

    Each search submits one BM25 + one vector future
    (``_FUTURES_PER_SEARCH``), so ``2 * concurrency`` slots keep every
    concurrent search's two legs overlapping instead of queueing behind a
    fixed ceiling. Floored at ``_FUTURES_PER_SEARCH`` so a misconfigured
    ``concurrency <= 1`` still parallelises a single search's two legs rather
    than collapsing to sequential.
    """
    return _FUTURES_PER_SEARCH * max(1, concurrency)


def build_dispatch_pool(concurrency: int) -> ThreadPoolExecutor:
    """Build a dispatch pool sized for ``concurrency`` concurrent searches.

    ``max_workers`` comes from :func:`dispatch_workers_for` so the size always
    tracks the configured concurrency — never a hardcoded literal. The caller
    owns the returned pool's lifecycle; :func:`_default_dispatch_pool` reuses a
    single process-wide instance.
    """
    return ThreadPoolExecutor(
        max_workers=dispatch_workers_for(concurrency),
        thread_name_prefix="search-dispatch",
    )


def cpu_aware_default_concurrency(cpu_count: int | None) -> int:
    """Derive the default concurrent-search load from the host core count.

    Public sizing seam (sibling of :func:`dispatch_workers_for`): ``cpu_count``
    is passed in (``os.cpu_count()`` at the one production call site) so tests
    exercise the scaling by value — a small core count yields a small load, an
    8-core box a larger one — without patching the process. The result is
    always clamped to ``[_MIN_DISPATCH_CONCURRENCY, _MAX_DISPATCH_CONCURRENCY]``
    so a 1-2 core box can't over-allocate and a 32/64-core box can't
    unbounded-explode the pool.

    Returns :data:`DEFAULT_DISPATCH_CONCURRENCY` when ``cpu_count`` is
    ``None`` (``os.cpu_count()`` can't determine the count) so an unknowable
    host keeps the historical fixed default.
    """
    if cpu_count is None:
        return DEFAULT_DISPATCH_CONCURRENCY
    scaled = _CONCURRENCY_PER_CPU * max(1, cpu_count)
    return min(_MAX_DISPATCH_CONCURRENCY, max(_MIN_DISPATCH_CONCURRENCY, scaled))


def resolve_dispatch_concurrency() -> int:
    """Resolve the expected concurrent-search load, with env override (F4-clean).

    Public seam so the resolution (env-override precedence + CPU-aware default)
    is testable by value. The env read is routed through :mod:`kairix.paths`
    (no direct process-env read leaks into the search tier). When the operator
    knob is UNSET the default is CPU-aware (:func:`cpu_aware_default_concurrency`
    over ``os.cpu_count()``) so a bigger box auto-scales dispatch parallelism;
    when ``KAIRIX_MAX_CONCURRENCY`` is SET, ``read_int_env`` returns that value
    verbatim and it stays authoritative (operators tune it to their teaming
    load — the number of agents firing searches at once).
    """
    from kairix.paths import read_int_env

    return read_int_env(
        "KAIRIX_MAX_CONCURRENCY",
        default=cpu_aware_default_concurrency(os.cpu_count()),
    )


def _default_dispatch_pool() -> ThreadPoolExecutor:
    """Return the process-lifetime dispatch pool, building it lazily.

    Built on the first search and reused for every subsequent dispatch so the
    per-worker thread-init cost is paid once and amortised. Sized from
    :func:`resolve_dispatch_concurrency` so the pool matches the expected
    concurrent load instead of the legacy fixed two workers (PLA-272).
    """
    global _DISPATCH_POOL
    with _DISPATCH_POOL_LOCK:
        if _DISPATCH_POOL is None:
            _DISPATCH_POOL = build_dispatch_pool(resolve_dispatch_concurrency())
        return _DISPATCH_POOL


# Process-shared thread pool for the CPU-bound cross-encoder rerank stage
# (PLA-272). Rerank is the single largest search stage (~331ms on the
# production probe) and — unlike bm25+vector, which the dispatch pool already
# overlaps — it ran INLINE on the request thread. Under concurrent teaming load
# that inline call was the serialisation point: a concurrency soak (N=1→20
# distinct queries) saw latency scale linearly with N and throughput stay flat
# (~2.5 req/s, effective concurrency ≈ 1).
#
# sentence-transformers/torch releases the GIL during the C/torch forward pass,
# so concurrent rerank forward passes CAN run in parallel across cores — but
# only when they go through a bounded, shared executor instead of contending
# uncontrolled on the (up-to-32) transport worker threads. Routing rerank onto
# a dedicated pool sized for the expected concurrent-search load caps the number
# of simultaneous forward passes at the configured concurrency (rather than the
# transport's worker ceiling), so concurrent requests' rerank overlaps on a
# controlled set of cores instead of oversubscribing them.
#
# Sized from the SAME ``KAIRIX_MAX_CONCURRENCY`` signal as the dispatch pool
# (:func:`resolve_dispatch_concurrency`) — one rerank task per search, so the
# worker count is the concurrency itself (the dispatch pool submits two futures
# per search and so doubles it). Built LAZILY so the size resolves from
# config/env at first use, and injectable via the ``rerank_pool`` seam on
# SearchPipeline so tests exercise routing + sizing without touching process env
# (F2-clean). Threads carry the ``search-rerank`` prefix so the pool stays
# identifiable in py-spy / stack dumps, distinct from the ``search-dispatch``
# pool. The single-request ranking is byte-for-byte unchanged — the reranker
# runs the same input on a pool thread instead of the request thread; only the
# concurrent overlap changes.
_RERANK_POOL: ThreadPoolExecutor | None = None
_RERANK_POOL_LOCK = threading.Lock()


def rerank_workers_for(concurrency: int) -> int:
    """Pool workers needed to rerank ``concurrency`` concurrent searches.

    Each search runs exactly one rerank task (unlike dispatch, which submits
    one BM25 + one vector future), so the worker count is the concurrency
    itself — the shared pool then caps the number of simultaneous CPU-bound
    cross-encoder forward passes at the configured concurrent-search load
    rather than the transport's worker ceiling. Floored at 1 so a
    misconfigured ``concurrency <= 1`` still yields a usable single-worker
    pool.
    """
    return max(1, concurrency)


def build_rerank_pool(concurrency: int) -> ThreadPoolExecutor:
    """Build a rerank pool sized for ``concurrency`` concurrent searches.

    ``max_workers`` comes from :func:`rerank_workers_for` so the size always
    tracks the configured concurrency — never a hardcoded literal. The caller
    owns the returned pool's lifecycle; :func:`_default_rerank_pool` reuses a
    single process-wide instance.
    """
    return ThreadPoolExecutor(
        max_workers=rerank_workers_for(concurrency),
        thread_name_prefix="search-rerank",
    )


def _default_rerank_pool() -> ThreadPoolExecutor:
    """Return the process-lifetime rerank pool, building it lazily.

    Built on the first reranked search and reused for every subsequent rerank
    so the per-worker thread-init cost is paid once and amortised. Sized from
    :func:`resolve_dispatch_concurrency` — the same ``KAIRIX_MAX_CONCURRENCY``
    signal the dispatch pool uses — so the two pools scale together with the
    operator's teaming load (PLA-272).
    """
    global _RERANK_POOL
    with _RERANK_POOL_LOCK:
        if _RERANK_POOL is None:
            _RERANK_POOL = build_rerank_pool(resolve_dispatch_concurrency())
        return _RERANK_POOL


@dataclass
class SearchResult:
    """Full result from the search pipeline."""

    query: str
    intent: QueryIntent
    results: list = field(default_factory=list)

    # Diagnostic info
    bm25_count: int = 0
    vec_count: int = 0
    fact_count: int = 0
    fused_count: int = 0
    # Per-stage wall-clock latency (ms) — populated by SearchPipeline.search.
    # Keys: classify, resolve, dispatch (bm25+vec parallel), fuse, enrich,
    # boost, budget. Sums approximately to total latency_ms minus a few ms
    # of bookkeeping. Surfaced via ``run_probe_search(... json_out=True)`` —
    # the legacy ``kairix probe search`` CLI was retired in v2026.6 (#282).
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    collections: list[str] = field(default_factory=list)
    tiers_used: list[str] = field(default_factory=list)
    total_tokens: int = 0
    latency_ms: float = 0.0
    vec_failed: bool = False
    fallback_used: bool = False
    error: str = ""


@dataclass
class SearchPipeline:
    """Composes protocol implementations into a complete search pipeline.

    Constructed once at startup via build_search_pipeline() factory (or directly
    in tests with fakes). Each field is a protocol implementation — swap any
    one to change behaviour without touching orchestration logic.
    """

    classifier: object  # IntentClassifier
    bm25: BM25SearchBackend
    vector: VectorSearchBackend
    graph: GraphRepository
    fusion: FusionStrategy
    boosts: list[BoostStrategy] = field(default_factory=list)
    logger: SearchLogger | None = None
    resolver: CollectionResolver | None = None
    config: RetrievalConfig = field(default_factory=RetrievalConfig.defaults)
    # In-process query-result cache (#281). When None, no caching is
    # applied — preserves existing test behaviour where pipelines are
    # constructed directly with fakes. Factories that want caching
    # inject a shared QueryResultCache instance per process.
    query_cache: QueryResultCache | None = None
    # Plan B-parity Capability #5 — optional fact retriever federated
    # alongside BM25 + vector. When ``None``, the federation stage is a
    # no-op and the pipeline degenerates to today's chunk-only behaviour
    # (regression-pinned). When wired, attribute-fact queries weight
    # fact hits ~2x their chunk counterparts in the fusion stage.
    fact_retriever: FactStore | None = None
    # Top-k feed into the fact retriever. Lossier than chunks, so the
    # default is roomy enough to give the intent-weighted fusion stage
    # genuine candidates to surface.
    top_k_facts: int = 15
    # Optional cross-encoder reranker (Issue 2 — closes the dead-code gap
    # where kairix.core.search.rerank.rerank() shipped + tested but was
    # never called from the pipeline). When wired AND
    # (config.rerank.enabled OR intent.value in config.rerank_intents),
    # the reranker is called after the boost chain and before budget so
    # the cross-encoder pass operates on the boosted candidates and the
    # budget enforces token caps on the reranked order.
    #
    # Signature: ``reranker(query, fused_results) -> list[FusedResult]``.
    # When None (the test-pipeline default), the rerank stage is a no-op
    # and the pipeline degrades to today's BM25-primary + boost ordering
    # — preserves every existing test that constructs SearchPipeline
    # directly with fakes.
    reranker: Callable[[str, list[FusedResult]], list[FusedResult]] | None = None
    # Backend dispatch pool for the parallel BM25 + vector legs. When ``None``
    # (the default — what the factory and every existing test construct), the
    # process-shared pool from :func:`_default_dispatch_pool` is used, sized
    # for the expected concurrent load so teaming traffic doesn't queue behind
    # the legacy fixed 2-worker ceiling (PLA-272). Tests inject a known-size
    # pool here to exercise sizing without touching process env (F2-clean).
    dispatch_pool: ThreadPoolExecutor | None = None
    # Bounded executor for the CPU-bound cross-encoder rerank stage (PLA-272).
    # When ``None`` (the default — what the factory and every existing test
    # construct), the process-shared pool from :func:`_default_rerank_pool` is
    # used, sized from the same ``KAIRIX_MAX_CONCURRENCY`` signal as the dispatch
    # pool so concurrent teaming searches' rerank overlaps on a controlled set of
    # cores instead of serialising on the request thread. The single-request
    # ranking is byte-for-byte identical — the reranker runs the same input on a
    # pool thread rather than inline; only the concurrent overlap changes. Tests
    # inject a known-size pool here to exercise routing + sizing without touching
    # process env (F2-clean).
    rerank_pool: ThreadPoolExecutor | None = None
    # PLA-270 — optional tiered-context summary source (a ``SummaryLoader``).
    # When ``None`` (the default the factory and every direct test construct)
    # the budget stage serves the full ``L2`` snippet for every row —
    # byte-identical to the pre-tiering behaviour. When wired (operator
    # generated L0/L1 summaries), the budget stage serves the cheapest
    # sufficient tier per the score/budget selection AND honours a caller's
    # ``max_tier`` ceiling.
    tier_summaries: SummaryLoader | None = None

    def search(
        self,
        query: str,
        budget: int = 3000,
        scope: Scope = Scope.SHARED_AGENT,
        agent: str | None = None,
        collections: list[str] | None = None,
        namespace: str | None = None,
        intent: QueryIntent | None = None,
        max_tier: str = "L2",
    ) -> SearchResult:
        """Execute the full search pipeline using composed components.

        ``namespace`` is the engagement-scoped recall key threaded through
        to the optional ``fact_retriever`` (Capability #5). When the
        pipeline has no fact retriever wired, ``namespace`` is ignored.

        ``intent`` lets a caller that has *already* classified the query
        (e.g. ``run_search``, which classifies once to size the token
        budget) hand the result in so the pipeline doesn't redundantly
        re-classify the same query on the warm path (#436 / PLA-273). When
        ``None`` (every existing caller + every direct test) the pipeline
        classifies internally as before. A supplied intent is treated as
        fully confident (confidence ``1.0``) — which matches production's
        rule classifier, whose ``classify`` surface already yields ``1.0``.

        Never raises — returns SearchResult with empty results on any failure.
        """
        # 0. Query-cache fast path (#281). When a cache is wired and the
        # key hits, return the cached SearchResult immediately — sidesteps
        # the entire pipeline including the dominant Azure embed HTTP cost.
        cache_key: tuple[Any, ...] | None = None
        if self.query_cache is not None:
            cache_key = make_cache_key(query, scope, agent, collections, coerce_tier(max_tier))
            cached = self.query_cache.get(cache_key)
            if cached is not None:
                return cached

        t_start = time.monotonic()
        stages: dict[str, float] = {}

        def _stage(name: str, start: float) -> None:
            """Record one stage's wall-clock duration into the stages dict."""
            stages[name] = round((time.monotonic() - start) * 1000.0, 2)

        # 1. Classify intent — reuse a caller-supplied intent when present
        # (PLA-273 warm-path dedup) so the query isn't classified twice
        # across run_search + the pipeline. Production's rule classifier
        # already reports confidence 1.0, so a supplied intent matches it.
        t = time.monotonic()
        if intent is None:
            intent, intent_confidence = self._classify_with_confidence(query)
        else:
            intent_confidence = 1.0
        _stage("classify", t)

        # 2. Entity intent requires graph
        if intent == QueryIntent.ENTITY and not self.graph.available:
            return SearchResult(query=query, intent=intent, error=_ENTITY_GRAPH_UNAVAILABLE, stage_latency_ms=stages)

        # 3. Resolve collections via the injected CollectionResolver
        t = time.monotonic()
        collections, resolve_error = self._resolve_collections(collections, agent, scope)
        _stage("resolve", t)
        if resolve_error is not None:
            return SearchResult(query=query, intent=intent, error=resolve_error, stage_latency_ms=stages)

        # 4. Dispatch BM25 + vector search — split into bm25/vector inside the helper
        t = time.monotonic()
        bm25_results, vec_results, vec_failed = self._dispatch_backends(query, collections, stages)
        _stage("dispatch", t)

        # 4b. Federation — dispatch the optional fact retriever (Cap #5).
        # Degrades to empty list when no retriever is wired or it raises;
        # never poisons the chunk-only pipeline result.
        t = time.monotonic()
        fact_hits = self._dispatch_facts(query, namespace)
        _stage("facts", t)

        # 5. Fuse — intent-weighted when the fact layer is wired (Cap #5).
        t = time.monotonic()
        fused = self._fuse_with_intent(bm25_results, vec_results, fact_hits, intent)
        fused = self._filter_by_collections(fused, collections)
        _stage("fuse", t)

        # 5b. Enrich each fused result with chunk_date metadata so the boost
        # chain (specifically ChunkDateBoost) can score by recency. Source
        # of truth is DocumentRepository.get_chunk_dates, exposed here via
        # the BM25 backend — boost adapters never reach into the repo.
        t = time.monotonic()
        self._enrich_chunk_dates(fused)
        _stage("enrich", t)

        # 6. Boost chain
        t = time.monotonic()
        fused = self._apply_boosts(fused, query, intent, intent_confidence)
        _stage("boost", t)

        # 6b. Cross-encoder rerank (Issue 2). No-op when reranker is None
        # OR when the intent isn't in config.rerank_intents (and rerank
        # isn't force-enabled). The reranker re-orders the top-N boosted
        # candidates by semantic similarity to the query so the budget
        # stage gets a higher-quality ordering to truncate from.
        t = time.monotonic()
        fused = self._maybe_rerank(query, fused, intent)
        _stage("rerank", t)

        # 7. Budget — tier each row to the cheapest sufficient representation
        # (PLA-270). ``summary_loader`` activates L0/L1 serving; ``max_tier``
        # is the caller's "no richer than this" ceiling, coerced once here so
        # an unknown request degrades to the full snippet.
        t = time.monotonic()
        budgeted = apply_budget(
            fused,
            budget=budget,
            deps=BudgetDeps(tier_summaries=self.tier_summaries),
            max_tier=coerce_tier(max_tier),
        )
        _stage("budget", t)
        total_tokens = sum(getattr(r, "token_estimate", 0) for r in budgeted)
        tiers_used = sorted({getattr(r, "tier", "L2") for r in budgeted})

        latency_ms = (time.monotonic() - t_start) * 1000.0

        result = SearchResult(
            query=query,
            intent=intent,
            results=budgeted,
            bm25_count=len(bm25_results),
            vec_count=len(vec_results),
            fact_count=len(fact_hits),
            fused_count=len(fused),
            collections=collections or [],
            tiers_used=tiers_used,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            vec_failed=vec_failed,
            fallback_used=not bm25_results and bool(vec_results),
            stage_latency_ms=stages,
        )

        # 8. Log
        self._log_search(query, intent, agent, scope, collections, result)

        # 9. Cache write (#281) — only cache successful results. Caching
        # errors would mask transient outages from subsequent retries
        # and stick a degraded answer in front of every same-key caller
        # for the next 5 minutes.
        if cache_key is not None and self.query_cache is not None and not result.error:
            self.query_cache.put(cache_key, result)

        return result

    def _classify(self, query: str) -> QueryIntent:
        """Classify intent; fall back to SEMANTIC on any classifier failure.

        Backwards-compat shim: delegates to :meth:`_classify_with_confidence`
        and returns the primary intent only. Existing tests + call sites
        that don't need confidence keep working byte-for-byte.
        """
        return self._classify_with_confidence(query)[0]

    def _classify_with_confidence(self, query: str) -> tuple[QueryIntent, float]:
        """Classify intent + emit confidence (Issue #456).

        Prefers ``classifier.classify_with_confidence(query)`` when the
        classifier exposes it (production + post-#456 fakes); falls back to
        ``classifier.classify(query)`` with confidence=1.0 when the
        classifier is a legacy fake/adapter that hasn't been updated.

        Confidence is consumed by the boost layer when the
        ``intent_confidence_gated_boosts`` feature flag is ON — see
        :func:`kairix.core.search.boosts._intent_confidence_passes`. When
        the flag is OFF, the confidence value is irrelevant.

        Failure-isolated: any classifier exception returns
        ``(SEMANTIC, 0.0)``. The 0.0 confidence makes downstream gated
        boosts skip, which is the safe-default behaviour.
        """
        try:
            classifier_with_confidence = getattr(self.classifier, "classify_with_confidence", None)
            if classifier_with_confidence is not None:
                decision = classifier_with_confidence(query)
                return decision.primary, float(decision.confidence)
            # Legacy classifier surface — no confidence available.
            return self.classifier.classify(query), 1.0  # type: ignore[union-attr] — classifier may be None when graph backend unavailable; guarded by try/except.
        except Exception as e:
            _logger.warning("pipeline: classify failed — %s", e)
            return QueryIntent.SEMANTIC, 0.0

    def _validate_explicit_collections(
        self,
        collections: list[str],
        agent: str,
        scope: Scope,
    ) -> tuple[list[str] | None, str | None]:
        """GH #373 — validate operator-supplied collection names against the
        actor's scope via the resolver's ``validate_explicit``. A raised
        exception or a reported error yields ``(None, <message>)``."""
        try:
            filtered, error = self.resolver.validate_explicit(agent, collections, scope)
        except Exception as e:
            return None, str(e)
        if error is not None:
            return None, error
        return filtered, None

    def _resolve_collections(
        self,
        collections: list[str] | None,
        agent: str | None,
        scope: Scope,
    ) -> tuple[list[str] | None, str | None]:
        """Resolve collections via the injected resolver when not pre-supplied.

        GH #373 — when the operator supplies ``collections=[...]`` AND the
        resolver advertises a ``validate_explicit`` method (the canonical
        :class:`TopologyCollectionResolver` does; a resolver that doesn't
        is treated as pass-through), validate the names
        against the actor's scope. Out-of-scope names yield empty
        results + an F21-shaped error in the result envelope so the
        operator sees the misconfiguration immediately rather than via
        a silent empty response.
        """
        if collections is not None:
            if agent is not None and hasattr(self.resolver, "validate_explicit"):
                return self._validate_explicit_collections(collections, agent, scope)
            return collections, None
        if self.resolver is None:
            return collections, None
        try:
            return self.resolver.resolve(agent, scope), None
        except NotImplementedError as e:
            # Operator misconfiguration (scope=all-agents without registry).
            return None, str(e)

    def _dispatch_backends(
        self,
        query: str,
        collections: list[str] | None,
        stages: dict[str, float] | None = None,
    ) -> tuple[list[dict], list[dict], bool]:
        """Run BM25 + vector search in parallel; isolate each failure so one can't break the other.

        Both legs are I/O-bound (SQLite FTS5 + Azure embed HTTP + usearch ANN)
        with no inter-dependency. The previous implementation ran them
        sequentially on the request thread; this version submits both to the
        injected ``dispatch_pool`` (or the process-shared
        :func:`_default_dispatch_pool` when none is injected) so wall-clock
        collapses to ~max(bm25, vector) instead of bm25 + vector. On the
        production warm-cache miss path (bm25 ~605ms, vector ~1780ms) that
        saves ~600ms per search. The pool is sized for the expected concurrent
        load (PLA-272) so teaming traffic overlaps rather than queueing behind
        a fixed worker ceiling.

        When ``stages`` is supplied, records ``bm25`` and ``vector`` wall-clock
        deltas into it so the caller can decompose the ``dispatch`` stage in
        SearchResult.stage_latency_ms (#282 follow-up). The per-leg values now
        report leg-internal wall-clock; the outer ``dispatch`` value
        (measured in :meth:`search`) reports the parallel-collapsed total,
        so ``bm25 + vector > dispatch`` is the expected post-parallelism
        shape and signals genuine overlap to operators reading probe data.

        Per-leg timing is captured inside each leg's worker thread (around
        the actual ``.search()`` call) so the recorded duration reflects
        what each backend really spent — pool queueing delay sits outside
        the leg timer and shows up as the difference between the parent
        ``dispatch`` and the larger of the two leg values.
        """
        cfg = self.config

        def _run_bm25() -> list[dict]:
            t0 = time.monotonic()
            try:
                rows = self.bm25.search(query, collections=collections, limit=cfg.bm25_limit)
            except Exception as e:
                _logger.warning("pipeline: BM25 search failed — %s", e)
                rows = []
            finally:
                if stages is not None:
                    stages["bm25"] = round((time.monotonic() - t0) * 1000.0, 2)
            return rows

        def _run_vector() -> tuple[list[dict], bool]:
            t0 = time.monotonic()
            try:
                rows, failed = self._dispatch_vector(query, collections, stages=stages)
            finally:
                if stages is not None:
                    stages["vector"] = round((time.monotonic() - t0) * 1000.0, 2)
            return rows, failed

        pool = self.dispatch_pool if self.dispatch_pool is not None else _default_dispatch_pool()
        bm25_future = pool.submit(_run_bm25)
        vector_future = pool.submit(_run_vector)
        bm25_results = bm25_future.result()
        vec_results, vec_failed = vector_future.result()
        return bm25_results, vec_results, vec_failed

    def _dispatch_vector(
        self,
        query: str,
        collections: list[str] | None,
        stages: dict[str, float] | None = None,
    ) -> tuple[list[dict], bool]:
        """Vector backend dispatch with skip-flag and failure-vs-empty distinction.

        ``vec_failed`` reflects backend failure only — operators consume
        this field to triage real outages. Empty-and-failed conflation
        produced false-positive alerts.

        When ``stages`` is supplied, records the ``embed_http`` and
        ``vector_ann`` split via the VectorSearchBackend timing hook so
        probe data can attribute slow tail queries to Azure HTTP tail vs
        local ANN cost (#282 follow-up). ``vector`` (the sum) stays the
        outer-wall total recorded in ``_dispatch_backends``.
        """
        if self.config.skip_vector:
            return [], False
        try:
            return (
                self.vector.search(
                    query,
                    collections=collections,
                    limit=self.config.vec_limit,
                    timings=stages,
                ),
                False,
            )
        except Exception as e:
            _logger.warning("pipeline: vector search failed — %s", e)
            return [], True

    def _dispatch_facts(self, query: str, namespace: str | None) -> list[Any]:
        """Dispatch the optional fact retriever; degrade gracefully on failure.

        Returns the FactHit list verbatim — the conversion to fused
        ``FusedResult`` shape happens inside ``_fuse_with_intent`` so
        fact_count diagnostics still see the raw hit list.

        Plan B-parity Capability #5. When ``fact_retriever`` is None the
        federation stage is a no-op; when it raises, the chunk-only
        pipeline is returned as a graceful degradation (logged at WARN
        so operators can triage without poisoning the response).
        """
        if self.fact_retriever is None:
            return []
        try:
            return list(self.fact_retriever.search(query, top_k=self.top_k_facts, namespace=namespace))
        except Exception as e:
            _logger.warning("pipeline: fact retriever raised — facts excluded from fusion: %s", e)
            return []

    def _fuse(self, bm25_results: list[dict], vec_results: list[dict]) -> list:
        """Fuse BM25 + vector results; on fusion failure return empty list."""
        try:
            return self.fusion.fuse(bm25_results, vec_results)
        except Exception as e:
            _logger.warning("pipeline: fusion failed — %s — falling back to empty fused list", e)
            return []

    def _fuse_with_intent(
        self,
        bm25_results: list[dict],
        vec_results: list[dict],
        fact_hits: list[Any],
        intent: QueryIntent,
    ) -> list:
        """Intent-weighted fusion across BM25 + vector + fact hits.

        Three-way fusion (Plan B-parity Capability #5):

        - ATTRIBUTE_FACT → facts dominate (0.6 / 0.3 / 0.1)
        - MULTI_HOP     → chunks dominate (0.7 / 0.2 / 0.1)
        - default       → balanced (today's chunk-only fusion is preserved
                          when ``fact_hits`` is empty).

        When ``fact_hits`` is empty (no retriever wired, or it returned
        nothing, or it raised), the fact_weight branch contributes zero
        and the pipeline degenerates EXACTLY to ``_fuse`` (regression
        contract — pinned in test_pipeline_without_fact_retriever_unchanged).
        """
        chunk_fused = self._fuse(bm25_results, vec_results)
        if not fact_hits:
            return chunk_fused

        fact_w, chunk_w, _graph_w = _fusion_weights_for_intent(intent)
        # Normalise each layer's scores to [0, 1] before applying the
        # per-intent weights. Without this, fact-store overlap scores
        # (raw range 0..1) overwhelm RRF scores (raw range 0..~0.033)
        # regardless of weighting — the weighting contract only holds
        # when both layers are on the same scale.
        #
        # Issue #455 — pure max-relative normalisation auto-promotes a
        # single-row layer's lone weak hit to 1.0 regardless of absolute
        # confidence. ``fact_layer_min_floor`` / ``chunk_layer_min_floor``
        # set an absolute floor on the denominator so a weak hit stays
        # weak even when it's the only one in its layer. Default 0.0 =
        # no-op (pre-#455 behaviour); operators flip to ~0.4 to opt in.
        chunk_floor = float(self.config.chunk_layer_min_floor)
        max_chunk_raw = max((_read_rrf_score(r) for r in chunk_fused), default=0.0)
        max_chunk = max(max_chunk_raw, chunk_floor) or 1.0
        for fused_result in chunk_fused:
            current = _read_rrf_score(fused_result)
            _write_rrf_score(fused_result, (current / max_chunk) * chunk_w)

        fact_floor = float(self.config.fact_layer_min_floor)
        max_fact_raw = max((float(h.score) for h in fact_hits), default=0.0)
        max_fact = max(max_fact_raw, fact_floor) or 1.0
        fact_fused = [_fused_from_fact_hit(hit, fact_w, denom=max_fact) for hit in fact_hits]

        # Merge by weighted-and-normalised score.
        combined: list[Any] = list(chunk_fused) + list(fact_fused)
        combined.sort(key=_read_rrf_score, reverse=True)

        # Issue #455 — cross-layer dedup. When a fact row and a chunk
        # row describe the same entity, keep the higher-scored row.
        # Stops the same signal occupying two top-K slots.
        if self.config.cross_layer_dedup_enabled:
            combined = _cross_layer_dedup(combined)
        return combined

    def _filter_by_collections(self, fused: list[Any], collections: list[str] | None) -> list[Any]:
        """Enforce the final collection-scope invariant after backend fusion.

        Backend adapters receive the same ``collections`` filter, but scoped
        output must stay correct even if a backend/cached leg returns stale or
        out-of-scope rows. ``collections=[]`` is a real resolved scope ("no
        readable collections"), so it intentionally filters to an empty list.
        """
        if collections is None:
            return fused
        allowed = set(collections)
        return [row for row in fused if _read_collection(row) in allowed]

    def _enrich_chunk_dates(self, fused: list) -> None:
        """Fill in ``chunk_date`` on each fused result so date-aware boosts see it."""
        if not fused:
            return
        try:
            paths = [getattr(r, "path", "") for r in fused]
            chunk_dates = self.bm25.get_chunk_dates(paths)
            for r in fused:
                cd = chunk_dates.get(getattr(r, "path", ""))
                if cd and not getattr(r, "chunk_date", ""):
                    r.chunk_date = cd
        except Exception as e:
            _logger.warning("pipeline: chunk_date enrichment failed — %s", e)

    def _apply_boosts(self, fused: list, query: str, intent: QueryIntent, intent_confidence: float = 1.0) -> list:
        """Apply each boost in order; per-boost failures are logged and skipped.

        ``intent_confidence`` is passed into the boost context so the
        confidence-gated boost strategies (Issue #456) can decide whether
        to fire based on classifier confidence in addition to intent match.
        Defaults to 1.0 so any legacy caller invoking this method directly
        (without going through ``search()``) gets the legacy
        unconditional-fire behaviour.
        """
        context = {
            "intent": intent,
            "intent_confidence": intent_confidence,
            "query": query,
            "graph": self.graph,
            "query_date": _extract_query_date(query),
        }
        for boost in self.boosts:
            try:
                fused = boost.boost(fused, query, context)
            except Exception as e:
                _logger.warning("pipeline: boost %s failed — %s", type(boost).__name__, e)
        return fused

    def _maybe_rerank(
        self,
        query: str,
        fused: list[FusedResult],
        intent: QueryIntent,
    ) -> list[FusedResult]:
        """Run the cross-encoder reranker when wired AND applicable for intent.

        Gating rules (must ALL hold):
          - ``self.reranker`` is not None (a reranker has been injected)
          - one of:
            - ``self.config.rerank.enabled`` is True (operator force-enable), OR
            - ``intent.value`` appears in ``self.config.rerank_intents``
              (per-intent default, e.g. ``("multi_hop", "semantic")``)

        Failure-isolated: any rerank exception logs at WARNING and returns
        ``fused`` unchanged. The pipeline never raises because of rerank.

        Concurrency (PLA-272): the CPU-bound cross-encoder forward pass is the
        single largest search stage and used to run INLINE on the request
        thread — under concurrent teaming load it was the serialisation point
        (soak: effective concurrency ≈ 1). The reranker call is now routed
        through a bounded, shared executor (:func:`_default_rerank_pool`, or the
        injected ``rerank_pool`` seam) so concurrent requests' rerank overlaps
        on a controlled set of cores. The cheap gating above stays inline; only
        the forward pass is offloaded. Correctness is unchanged: the SAME
        reranker runs the SAME ``fused`` input, so a single request's output
        ranking is byte-for-byte identical — ``Future.result()`` returns the
        reranker's value and re-raises its exceptions into the failure-isolation
        handler exactly as the inline call did.

        Closes Issue 2 (Phase A) — dead-code-shaped lift. The rerank module
        + config + tests have all shipped; only the wiring into the pipeline
        was missing. The expected lift on the 2026-06-08 reflib eval is
        +0.010 to +0.020 weighted_total, concentrated on MULTI_HOP and
        SEMANTIC intents.
        """
        if self.reranker is None:
            return fused
        force_enabled = bool(self.config.rerank.enabled)
        intent_matches = intent.value in self.config.rerank_intents
        if not force_enabled and not intent_matches:
            return fused
        pool = self.rerank_pool if self.rerank_pool is not None else _default_rerank_pool()
        try:
            return pool.submit(self.reranker, query, fused).result()
        except Exception as e:
            _logger.warning("pipeline: rerank failed — %s — returning boosted order unchanged", e)
            return fused

    def _log_search(
        self,
        query: str,
        intent: QueryIntent,
        agent: str | None,
        scope: Scope,
        collections: list[str] | None,
        result: SearchResult,
    ) -> None:
        """Emit a search log entry; never raises."""
        if not self.logger:
            return
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:12]
        try:
            self.logger.log_search(
                {
                    "query_hash": query_hash,
                    "intent": intent.value,
                    "agent": agent,
                    # scope is a Scope enum (str subclass); .value for stable serialisation
                    "scope": scope.value if hasattr(scope, "value") else str(scope),
                    "collections_searched": collections or [],
                    "bm25_count": result.bm25_count,
                    "vec_count": result.vec_count,
                    "fused_count": result.fused_count,
                    "total_tokens": result.total_tokens,
                    "latency_ms": round(result.latency_ms, 1),
                    "vec_failed": result.vec_failed,
                    "fallback_used": result.fallback_used,
                    "ts": int(time.time()),
                }
            )
        except Exception as e:
            _logger.warning("pipeline: log_search failed — %s", e)


_ENTITY_GRAPH_UNAVAILABLE = (
    "Entity queries require Neo4j but the graph is unavailable. "
    "Check KAIRIX_NEO4J_URI, KAIRIX_NEO4J_USER, KAIRIX_NEO4J_PASSWORD "
    "and run `kairix onboard check` for diagnostics."
)


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent-weighted fusion helpers (Plan B-parity Capability #5).
#
# Tuple shape: (fact_weight, chunk_weight, graph_weight).
# graph_weight is reserved for the future entity-graph contribution
# inside the federation stage; today it's threaded through unused so the
# weighting matrix can grow without changing call sites.
# ---------------------------------------------------------------------------

_FUSION_WEIGHTS: dict[QueryIntent, tuple[float, float, float]] = {
    QueryIntent.ATTRIBUTE_FACT: (0.6, 0.3, 0.1),
    QueryIntent.MULTI_HOP: (0.2, 0.7, 0.1),
}
_FUSION_WEIGHTS_DEFAULT = (0.33, 0.33, 0.34)


def _fusion_weights_for_intent(intent: QueryIntent) -> tuple[float, float, float]:
    """Return the (fact, chunk, graph) weights for ``intent``.

    Anything not pinned in ``_FUSION_WEIGHTS`` falls through to the
    balanced default — SEMANTIC, KEYWORD, TEMPORAL, ENTITY, PROCEDURAL.
    """
    return _FUSION_WEIGHTS.get(intent, _FUSION_WEIGHTS_DEFAULT)


def _fused_from_fact_hit(hit: Any, fact_weight: float, *, denom: float = 1.0) -> FusedResult:
    """Adapt a ``FactHit`` to ``FusedResult`` so the boost/budget stages consume it.

    Each fact's ``record.value`` becomes the snippet body and the
    synthesised ``path`` namespaces it under ``facts://<id>`` so downstream
    consumers (logger, MCP renderer, dedup, prep's fact-snippet floor) can
    tell fact rows apart from chunk rows without sniffing field shapes — the
    ``facts://`` prefix stays the row's *type tag*.

    ``denom`` normalises the raw FactHit score against the strongest
    hit in this query's batch — same scale as the normalised chunk
    layer, so per-intent weighting is meaningful.
    """
    record = hit.record
    score = (float(hit.score) / denom) * fact_weight if denom else 0.0
    snippet = f"{record.entity} {record.attribute}: {record.value}"
    fact_uri = f"facts://{record.id}"
    return FusedResult(
        path=fact_uri,
        collection="facts",
        title=f"{record.entity} — {record.attribute}",
        snippet=snippet,
        rrf_score=score,
        in_bm25=False,
        in_vec=False,
        # PLA-261 — carry REAL provenance (#429): the conversation the fact
        # was grounded in resolves to a re-openable document path, not the
        # un-openable ``facts://<id>`` self-pointer. ``path`` keeps the
        # ``facts://`` type tag; ``source_uri`` is the resolvable breadcrumb
        # downstream SourceRefs (prep / search) cite. Falls back to
        # ``facts://<id>`` for legacy rows with no stored provenance.
        source_uri=resolve_fact_source_uri(record),
    )


def _read_rrf_score(row: Any) -> float:
    """Read ``rrf_score`` from a row that may be a dataclass or a dict."""
    if isinstance(row, dict):
        return float(row.get("rrf_score", row.get("score", 0.0)) or 0.0)
    return float(getattr(row, "rrf_score", 0.0) or 0.0)


def _read_collection(row: Any) -> str:
    """Read ``collection`` from a fused row that may be a dataclass or a dict."""
    if isinstance(row, dict):
        return str(row.get("collection", "") or "")
    return str(getattr(row, "collection", "") or "")


def _is_fact_row(row: Any) -> bool:
    """A fused row is a fact iff its synthesised path is namespaced under
    ``facts://`` (see :func:`_fused_from_fact_hit`)."""
    return str(getattr(row, "path", "") or "").startswith("facts://")


def _fact_entity_name(row: Any) -> str:
    """Return the entity name from a fact row's title.

    Title shape is ``"{entity} — {attribute}"`` (em-dash separator).
    Returns an empty string when the row isn't a fact or the title
    can't be parsed.
    """
    title = str(getattr(row, "title", "") or "")
    if not title:
        return ""
    parts = title.split(" — ", 1)
    return parts[0].strip()


def _chunk_overlaps_entity(row: Any, entity_lower: str) -> bool:
    """True iff a chunk row's title or path contains the entity name."""
    if not entity_lower:
        return False
    title = str(getattr(row, "title", "") or "").lower()
    path = str(getattr(row, "path", "") or "").lower()
    return entity_lower in title or entity_lower in path


def _collect_fact_indices(combined: list) -> list[tuple[int, str]]:
    """Return ``[(index, entity_name_lower), ...]`` for every fact row.

    Skips fact rows whose title can't be parsed into an entity — they
    have no dedup target so they pass through dedup unchanged.
    """
    out: list[tuple[int, str]] = []
    for idx, row in enumerate(combined):
        if not _is_fact_row(row):
            continue
        entity = _fact_entity_name(row).lower()
        if entity:
            out.append((idx, entity))
    return out


def _dedup_one_fact(
    combined: list,
    fact_idx: int,
    entity_lower: str,
    drop: set[int],
) -> None:
    """Decide which side of each overlap to drop for one fact row.

    Mutates ``drop`` in place. Returns once the fact is itself
    dropped (no point checking it against further chunks).
    """
    fact_score = _read_rrf_score(combined[fact_idx])
    for chunk_idx, chunk_row in enumerate(combined):
        if chunk_idx == fact_idx or chunk_idx in drop:
            continue
        if _is_fact_row(chunk_row):
            continue  # don't dedup fact vs fact
        if not _chunk_overlaps_entity(chunk_row, entity_lower):
            continue
        chunk_score = _read_rrf_score(chunk_row)
        if fact_score >= chunk_score:
            drop.add(chunk_idx)
        else:
            drop.add(fact_idx)
            return


def _cross_layer_dedup(combined: list) -> list:
    """Drop the lower-scored row when a fact and chunk describe the same entity.

    Walks every fact row, looks for chunk rows whose title or path
    contains the fact's entity name (case-insensitive), and removes
    the lower-scored side of each overlap. Both sides are evaluated
    against ``rrf_score`` (which by this point is the normalised +
    weighted fused score).

    Idempotent — running twice produces the same list. Stable for the
    surviving rows (their relative order is preserved). Handles fact
    rows whose title can't be parsed by treating them as having no
    entity name (no dedup target).
    """
    if not combined:
        return combined
    fact_indices = _collect_fact_indices(combined)
    if not fact_indices:
        return combined

    drop: set[int] = set()
    for fact_idx, entity_lower in fact_indices:
        if fact_idx in drop:
            continue
        _dedup_one_fact(combined, fact_idx, entity_lower, drop)
    return [row for idx, row in enumerate(combined) if idx not in drop]


def _write_rrf_score(row: Any, value: float) -> None:
    """Set ``rrf_score`` to ``value``; mutate in place.

    Supports both ``FusedResult`` dataclass rows (production fusion
    strategies) and plain ``dict`` rows (``FakeFusion`` in unit tests).
    """
    if isinstance(row, dict):
        row["rrf_score"] = float(value)
        return
    try:
        row.rrf_score = float(value)
    except (AttributeError, TypeError):
        # Row is read-only; degrade silently. Fact rows (synthesised as
        # FusedResult) always accept the write; this branch covers
        # immutable rows in third-party fusion strategies.
        pass


def _extract_query_date(query: str) -> datetime.date | None:
    """Best-effort extraction of an explicit calendar date from the query.

    Returns a ``datetime.date`` for the first ISO ``YYYY-MM-DD`` substring
    in the query, or ``None`` if none is present (or if parsing fails).
    Used by the boost chain to drive ``ChunkDateBoost`` recency scoring.
    Never raises.
    """
    import re

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", query)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except (ValueError, TypeError):
        return None
