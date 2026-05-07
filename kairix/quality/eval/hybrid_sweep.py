"""
Hybrid pipeline calibration sweep — grid search over RRF k, boost configs,
and retrieval modes against an independent gold suite.

Evaluates the full hybrid pipeline (BM25 + vector + RRF + boosts) with
different parameter combinations. Designed to answer:

1. Does vector search help or hurt? (BM25-only vs hybrid)
2. What RRF k constant is optimal? (10/20/40/60/100)
3. Do boost layers improve ranking? (minimal vs defaults vs tuned)
4. What boost parameter values work best?

Usage::

    kairix eval hybrid-sweep \\
        --suite suites/v2-independent-gold.yaml \\
        --output hybrid-sweep-results.csv

Requires a running kairix instance with DB, embeddings, and optionally Neo4j.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from kairix.quality.eval.constants import CATEGORY_ALIASES, CATEGORY_WEIGHTS
from kairix.quality.eval.metrics import hit_at_k_graded as compute_hit_at_k
from kairix.quality.eval.metrics import ndcg_graded as compute_ndcg
from kairix.quality.eval.metrics import reciprocal_rank_graded as compute_mrr

if TYPE_CHECKING:
    from kairix.core.protocols import Retriever
    from kairix.core.search.config import RetrievalConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sweep configuration space
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HybridSweepConfig:
    """A single configuration to evaluate in the sweep."""

    name: str
    mode: str  # "hybrid", "bm25_only", or "bm25_primary"
    rrf_k: int = 60
    entity_enabled: bool = True
    entity_factor: float = 0.20
    entity_cap: float = 2.0
    procedural_enabled: bool = True
    procedural_factor: float = 1.4
    chunk_date_enabled: bool = False
    bm25_limit: int = 20
    vec_limit: int = 10
    vec_distance_threshold: float = 0.0  # 0 = no filtering; >0 = discard vec results above this distance


def build_default_configs() -> list[HybridSweepConfig]:
    """Build the default sweep configuration space."""
    configs: list[HybridSweepConfig] = []

    # --- Baseline: BM25-only (current best = 0.749 NDCG) ---
    configs.append(
        HybridSweepConfig(
            name="bm25-only",
            mode="bm25_only",
        )
    )

    # --- RRF k sweep (hybrid, minimal boosts) ---
    for k in [10, 20, 40, 60, 100]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-k{k}-minimal",
                mode="hybrid",
                rrf_k=k,
                entity_enabled=False,
                procedural_enabled=False,
            )
        )

    # --- RRF k sweep (hybrid, default boosts) ---
    for k in [20, 40, 60]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-k{k}-defaults",
                mode="hybrid",
                rrf_k=k,
                entity_enabled=True,
                procedural_enabled=True,
            )
        )

    # --- Entity boost factor sweep (k=60) ---
    for ef in [0.10, 0.20, 0.30, 0.50]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-entity-f{ef}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=True,
                entity_factor=ef,
                procedural_enabled=True,
            )
        )

    # --- Entity cap sweep ---
    for cap in [1.5, 2.0, 3.0]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-entity-cap{cap}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=True,
                entity_cap=cap,
                procedural_enabled=True,
            )
        )

    # --- Procedural factor sweep ---
    for pf in [1.0, 1.2, 1.4, 1.6, 2.0]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-proc-f{pf}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=True,
                procedural_enabled=True,
                procedural_factor=pf,
            )
        )

    # --- BM25 limit sweep (more/fewer candidates for RRF) ---
    for lim in [10, 20, 30, 50]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-bm25lim{lim}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=False,
                procedural_enabled=False,
                bm25_limit=lim,
            )
        )

    # --- Vector limit sweep ---
    for vlim in [5, 10, 20, 30]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-vlim{vlim}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=False,
                procedural_enabled=False,
                vec_limit=vlim,
            )
        )

    # --- Distance-gated hybrid (filter poor vector results before RRF) ---
    for threshold in [0.3, 0.4, 0.5]:
        configs.append(
            HybridSweepConfig(
                name=f"hybrid-gate-d{threshold}",
                mode="hybrid",
                rrf_k=60,
                entity_enabled=False,
                procedural_enabled=False,
                vec_distance_threshold=threshold,
            )
        )

    # --- BM25-primary mode (BM25 ranked first, vector-only appended) ---
    for vlim in [5, 10, 20]:
        configs.append(
            HybridSweepConfig(
                name=f"bm25primary-v{vlim}",
                mode="bm25_primary",
                bm25_limit=20,
                vec_limit=vlim,
                entity_enabled=False,
                procedural_enabled=False,
            )
        )

    # BM25-primary with more BM25 candidates
    configs.append(
        HybridSweepConfig(
            name="bm25primary-bm30-v10",
            mode="bm25_primary",
            bm25_limit=30,
            vec_limit=10,
            entity_enabled=False,
            procedural_enabled=False,
        )
    )

    # --- Best combo candidates (informed by individual sweeps) ---
    configs.append(
        HybridSweepConfig(
            name="hybrid-tuned-a",
            mode="hybrid",
            rrf_k=20,
            entity_enabled=True,
            entity_factor=0.30,
            entity_cap=2.0,
            procedural_enabled=True,
            procedural_factor=1.4,
            bm25_limit=30,
            vec_limit=10,
        )
    )
    configs.append(
        HybridSweepConfig(
            name="hybrid-tuned-b",
            mode="hybrid",
            rrf_k=40,
            entity_enabled=True,
            entity_factor=0.20,
            entity_cap=2.0,
            procedural_enabled=True,
            procedural_factor=1.2,
            bm25_limit=20,
            vec_limit=20,
        )
    )

    return configs


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class HybridSweepResult:
    """Result of evaluating a single hybrid configuration."""

    config: HybridSweepConfig
    weighted_total: float = 0.0
    ndcg_at_10: float = 0.0
    hit_at_5: float = 0.0
    mrr_at_10: float = 0.0
    category_scores: dict[str, float] = field(default_factory=dict)
    n_cases: int = 0
    n_vec_failed: int = 0
    avg_bm25_count: float = 0.0
    avg_vec_count: float = 0.0
    avg_fused_count: float = 0.0
    avg_latency_ms: float = 0.0
    duration_s: float = 0.0


@dataclass
class HybridSweepReport:
    """Summary of a full hybrid calibration sweep."""

    results: list[HybridSweepResult] = field(default_factory=list)
    best: HybridSweepResult | None = None
    total_configs: int = 0
    total_duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Retrieval — delegates to the main search pipeline (DRY)
# ---------------------------------------------------------------------------


def sweep_config_to_retrieval_config(cfg: HybridSweepConfig) -> RetrievalConfig:
    """Convert a sweep config to a RetrievalConfig for the main search pipeline."""
    from kairix.core.search.config import (
        EntityBoostConfig,
        ProceduralBoostConfig,
        RetrievalConfig,
        TemporalBoostConfig,
    )

    return RetrievalConfig(
        fusion_strategy=("bm25_primary" if cfg.mode in ("bm25_only", "bm25_primary") else "rrf"),
        rrf_k=cfg.rrf_k,
        bm25_limit=cfg.bm25_limit,
        vec_limit=cfg.vec_limit,
        skip_vector=(cfg.mode == "bm25_only"),
        entity=EntityBoostConfig(
            enabled=cfg.entity_enabled,
            factor=cfg.entity_factor,
            cap=cfg.entity_cap,
        ),
        procedural=ProceduralBoostConfig(
            enabled=cfg.procedural_enabled,
            factor=cfg.procedural_factor,
        ),
        temporal=TemporalBoostConfig(
            chunk_date_boost_enabled=cfg.chunk_date_enabled,
        ),
    )


class _DefaultHybridRetriever:
    """Production Retriever for the hybrid sweep — delegates to the shared
    eval retrieval module's `retrieve(system="hybrid", ...)` entry point.

    Conforms to ``kairix.core.protocols.Retriever``. Wrapping the
    module-level call as an injectable object lets tests substitute a
    `FakeRetriever` via the `retriever=` kwarg on `evaluate_single_config`
    and `sweep_hybrid_params` without monkeypatching module state.
    """

    def retrieve(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        cfg: Any = None,
    ) -> Any:
        from kairix.quality.eval.retrieval import retrieve

        return retrieve(
            query=query,
            system="hybrid",
            config=cfg,
            collections=collections,
        )


# DEPRECATED — retained as a thin module-level shim for one release window
# so any external callers / scripts that imported `_retrieve` directly do
# not break. Phase 4 of #143 removes this entirely; new code should depend
# on the `Retriever` protocol via constructor / kwarg injection instead.
def _retrieve(
    query: str,
    collections: list[str] | None,
    cfg: HybridSweepConfig,
) -> tuple[list[str], dict[str, Any]]:
    """Run retrieval via the shared retrieval module (deprecated shim)."""
    config = sweep_config_to_retrieval_config(cfg)
    result = _DefaultHybridRetriever().retrieve(query, collections=collections, cfg=config)
    return result.paths, {
        "bm25_count": result.meta.get("bm25_count", 0),
        "vec_count": result.meta.get("vec_count", 0),
        "fused_count": result.meta.get("fused_count", 0),
        "vec_failed": result.meta.get("vec_failed", False),
    }


# ---------------------------------------------------------------------------
# Extracted helpers for sweep_hybrid_params (reduce cognitive complexity)
# ---------------------------------------------------------------------------


def load_and_validate_suite(
    suite_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load YAML suite, extract NDCG cases with gold references.

    Returns (all_cases, ndcg_cases). Both empty on load failure.
    """
    with open(suite_path) as f:
        suite_data = yaml.safe_load(f)

    cases = suite_data.get("cases", [])
    ndcg_cases = [c for c in cases if c.get("score_method") == "ndcg" and (c.get("gold_titles") or c.get("gold_paths"))]
    return cases, ndcg_cases


