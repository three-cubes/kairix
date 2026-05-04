"""
kairix.knowledge.contradict.detector — Contradiction detection for new memory writes.

Checks whether a new piece of content contradicts existing knowledge in
the document store via a Strategy-pattern pipeline:

  1. ``ClaimExtractor`` splits the input into top-N high-signal claims.
  2. For each claim, hybrid search retrieves candidate snippets.
  3. ``CompositeContradictionScorer`` evaluates each (claim, candidate)
     pair across three categories — direct, overstatement, status
     mismatch — and returns the winning category for each candidate.
  4. Results above ``threshold`` are returned, sorted by score.

Never raises — failures return empty lists. Pass injectable scorer/
extractor/search/LLM for testing without monkey-patching.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor
from kairix.knowledge.contradict.scorers import (
    CompositeContradictionScorer,
    default_contradiction_scorer,
    parse_llm_score,
)

logger = logging.getLogger(__name__)


@dataclass
class ContradictionResult:
    """A single detected contradiction between new content and an existing document."""

    doc_path: str
    score: float  # 0.0-1.0; higher = stronger contradiction
    reason: str  # one-sentence explanation
    snippet: str  # excerpt from the existing document
    category: str = "direct"  # which scorer fired (direct | overstatement | status_mismatch)
    claim: str = ""  # the extracted claim that drove the search/scoring


# Backwards-compat shim — historical callers and tests imported the private
# parser. parse_llm_score now returns (None, "") on failure so this is a
# direct alias. Marked for removal once tests/contradict/test_detector.py
# migrates to the public parse_llm_score.
_parse_llm_response = parse_llm_score


def check_contradiction(
    content: str,
    llm: Any,
    top_k: int = 5,
    threshold: float = 0.45,
    agent: str | None = None,
    scope: Any = None,
    *,
    top_claims: int = 3,
    search_fn: Callable[..., Any] | None = None,
    scorer: CompositeContradictionScorer | None = None,
    extractor: Any | None = None,
) -> list[ContradictionResult]:
    """
    Check whether *content* contradicts existing knowledge in the document store.

    Args:
        content:    The new content to check (claim, note, decision, etc.).
        llm:        An LLM backend implementing ``chat(messages)`` — only used
                    if scorer is None (the default scorer wraps the LLM).
        top_k:      How many similar documents to compare against per claim.
        threshold:  Minimum contradiction score (0.0-1.0) to include. Default
                    0.45 — calibrated for the three-category composite which
                    individually scores more conservatively than the historic
                    single-prompt scorer.
        top_claims: How many high-signal claims to extract from content.
        search_fn:  Injectable search function. Defaults to SearchPipeline.search.
        scorer:     Injectable composite scorer. Defaults to all three categories
                    (direct + overstatement + status_mismatch).
        extractor:  Injectable claim extractor. Defaults to entity-density rank.

    Returns:
        List of ContradictionResult, sorted by score descending. Empty list
        when no contradictions found or on any failure.
    """
    if search_fn is None:
        from kairix.core.factory import build_search_pipeline

        _pipeline = build_search_pipeline()
        search_fn = _pipeline.search

    if scorer is None:
        scorer = default_contradiction_scorer(llm)

    if extractor is None:
        extractor = EntityDensityClaimExtractor()

    claims = extractor.extract(content, top_n=top_claims) or [content[:500]]

    # Union candidates across all claims, deduping on doc_path so a doc that
    # surfaced for two claims is scored once against the most-relevant claim.
    # Build search kwargs — agent and scope only forwarded when explicitly set
    # so callers passing fakes that don't accept those kwargs aren't broken.
    search_kwargs: dict[str, Any] = {"budget": 5000}
    if agent is not None:
        search_kwargs["agent"] = agent
    if scope is not None:
        search_kwargs["scope"] = scope

    seen_paths: dict[str, tuple[str, Any]] = {}
    for claim in claims:
        try:
            sr = search_fn(query=claim[:500], **search_kwargs)
            for bundle in sr.results[:top_k]:
                path = bundle.result.path
                if path not in seen_paths:
                    seen_paths[path] = (claim, bundle)
        except Exception as exc:
            logger.warning("contradict: hybrid search failed for claim — %s", exc)

    logger.info(
        "contradict: extracted %d claims, retrieved %d unique candidates (threshold=%.2f)",
        len(claims),
        len(seen_paths),
        threshold,
    )

    results: list[ContradictionResult] = []
    for path, (claim, bundle) in seen_paths.items():
        snippet = bundle.content[:800]
        category, score, reason = scorer.best_category(claim, snippet)
        if score >= threshold:
            results.append(
                ContradictionResult(
                    doc_path=path,
                    score=score,
                    reason=reason,
                    snippet=snippet[:300],
                    category=category or "direct",
                    claim=claim,
                )
            )

    results.sort(key=lambda r: r.score, reverse=True)
    logger.info("contradict: %d contradictions found (threshold=%.2f)", len(results), threshold)
    return results
