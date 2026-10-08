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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairix.core.search.rrf import FusedResult

logger = logging.getLogger(__name__)

RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_CANDIDATE_LIMIT: int = 20


class CrossEncoderCache:
    """Load-once holder for the cross-encoder model.

    The model is loaded on the first :meth:`get` and reused for every later
    call; a failed load (sentence-transformers missing, corrupt weights, bad
    model name) is remembered too, so a broken install never retries the
    ≈300ms load on every query. Production shares the process-wide
    :data:`_DEFAULT_CACHE`; a caller wanting isolated state (a test, or a
    second model) constructs its own instance and passes it as ``cache=``.
    """

    def __init__(self) -> None:
        self._encoder: Any = None
        self._checked = False  # True once we've tried to load (even if it failed)

    def get(self, model: str = RERANK_MODEL) -> Any:
        """Return the cached encoder, loading it on first call. ``None`` on any failure."""
        if self._checked:
            return self._encoder
        self._checked = True
        try:
            from sentence_transformers import (
                CrossEncoder,  # type: ignore[import-untyped] — sentence-transformers has no upstream type stubs
            )
        except ImportError:
            logger.warning(
                "rerank: sentence-transformers not installed — re-ranking disabled. "
                "Install with: pip install kairix[rerank]"
            )
            return None
        # Guarded separately: an ImportError raised WHILE the model loads (a
        # missing backend, say) is a load failure, not "not installed".
        try:
            self._encoder = CrossEncoder(model)
        except Exception as e:
            logger.warning("rerank: failed to load model %r — %s — re-ranking disabled", model, e)
            return None
        logger.info("rerank: loaded cross-encoder model %r", model)
        return self._encoder


# Process-wide lazy singleton used when no ``cache=`` is supplied.
_DEFAULT_CACHE = CrossEncoderCache()


def get_cross_encoder(model: str = RERANK_MODEL, *, cache: CrossEncoderCache | None = None):
    """Load and cache the cross-encoder model.

    Public API for dependency injection. Returns None on any import/load failure.
    ``cache`` defaults to the process-wide singleton.
    """
    return (cache if cache is not None else _DEFAULT_CACHE).get(model)


def rerank(
    query: str,
    results: list[FusedResult],
    model: str = RERANK_MODEL,
    candidate_limit: int = RERANK_CANDIDATE_LIMIT,
    encoder=None,
    cache: CrossEncoderCache | None = None,
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
        cache:           Where the lazy loader keeps the model when ``encoder``
                         is omitted. Defaults to the process-wide singleton.

    Returns:
        Results re-sorted by re-rank score. Returns ``results`` unchanged on any
        error (import failure, model load, inference). Never raises.
    """
    if not results:
        return results

    if encoder is None:
        encoder = get_cross_encoder(model, cache=cache)
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