@dataclass
class _RetrievalAccumulator:
    """Running counter state for a single-config sweep pass.

    Carries the per-query metric / metadata totals through `evaluate_single_config`
    so the aggregator (`aggregate_ndcg_for_config`) takes one struct rather
    than 11 positional arguments. The dataclass is module-private — production
    callers don't construct it directly.
    """

    scores: list[float] = field(default_factory=list)
    hits: list[bool] = field(default_factory=list)
    mrrs: list[float] = field(default_factory=list)
    category_ndcg: dict[str, list[float]] = field(default_factory=dict)
    total_bm25: int = 0
    total_vec: int = 0
    total_fused: int = 0
    total_latency: float = 0.0
    n_vec_failed: int = 0


def evaluate_single_config(
    cfg: HybridSweepConfig,
    ndcg_cases: list[dict[str, Any]],
    collections: list[str] | None,
    category_weights: dict[str, float],
    *,
    retriever: Retriever | None = None,
) -> HybridSweepResult:
    """Run all queries for one config and return the result.

    Args:
        cfg:               Sweep configuration to evaluate.
        ndcg_cases:        Filtered NDCG-scored suite cases.
        collections:       Collection-name filter passed to the retriever.
        category_weights:  Category → weight mapping for the weighted total.
        retriever:         Optional `Retriever` protocol implementation. Defaults
                           to the production `_DefaultHybridRetriever` when None.
                           Tests inject a `FakeRetriever` here to avoid spinning
                           up the real search pipeline.
    """
    t_start = time.monotonic()
    if retriever is None:
        retriever = _DefaultHybridRetriever()

    retrieval_config = sweep_config_to_retrieval_config(cfg)
    acc = _RetrievalAccumulator()

    for case in ndcg_cases:
        query = case["query"]
        gold = case.get("gold_titles") or case.get("gold_paths", [])
        raw_category = case.get("category", "recall")
        category = CATEGORY_ALIASES.get(raw_category, raw_category)

        t_q_start = time.monotonic()
        result = retriever.retrieve(query, collections=collections, cfg=retrieval_config)
        t_q_end = time.monotonic()
        acc.total_latency += (t_q_end - t_q_start) * 1000.0

        paths = list(result.paths)
        meta = result.meta if hasattr(result, "meta") else {}
        acc.total_bm25 += meta.get("bm25_count", 0)
        acc.total_vec += meta.get("vec_count", 0)
        acc.total_fused += meta.get("fused_count", 0)
        if meta.get("vec_failed"):
            acc.n_vec_failed += 1

        ndcg = compute_ndcg(paths, gold)
        hit = compute_hit_at_k(paths, gold)
        mrr = compute_mrr(paths, gold)

        acc.scores.append(ndcg)
        acc.hits.append(hit)
        acc.mrrs.append(mrr)

        if category not in acc.category_ndcg:
            acc.category_ndcg[category] = []
        acc.category_ndcg[category].append(ndcg)

    return aggregate_ndcg_for_config(
        acc,
        len(ndcg_cases),
        cfg,
        t_start,
        category_weights,
    )


