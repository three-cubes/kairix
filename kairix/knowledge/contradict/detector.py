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

Never raises — failures return empty lists. Pass a ``ContradictDetectorDeps``
dataclass to substitute scorer / extractor / search at the boundary
without monkey-patching.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor
from kairix.knowledge.contradict.scorers import (
    CompositeContradictionScorer,
    default_contradiction_scorer,
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


def _default_search() -> Callable[..., Any]:
    """Production search callable — bound to the canonical ``SearchPipeline``."""
    from kairix.core.factory import build_search_pipeline

    return build_search_pipeline().search


@dataclass
class ContradictDetectorDeps:
    """Injectable dependencies for ``check_contradiction``.

    Each field defaults to a production implementation; tests construct
    ``ContradictDetectorDeps(search=fake_search, ...)`` with fakes
    rather than threading per-helper ``*_fn=None`` substitution kwargs.

    The scorer field is built lazily from the LLM passed to
    ``check_contradiction`` because the production scorer needs the
    LLM at construction time. Tests bypass that by setting ``scorer``
    directly.
    """

    search: Callable[..., Any] = field(default_factory=_default_search)
    scorer: CompositeContradictionScorer | None = None
    extractor: Any = field(default_factory=EntityDensityClaimExtractor)


def _gather_candidates(
    claims: list[str],
    search: Callable[..., Any],
    search_kwargs: dict[str, Any],
    top_k: int,
) -> dict[str, tuple[str, Any]]:
    """Run search across all claims and dedupe candidates by doc path."""
    seen: dict[str, tuple[str, Any]] = {}
    for claim in claims:
        try:
            sr = search(query=claim[:500], **search_kwargs)
        except Exception as exc:
            logger.warning("contradict: hybrid search failed for claim — %s", exc)
            continue
        for bundle in sr.results[:top_k]:
            path = bundle.result.path
            if path not in seen:
                seen[path] = (claim, bundle)
    return seen


def _build_search_kwargs(agent: str | None, scope: Any) -> dict[str, Any]:
    """Construct kwargs forwarded to the search backend; only sets explicit fields."""
    kwargs: dict[str, Any] = {"budget": 5000}
    if agent is not None:
        kwargs["agent"] = agent
    if scope is not None:
        kwargs["scope"] = scope
    return kwargs


def check_contradiction(
    content: str,
    llm: Any,
    top_k: int = 5,
    threshold: float = 0.45,
    agent: str | None = None,
    scope: Any = None,
    *,
    top_claims: int = 3,
    deps: ContradictDetectorDeps | None = None,
) -> list[ContradictionResult]:
    """Check whether ``content`` contradicts existing knowledge in the document store.

    Args:
        content:    The new content to check (claim, note, decision, etc.).
        llm:        An LLM backend implementing ``chat(messages)`` — only used
                    to build the default scorer when ``deps.scorer`` is None.
        top_k:      How many similar documents to compare against per claim.
        threshold:  Minimum contradiction score (0.0-1.0) to include. Default
                    0.45 — calibrated for the three-category composite which
                    individually scores more conservatively than the historic
                    single-prompt scorer.
        top_claims: How many high-signal claims to extract from content.
        deps:       Injectable dependency bundle (search callable, scorer,
                    extractor). Production callers leave ``None`` and the
                    defaults wire to the real ``SearchPipeline``,
                    composite scorer, and entity-density extractor.

    Returns:
        List of ContradictionResult, sorted by score descending. Empty list
        when no contradictions found or on any failure.
    """
    d = deps if deps is not None else ContradictDetectorDeps()
    scorer = d.scorer if d.scorer is not None else default_contradiction_scorer(llm)

    claims = d.extractor.extract(content, top_n=top_claims) or [content[:500]]
    seen_paths = _gather_candidates(claims, d.search, _build_search_kwargs(agent, scope), top_k)

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
