"""
Cross-encoder re-ranking for kairix search (post-RRF semantic pass).

Uses `ms-marco-MiniLM-L-6-v2` (~22 MB, CPU-only) to re-score the top-N
candidates from RRF by semantic query-document relevance. The model runs
locally — no API calls, no Azure dependency.

The cross-encoder is a lazy singleton: the model is loaded on the first call
and reused for all subsequent calls. This keeps cold-start cost (≈300ms) to a
single request per process.

Design constraints:
  - Only the first RERANK_CANDIDATE_LIMIT results are re-scored. Results
    beyond that limit are returned unchanged (ranked below re-scored results).
  - Re-ranking uses `result.snippet` (≤500 chars) rather than full document
    text to stay within a 150ms latency budget on modern hardware.
  - On any error (import failure, model load failure, inference error) the
    function returns the input list unmodified. Never raises.

Optional dependency — install via:
    pip install kairix[rerank]
    # which installs sentence-transformers
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairix.core.search.rrf import FusedResult

logger = logging.getLogger(__name__)

RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_CANDIDATE_LIMIT: int = 20

_cross_encoder = None  # lazy singleton
_cross_encoder_checked = False  # True once we've tried to load (even if it failed)


def _get_cross_encoder(model: str):
    """Load and cache the cross-encoder model. Returns None on any import/load failure."""
    global _cross_encoder, _cross_encoder_checked
    if _cross_encoder_checked:
        return _cross_encoder
    _cross_encoder_checked = True
    try:
        from sentence_transformers import (
            CrossEncoder,  # type: ignore[import-untyped] — sentence-transformers has no upstream type stubs
        )

        _cross_encoder = CrossEncoder(model)
        logger.info("rerank: loaded cross-encoder model %r", model)
        return _cross_encoder
    except ImportError:
        logger.warning(
            "rerank: sentence-transformers not installed — re-ranking disabled. "
            "Install with: pip install kairix[rerank]"
        )
        return None
    except Exception as e:
        logger.warning("rerank: failed to load model %r — %s — re-ranking disabled", model, e)
        return None


def get_cross_encoder(model: str = RERANK_MODEL):
    """Load and cache the cross-encoder model.

    Public API for dependency injection. Returns None on any import/load failure.

    .. deprecated:: 2025.04
        ``_get_cross_encoder`` is now ``get_cross_encoder``. The private name
        remains as an alias.
    """
    return _get_cross_encoder(model)


def rerank(
    query: str,
    results: list[FusedResult],
    model: str = RERANK_MODEL,
    candidate_limit: int = RERANK_CANDIDATE_LIMIT,
    encoder=None,
) -> list[FusedResult]:
    """
    Re-sort results by cross-encoder relevance score (post-RRF semantic pass).

    Only the top ``candidate_limit`` results are re-scored. Any results beyond
    that limit are appended after the re-ranked candidates, preserving their
    original relative order.

    The re-rank score is stored in ``result.rerank_score`` and used to sort the
    candidates. ``boosted_score`` is overwritten with the re-rank score so that
    ``apply_budget`` (which sorts by ``boosted_score``) respects the new order.

    Args:
        query:           Search query string.
        results:         FusedResult list from RRF + boost pipeline.
        model:           Cross-encoder model name. Default: ms-marco-MiniLM-L-6-v2.
        candidate_limit: Number of top candidates to pass to the cross-encoder.
        encoder:         Optional pre-loaded cross-encoder instance for
                         dependency injection. Defaults to lazy-loaded singleton.

    Returns:
        Results re-sorted by re-rank score. Returns ``results`` unchanged on any
        error (import failure, model load, inference). Never raises.
    """
    if not results:
        return results

    if encoder is None:
        encoder = _get_cross_encoder(model)
    if encoder is None:
        return results

    candidates = results[:candidate_limit]
    tail = results[candidate_limit:]

    try:
        pairs = [(query, r.snippet[:500] if r.snippet else r.title) for r in candidates]
        scores: list[float] = encoder.predict(pairs).tolist()

        for r, score in zip(candidates, scores, strict=False):
            r.rerank_score = float(score)
            r.boosted_score = float(score)  # overwrite so apply_budget respects new order

        re_ranked = sorted(candidates, key=lambda r: r.rerank_score, reverse=True)
        return re_ranked + tail

    except Exception as e:
        logger.warning("rerank: inference failed — %s — returning unmodified results", e)
        return results
