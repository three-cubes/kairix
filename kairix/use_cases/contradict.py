"""Contradict use case — single source of truth for ``kairix contradict check``
and ``mcp__contradict``.

Phase 2 of the CLI/MCP feature parity initiative (#168). Pre-Phase-2
drift:

  - CLI accepted ``--top-claims``; MCP did not (hardcoded to 3).
  - CLI default agent was the literal string ``"shared"``; MCP defaulted
    to ``None``. Same query produced different result sets.
  - CLI emitted ``category`` and ``claim`` per result; MCP omitted both.
  - CLI rounded score to 4 decimals in ``--json``; MCP returned raw float.

This use case absorbs every divergence into one ``run_contradict``
returning a ``ContradictOutput`` dataclass. Adapters serialise from it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.core.search.scope import Scope
from kairix.use_cases import _contradict_defaults as _defaults

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContradictionHit:
    """A single contradicting document, projected from the detector's result."""

    path: str
    score: float
    reason: str
    snippet: str
    category: str = ""
    claim: str = ""


@dataclass(frozen=True)
class ContradictOutput:
    """Outcome of one ``run_contradict`` invocation.

    Attributes:
        content: The caller's content, unchanged.
        contradictions: Up to ``top_k * top_claims`` ``ContradictionHit``s
            that scored above ``threshold``, best-first.
        has_contradictions: Equivalent to ``len(contradictions) > 0``;
            kept as a top-level field for ergonomic JSON-envelope reads.
        error: Empty string on success; structured ``"<Class>: <msg>"`` on
            top-level failure.
    """

    content: str
    contradictions: list[ContradictionHit] = field(default_factory=list)
    has_contradictions: bool = False
    error: str = ""


@dataclass(frozen=True)
class ContradictDeps:
    """Injectable dependencies for ``run_contradict``."""

    check_fn: Callable[..., list[Any]] | None = None
    llm_backend: Any | None = None


def _project(r: Any) -> ContradictionHit:
    return ContradictionHit(
        path=str(getattr(r, "doc_path", "")),
        score=float(getattr(r, "score", 0.0)),
        reason=str(getattr(r, "reason", "")),
        snippet=str(getattr(r, "snippet", "")),
        category=str(getattr(r, "category", "")),
        claim=str(getattr(r, "claim", "")),
    )


def run_contradict(
    content: str,
    *,
    agent: str | None = None,
    scope: Scope = Scope.SHARED_AGENT,
    top_k: int = 5,
    threshold: float = 0.45,
    top_claims: int = 3,
    deps: ContradictDeps | None = None,
) -> ContradictOutput:
    """Run contradiction detection and return a structured result.

    Never raises — failures populate ``ContradictOutput.error``.

    Args:
        content: The new content to check.
        agent: Agent scope for retrieval; passed through unchanged.
        scope: Multi-agent scope.
        top_k: Documents compared per claim.
        threshold: Minimum contradiction score (0.0-1.0).
        top_claims: High-signal claims extracted from ``content``.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or ContradictDeps()
    check = d.check_fn or _defaults.default_check_contradiction

    try:
        llm = d.llm_backend if d.llm_backend is not None else _defaults.default_llm_backend()

        kwargs: dict[str, Any] = {
            "content": content,
            "llm": llm,
            "top_k": top_k,
            "threshold": threshold,
            "top_claims": top_claims,
            "scope": scope,
        }
        if agent is not None:
            kwargs["agent"] = agent

        results = check(**kwargs)
        hits = [_project(r) for r in results]
        return ContradictOutput(
            content=content,
            contradictions=hits,
            has_contradictions=bool(hits),
        )
    except Exception as exc:
        logger.warning("run_contradict failed: %s", exc, exc_info=True)
        return ContradictOutput(
            content=content,
            contradictions=[],
            has_contradictions=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def contradict_output_to_envelope(out: ContradictOutput) -> dict[str, Any]:
    """Project a ``ContradictOutput`` to the JSON envelope MCP callers receive."""
    return {
        "content": out.content,
        "contradictions": [
            {
                "path": h.path,
                "score": h.score,
                "reason": h.reason,
                "snippet": h.snippet,
                "category": h.category,
                "claim": h.claim,
            }
            for h in out.contradictions
        ],
        "has_contradictions": out.has_contradictions,
        "error": out.error,
    }
