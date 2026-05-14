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
import time
from dataclasses import dataclass, field

from kairix.core.protocols import (
    BoostStrategy,
    CollectionResolver,
    FusionStrategy,
    GraphRepository,
    SearchLogger,
)
from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.budget import apply_budget
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.scope import Scope

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Full result from the search pipeline."""

    query: str
    intent: QueryIntent
    results: list = field(default_factory=list)

    # Diagnostic info
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
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

    def search(
        self,
        query: str,
        budget: int = 3000,
        scope: Scope = Scope.SHARED_AGENT,
        agent: str | None = None,
        collections: list[str] | None = None,
    ) -> SearchResult:
        """Execute the full search pipeline using composed components.

        Never raises — returns SearchResult with empty results on any failure.
        """
        t_start = time.monotonic()

        # 1. Classify intent
        intent = self._classify(query)

        # 2. Entity intent requires graph
        if intent == QueryIntent.ENTITY and not self.graph.available:
            return SearchResult(query=query, intent=intent, error=_ENTITY_GRAPH_UNAVAILABLE)

        # 3. Resolve collections via the injected CollectionResolver
        collections, resolve_error = self._resolve_collections(collections, agent, scope)
        if resolve_error is not None:
            return SearchResult(query=query, intent=intent, error=resolve_error)

        # 4. Dispatch BM25 + vector search
        bm25_results, vec_results, vec_failed = self._dispatch_backends(query, collections)

        # 5. Fuse
        fused = self._fuse(bm25_results, vec_results)

        # 5b. Enrich each fused result with chunk_date metadata so the boost
        # chain (specifically ChunkDateBoost) can score by recency. Source
        # of truth is DocumentRepository.get_chunk_dates, exposed here via
        # the BM25 backend — boost adapters never reach into the repo.
        self._enrich_chunk_dates(fused)

        # 6. Boost chain
        fused = self._apply_boosts(fused, query, intent)

        # 7. Budget
        budgeted = apply_budget(fused, budget=budget)
        total_tokens = sum(getattr(r, "token_estimate", 0) for r in budgeted)
        tiers_used = sorted({getattr(r, "tier", "L2") for r in budgeted})

        latency_ms = (time.monotonic() - t_start) * 1000.0

        result = SearchResult(
            query=query,
            intent=intent,
            results=budgeted,
            bm25_count=len(bm25_results),
            vec_count=len(vec_results),
            fused_count=len(fused),
            collections=collections or [],
            tiers_used=tiers_used,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            vec_failed=vec_failed,
            fallback_used=not bm25_results and bool(vec_results),
        )

        # 8. Log
        self._log_search(query, intent, agent, scope, collections, result)

        return result

    def _classify(self, query: str) -> QueryIntent:
        """Classify intent; fall back to SEMANTIC on any classifier failure."""
        try:
            return self.classifier.classify(query)  # type: ignore[union-attr] — classifier may be None when graph backend unavailable; guarded by try
        except Exception as e:
            _logger.warning("pipeline: classify failed — %s", e)
            return QueryIntent.SEMANTIC

    def _resolve_collections(
        self,
        collections: list[str] | None,
        agent: str | None,
        scope: Scope,
    ) -> tuple[list[str] | None, str | None]:
        """Resolve collections via the injected resolver when not pre-supplied."""
        if collections is not None or self.resolver is None:
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
    ) -> tuple[list[dict], list[dict], bool]:
        """Run BM25 + vector search; isolate each failure so one can't break the other."""
        cfg = self.config
        bm25_results: list[dict] = []
        try:
            bm25_results = self.bm25.search(query, collections=collections, limit=cfg.bm25_limit)
        except Exception as e:
            _logger.warning("pipeline: BM25 search failed — %s", e)

        vec_results, vec_failed = self._dispatch_vector(query, collections)
        return bm25_results, vec_results, vec_failed

    def _dispatch_vector(
        self,
        query: str,
        collections: list[str] | None,
    ) -> tuple[list[dict], bool]:
        """Vector backend dispatch with skip-flag and failure-vs-empty distinction.

        ``vec_failed`` reflects backend failure only — operators consume
        this field to triage real outages. Empty-and-failed conflation
        produced false-positive alerts.
        """
        if self.config.skip_vector:
            return [], False
        try:
            return self.vector.search(query, collections=collections, limit=self.config.vec_limit), False
        except Exception as e:
            _logger.warning("pipeline: vector search failed — %s", e)
            return [], True

    def _fuse(self, bm25_results: list[dict], vec_results: list[dict]) -> list:
        """Fuse BM25 + vector results; on fusion failure return empty list."""
        try:
            return self.fusion.fuse(bm25_results, vec_results)
        except Exception as e:
            _logger.warning("pipeline: fusion failed — %s — falling back to empty fused list", e)
            return []

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

    def _apply_boosts(self, fused: list, query: str, intent: QueryIntent) -> list:
        """Apply each boost in order; per-boost failures are logged and skipped."""
        context = {
            "intent": intent,
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