def aggregate_ndcg_for_config(
    acc: _RetrievalAccumulator,
    n: int,
    cfg: HybridSweepConfig,
    t_start: float,
    category_weights: dict[str, float],
) -> HybridSweepResult:
    """Compute averages and build a HybridSweepResult for one config.

    Takes the running `_RetrievalAccumulator` instead of 11 positional totals
    to keep the parameter count manageable. (#143 Phase 2b refactor.)
    """
    cat_scores = {cat: sum(s) / len(s) if s else 0.0 for cat, s in acc.category_ndcg.items()}
    weighted_total = sum(cat_scores.get(cat, 0.0) * weight for cat, weight in category_weights.items())
    return HybridSweepResult(
        config=cfg,
        weighted_total=weighted_total,
        ndcg_at_10=sum(acc.scores) / n if n else 0.0,
        hit_at_5=sum(acc.hits) / n if n else 0.0,
        mrr_at_10=sum(acc.mrrs) / n if n else 0.0,
        category_scores=cat_scores,
        n_cases=n,
        n_vec_failed=acc.n_vec_failed,
        avg_bm25_count=acc.total_bm25 / n if n else 0.0,
        avg_vec_count=acc.total_vec / n if n else 0.0,
        avg_fused_count=acc.total_fused / n if n else 0.0,
        avg_latency_ms=acc.total_latency / n if n else 0.0,
        duration_s=time.monotonic() - t_start,
    )


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


