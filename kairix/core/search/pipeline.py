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

import hashlib
import logging
import time
from dataclasses import dataclass, field

from kairix.core.protocols import (
    BoostStrategy,
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
        try:
            intent = self.classifier.classify(query)  # type: ignore[union-attr]
        except Exception as e:
            _logger.warning("pipeline: classify failed — %s", e)
            intent = QueryIntent.SEMANTIC

        # 2. Entity intent requires graph
        if intent == QueryIntent.ENTITY and not self.graph.available:
            return SearchResult(
                query=query,
                intent=intent,
                error=(
                    "Entity queries require Neo4j but the graph is unavailable. "
                    "Check KAIRIX_NEO4J_URI, KAIRIX_NEO4J_USER, KAIRIX_NEO4J_PASSWORD "
                    "and run `kairix onboard check` for diagnostics."
                ),
            )

        # 3. Dispatch BM25 + vector search
        cfg = self.config
        bm25_results: list[dict] = []
        vec_results: list[dict] = []
        vec_failed = False

        try:
            bm25_results = self.bm25.search(
                query,
                collections=collections,
                limit=cfg.bm25_limit,
            )
        except Exception as e:
            _logger.warning("pipeline: BM25 search failed — %s", e)

        if not cfg.skip_vector:
            try:
                vec_results = self.vector.search(
                    query,
                    collections=collections,
                    limit=cfg.vec_limit,
                )
            except Exception as e:
                _logger.warning("pipeline: vector search failed — %s", e)
                vec_failed = True

            if not vec_results:
                vec_failed = True

        # 4. Fuse
        fused = self.fusion.fuse(bm25_results, vec_results)

        # 5. Boost chain
        context = {"intent": intent, "query": query, "graph": self.graph}
        for boost in self.boosts:
            try:
                fused = boost.boost(fused, query, context)
            except Exception as e:
                _logger.warning("pipeline: boost %s failed — %s", type(boost).__name__, e)

        # 6. Budget
        budgeted = apply_budget(fused, budget=budget)
        total_tokens = sum(getattr(r, "token_estimate", 0) for r in budgeted)
        tiers_used = sorted({getattr(r, "tier", "L2") for r in budgeted})

        t_end = time.monotonic()
        latency_ms = (t_end - t_start) * 1000.0

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

        # 7. Log
        if self.logger:
            query_hash = hashlib.sha256(query.encode()).hexdigest()[:12]
            try:
                self.logger.log_search(
                    {
                        "query_hash": query_hash,
                        "intent": intent.value,
                        "agent": agent,
                        "scope": scope,
                        "bm25_count": len(bm25_results),
                        "vec_count": len(vec_results),
                        "fused_count": len(fused),
                        "total_tokens": total_tokens,
                        "latency_ms": round(latency_ms, 1),
                        "vec_failed": vec_failed,
                        "ts": int(time.time()),
                    }
                )
            except Exception as e:
                _logger.warning("pipeline: log_search failed — %s", e)

        return result


_logger = logging.getLogger(__name__)
