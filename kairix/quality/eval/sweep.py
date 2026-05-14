"""
BM25 parameter sweep — grid search over column weights and query styles.

Runs each configuration against a benchmark suite and reports NDCG@10,
Hit@5, MRR@10, and per-category scores to identify the optimal BM25
configuration.

Usage::

    kairix eval sweep \\
        --suite suites/v2-independent-gold.yaml \\
        --output sweep-results.csv

No LLM API calls — runs entirely locally against the kairix DB.
"""

from __future__ import annotations

import csv
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.quality.eval.constants import CATEGORY_ALIASES as _CATEGORY_ALIASES
from kairix.quality.eval.constants import CATEGORY_WEIGHTS as _CATEGORY_WEIGHTS
from kairix.quality.eval.metrics import hit_at_k_graded as _compute_hit_at_k
from kairix.quality.eval.metrics import ndcg_graded as _compute_ndcg
from kairix.quality.eval.metrics import reciprocal_rank_graded as _compute_mrr

logger = logging.getLogger(__name__)

# Default parameter space
DEFAULT_WEIGHT_CONFIGS: list[tuple[float, float, float]] = [
    (1.0, 1.0, 1.0),  # equal (current kairix)
    (10.0, 1.0, 1.0),  # filepath-heavy weights
    (10.0, 5.0, 1.0),  # filepath + title boost
    (1.0, 5.0, 1.0),  # title-heavy
    (1.0, 3.0, 1.0),  # title-moderate
    (2.0, 5.0, 1.0),  # filepath slight + title heavy
    (5.0, 3.0, 1.0),  # filepath moderate + title moderate
    (0.5, 1.0, 1.0),  # de-weight filepath
    (1.0, 1.0, 0.5),  # de-weight body
    (5.0, 5.0, 1.0),  # both boosted
]

DEFAULT_QUERY_STYLES: list[str] = [
    "bare",  # `term1 term2` (implicit AND)
    "prefix",  # `"term1"* AND "term2"*` (prefix-style)
    "quoted",  # `"term1" AND "term2"` (exact match)
]


@dataclass
class SweepResult:
    """Result of a single sweep configuration."""

    weights: tuple[float, float, float]
    query_style: str
    weighted_total: float = 0.0
    ndcg_at_10: float = 0.0
    hit_at_5: float = 0.0
    mrr_at_10: float = 0.0
    category_scores: dict[str, float] = field(default_factory=dict)
    n_cases: int = 0
    duration_s: float = 0.0


@dataclass
class SweepReport:
    """Summary of a full parameter sweep."""

    results: list[SweepResult] = field(default_factory=list)
    best: SweepResult | None = None
    total_configs: int = 0
    total_duration_s: float = 0.0


def _build_query(query: str, style: str) -> str | None:
    """Build FTS5 query string in the specified style.

    Delegates to :func:`kairix.core.search.tokenizer.tokenize_fts_query`.
    """
    from kairix.core.search.tokenizer import tokenize_fts_query

    return tokenize_fts_query(query, style=style)


def _validate_weights(weights: tuple[float, float, float]) -> None:
    """Raise ValueError if any BM25 weight is non-finite or non-positive.

    Guards the f-string injection at ``_bm25_search_config`` against nan/inf
    leaking into the SQL ORDER BY clause. SQLite's bm25() accepts those as
    legal floats but produces nondeterministic ordering, which silently
    invalidates the sweep results.
    """
    w_fp, w_title, w_doc = weights
    for label, w in (("filepath", w_fp), ("title", w_title), ("doc", w_doc)):
        if not math.isfinite(w) or w <= 0:
            raise ValueError(
                f"sweep: BM25 weight {label}={w!r} must be finite and positive; weights tuple = (filepath, title, doc)"
            )