def sweep_hybrid_params(
    suite_path: Path,
    output_path: Path | None = None,
    configs: list[HybridSweepConfig] | None = None,
    collections_override: list[str] | None = None,
    *,
    retriever: Retriever | None = None,
) -> HybridSweepReport:
    """
    Grid search over hybrid pipeline configurations.

    For each configuration, runs all suite queries through the full pipeline
    (or BM25-only) and computes NDCG@10, Hit@5, MRR@10 against the gold suite.

    Args:
        suite_path:            Path to benchmark suite YAML (independent gold).
        output_path:           Optional CSV output for results.
        configs:               Configurations to evaluate. Defaults to build_default_configs().
        collections_override:  Explicit collection list. Overrides suite metadata when set.
        retriever:             Optional `Retriever` protocol implementation. Defaults
                               to the production `_DefaultHybridRetriever` when None.
                               Tests inject a `FakeRetriever` to bypass the real
                               search pipeline.

    Returns:
        HybridSweepReport with results sorted by weighted_total descending.
    """
    if configs is None:
        configs = build_default_configs()

    cases, ndcg_cases = load_and_validate_suite(suite_path)
    if not cases:
        logger.error("hybrid_sweep: no cases in suite %s", suite_path)
        return HybridSweepReport()
    if not ndcg_cases:
        logger.error("hybrid_sweep: no ndcg-scored cases with gold in suite")
        return HybridSweepReport()

    logger.info(
        "hybrid_sweep: %d configs x %d cases = %d evaluations",
        len(configs),
        len(ndcg_cases),
        len(configs) * len(ndcg_cases),
    )

    # Determine collections: CLI override > suite metadata > None (all)
    with open(suite_path) as f:
        suite_data = yaml.safe_load(f)
    collections = collections_override or suite_data.get("collections")

    report = HybridSweepReport()
    report.total_configs = len(configs)
    t_total_start = time.monotonic()

    for cfg in configs:
        result = evaluate_single_config(
            cfg,
            ndcg_cases,
            collections,
            CATEGORY_WEIGHTS,
            retriever=retriever,
        )
        report.results.append(result)

        logger.info(
            "hybrid_sweep: %-30s → weighted=%.4f NDCG=%.4f Hit@5=%.3f vec_fail=%d latency=%.0fms (%ds)",
            cfg.name,
            result.weighted_total,
            result.ndcg_at_10,
            result.hit_at_5,
            result.n_vec_failed,
            result.avg_latency_ms,
            int(result.duration_s),
        )

    # Sort by weighted total
    report.results.sort(key=lambda r: r.weighted_total, reverse=True)
    report.best = report.results[0] if report.results else None
    report.total_duration_s = time.monotonic() - t_total_start

    # Write CSV
    if output_path and report.results:
        _write_csv(output_path, report)

    # Print summary table
    _print_summary(report)

    return report


def _write_csv(output_path: Path, report: HybridSweepReport) -> None:
    """Write sweep results to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cat_names = sorted(CATEGORY_WEIGHTS.keys())
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "config_name",
                "mode",
                "rrf_k",
                "entity_enabled",
                "entity_factor",
                "entity_cap",
                "proc_enabled",
                "proc_factor",
                "bm25_limit",
                "vec_limit",
                "weighted_total",
                "ndcg_at_10",
                "hit_at_5",
                "mrr_at_10",
                *cat_names,
                "n_vec_failed",
                "avg_bm25",
                "avg_vec",
                "avg_fused",
                "avg_latency_ms",
                "duration_s",
            ]
        )
        for r in report.results:
            c = r.config
            writer.writerow(
                [
                    c.name,
                    c.mode,
                    c.rrf_k,
                    c.entity_enabled,
                    c.entity_factor,
                    c.entity_cap,
                    c.procedural_enabled,
                    c.procedural_factor,
                    c.bm25_limit,
                    c.vec_limit,
                    f"{r.weighted_total:.4f}",
                    f"{r.ndcg_at_10:.4f}",
                    f"{r.hit_at_5:.4f}",
                    f"{r.mrr_at_10:.4f}",
                    *[f"{r.category_scores.get(cat, 0):.4f}" for cat in cat_names],
                    r.n_vec_failed,
                    f"{r.avg_bm25_count:.1f}",
                    f"{r.avg_vec_count:.1f}",
                    f"{r.avg_fused_count:.1f}",
                    f"{r.avg_latency_ms:.0f}",
                    f"{r.duration_s:.1f}",
                ]
            )


def _print_summary(report: HybridSweepReport) -> None:
    """Print a formatted summary table to the logger."""
    if not report.results:
        return

    logger.info("")
    logger.info("=" * 100)
    logger.info(
        "HYBRID CALIBRATION SWEEP — %d configs, %.0fs total",
        report.total_configs,
        report.total_duration_s,
    )
    logger.info("=" * 100)
    logger.info(
        "%-30s  %6s  %6s  %6s  %6s  %5s  %6s",
        "Config",
        "Weight",
        "NDCG",
        "Hit@5",
        "MRR",
        "VecF",
        "Lat ms",
    )
    logger.info("-" * 100)

    for r in report.results[:20]:  # Top 20
        logger.info(
            "%-30s  %6.4f  %6.4f  %6.3f  %6.4f  %5d  %6.0f",
            r.config.name,
            r.weighted_total,
            r.ndcg_at_10,
            r.hit_at_5,
            r.mrr_at_10,
            r.n_vec_failed,
            r.avg_latency_ms,
        )

    if report.best:
        logger.info("-" * 100)
        b = report.best
        logger.info("BEST: %s", b.config.name)
        logger.info(
            "  Mode: %s | RRF k=%d | Entity: %s (f=%.2f, cap=%.1f) | Proc: %s (f=%.1f)",
            b.config.mode,
            b.config.rrf_k,
            b.config.entity_enabled,
            b.config.entity_factor,
            b.config.entity_cap,
            b.config.procedural_enabled,
            b.config.procedural_factor,
        )
        logger.info(
            "  Weighted=%.4f  NDCG=%.4f  Hit@5=%.3f  MRR=%.4f",
            b.weighted_total,
            b.ndcg_at_10,
            b.hit_at_5,
            b.mrr_at_10,
        )
        logger.info(
            "  Categories: %s",
            "  ".join(
                f"{cat}={b.category_scores.get(cat, 0):.3f}"
                for cat in sorted(CATEGORY_WEIGHTS.keys())
                if CATEGORY_WEIGHTS[cat] > 0
            ),
        )
    logger.info("=" * 100)