def _bm25_search_config(
    db: sqlite3.Connection,
    query: str,
    weights: tuple[float, float, float],
    query_style: str,
    limit: int = 20,
) -> list[str]:
    """
    Run BM25 search with specific config. Returns list of paths (ranked order).
    """
    fts_query = _build_query(query, query_style)
    if not fts_query:
        return []

    w_fp, w_title, w_doc = weights
    # safe: float() cast on bm25 weights (validated finite/positive at sweep entry);
    # no ? binding available for bm25 args
    try:
        rows = db.execute(
            f"""
            SELECT d.path
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            JOIN content c ON c.hash = d.hash
            WHERE documents_fts MATCH ?
              AND d.active = 1
            ORDER BY bm25(documents_fts, {float(w_fp)}, {float(w_title)}, {float(w_doc)}) ASC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.debug("sweep: FTS query failed — %s (query=%r)", e, query[:40])
        return []


def _load_ndcg_cases(suite_path: Path) -> list[dict[str, Any]]:
    """Load suite YAML and return ndcg-scored cases that have gold labels.

    Returns ``[]`` (with a logged error) on empty suite or no eligible
    cases. ``gold_titles`` is preferred over ``gold_paths`` per the
    suite-format conventions.
    """
    with open(suite_path) as f:
        suite_data = yaml.safe_load(f)
    cases = suite_data.get("cases", [])
    if not cases:
        logger.error("sweep: no cases in suite %s", suite_path)
        return []
    ndcg_cases = [c for c in cases if c.get("score_method") == "ndcg" and (c.get("gold_titles") or c.get("gold_paths"))]
    if not ndcg_cases:
        logger.error("sweep: no ndcg-scored cases with gold_titles or gold_paths in suite")
    return ndcg_cases


def _evaluate_config(
    db: sqlite3.Connection,
    weights: tuple[float, float, float],
    style: str,
    ndcg_cases: list[dict[str, Any]],
) -> SweepResult:
    """Run one (weights, style) configuration over all cases and return a SweepResult."""
    t_start = time.monotonic()
    ndcg_scores: list[float] = []
    hit_scores: list[bool] = []
    mrr_scores: list[float] = []
    category_ndcg: dict[str, list[float]] = {}

    for case in ndcg_cases:
        query = case["query"]
        gold = case.get("gold_titles") or case.get("gold_paths", [])
        raw_category = case.get("category", "recall")
        category = _CATEGORY_ALIASES.get(raw_category, raw_category)

        paths = _bm25_search_config(db, query, weights, style, limit=20)

        ndcg_scores.append(_compute_ndcg(paths, gold))
        hit_scores.append(_compute_hit_at_k(paths, gold))
        mrr_scores.append(_compute_mrr(paths, gold))
        category_ndcg.setdefault(category, []).append(ndcg_scores[-1])

    cat_scores = {cat: sum(scores) / len(scores) if scores else 0.0 for cat, scores in category_ndcg.items()}
    weighted_total = sum(cat_scores.get(cat, 0.0) * weight for cat, weight in _CATEGORY_WEIGHTS.items())

    return SweepResult(
        weights=weights,
        query_style=style,
        weighted_total=weighted_total,
        ndcg_at_10=sum(ndcg_scores) / len(ndcg_scores) if ndcg_scores else 0.0,
        hit_at_5=sum(hit_scores) / len(hit_scores) if hit_scores else 0.0,
        mrr_at_10=sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0.0,
        category_scores=cat_scores,
        n_cases=len(ndcg_cases),
        duration_s=time.monotonic() - t_start,
    )


def _resolve_db_path(db_path: Path | None) -> Path:
    """Return an explicit db_path or resolve from the env via ``get_db_path``."""
    if db_path is not None:
        return Path(db_path)
    from kairix.core.db import get_db_path

    return Path(get_db_path())


def _write_sweep_csv(output_path: Path, report: SweepReport) -> None:
    """Serialise a SweepReport to CSV with category columns in stable order."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cats = sorted(_CATEGORY_WEIGHTS.keys())
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "fp_weight",
                "title_weight",
                "doc_weight",
                "query_style",
                "weighted_total",
                "ndcg_at_10",
                "hit_at_5",
                "mrr_at_10",
                *cats,
            ]
        )
        for r in report.results:
            writer.writerow(
                [
                    r.weights[0],
                    r.weights[1],
                    r.weights[2],
                    r.query_style,
                    f"{r.weighted_total:.4f}",
                    f"{r.ndcg_at_10:.4f}",
                    f"{r.hit_at_5:.4f}",
                    f"{r.mrr_at_10:.4f}",
                    *[f"{r.category_scores.get(cat, 0):.4f}" for cat in cats],
                ]
            )


def sweep_bm25_params(
    suite_path: Path,
    output_path: Path | None = None,
    weight_configs: list[tuple[float, float, float]] | None = None,
    query_styles: list[str] | None = None,
    db_path: Path | None = None,
) -> SweepReport:
    """Grid search over BM25 column weights and query styles.

    For each (weights, style) configuration, runs all suite queries via direct
    FTS5 and computes NDCG@10 against the suite's gold_titles.

    Args:
        suite_path:      Path to benchmark suite YAML.
        output_path:     Optional CSV output for results.
        weight_configs:  Column weight tuples (filepath, title, doc).
        query_styles:    Query construction styles to test.
        db_path:         Optional explicit kairix DB path. Defaults to
                         ``get_db_path()`` (env-resolved). Tests pass a
                         tmp_path-rooted DB to avoid env mutation.

    Returns:
        SweepReport with results sorted by weighted_total descending.
    """
    weight_configs = weight_configs or DEFAULT_WEIGHT_CONFIGS
    query_styles = query_styles or DEFAULT_QUERY_STYLES

    # Fail-fast on bad weights before we open the DB or scan the suite.
    for cfg in weight_configs:
        _validate_weights(cfg)

    ndcg_cases = _load_ndcg_cases(suite_path)
    if not ndcg_cases:
        return SweepReport()

    logger.info(
        "sweep: %d configs x %d cases = %d evaluations",
        len(weight_configs) * len(query_styles),
        len(ndcg_cases),
        len(weight_configs) * len(query_styles) * len(ndcg_cases),
    )

    from kairix.core.db import open_db

    db = open_db(_resolve_db_path(db_path))
    db.row_factory = sqlite3.Row

    report = SweepReport()
    report.total_configs = len(weight_configs) * len(query_styles)
    t_total_start = time.monotonic()

    try:
        for weights in weight_configs:
            for style in query_styles:
                result = _evaluate_config(db, weights, style, ndcg_cases)
                report.results.append(result)
                logger.info(
                    "sweep: weights=(%s,%s,%s) style=%-7s → weighted=%.4f NDCG=%.4f Hit@5=%.3f (%ds)",
                    *weights,
                    style,
                    result.weighted_total,
                    result.ndcg_at_10,
                    result.hit_at_5,
                    int(result.duration_s),
                )
    finally:
        db.close()

    report.results.sort(key=lambda r: r.weighted_total, reverse=True)
    report.best = report.results[0] if report.results else None
    report.total_duration_s = time.monotonic() - t_total_start

    if output_path and report.results:
        _write_sweep_csv(output_path, report)

    if report.best:
        logger.info(
            "sweep: BEST → weights=(%s,%s,%s) style=%s weighted=%.4f NDCG=%.4f",
            *report.best.weights,
            report.best.query_style,
            report.best.weighted_total,
            report.best.ndcg_at_10,
        )

    return report
