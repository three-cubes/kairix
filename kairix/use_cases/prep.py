"""Prep use case — tiered L0/L1 context summary shared by CLI and MCP.

Phase 3c of the CLI/MCP feature parity initiative (#168). Pre-Phase-3c
``prep`` was MCP-only — operators couldn't reproduce an agent's prep
output from a shell. This module wraps the existing tool_prep logic
so both surfaces call the same ``run_prep``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kairix.core.search.scope import Scope
from kairix.text import estimate_tokens
from kairix.use_cases import _prep_defaults as _defaults

logger = logging.getLogger(__name__)


_L0_BUDGET = 1500
_L1_BUDGET = 3000
_L0_MAX_TOKENS = 150
_L1_MAX_TOKENS = 600


@dataclass(frozen=True)
class PrepOutput:
    """Outcome of one ``run_prep`` invocation.

    Attributes:
        query: The caller's query, unchanged.
        tier: Either ``"l0"`` (2-3 sentences) or ``"l1"`` (structured overview).
        summary: LLM-generated summary grounded in retrieved documents.
            Empty when no relevant documents were found, or on error.
        tokens: Estimated token count of ``summary``.
        sources: Up to 5 source titles/paths used as context.
        error: Empty on success; structured ``"<Class>: <msg>"`` on
            top-level failure.
    """

    query: str
    tier: str
    summary: str = ""
    tokens: int = 0
    sources: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class PrepDeps:
    """Injectable dependencies for ``run_prep``."""

    search_fn: Callable[..., Any] | None = None
    chat_fn: Callable[..., str] | None = None


def _build_messages(query: str, tier: str, context: str) -> list[dict[str, str]]:
    if tier == "l0":
        system = (
            "You are a concise knowledge assistant. Based ONLY on the provided documents, "
            "summarise what is known about the topic in 2-3 sentences. "
            "Do not add information that is not in the documents."
        )
    else:
        system = (
            "You are a knowledge assistant. Based ONLY on the provided documents, "
            "provide a structured overview of the topic. "
            "Do not add information that is not in the documents."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Topic: {query}\n\nDocuments:\n{context}"},
    ]


def _format_context(search_result: Any) -> tuple[str, list[str]]:
    """Project a SearchResult's top 5 hits into a context string + source titles."""
    parts: list[str] = []
    sources: list[str] = []
    for budgeted in getattr(search_result, "results", [])[:5]:
        inner = getattr(budgeted, "result", None)
        if inner is None:
            continue
        title = getattr(inner, "title", "") or getattr(inner, "path", "")
        snippet = getattr(budgeted, "content", "") or ""
        parts.append(f"[{title}]\n{snippet[:500]}")
        sources.append(str(title))
    return ("\n\n---\n\n".join(parts) if parts else ""), sources


def run_prep(
    query: str,
    *,
    agent: str | None = None,
    scope: Scope = Scope.SHARED_AGENT,
    tier: Literal["l0", "l1"] = "l0",
    deps: PrepDeps | None = None,
) -> PrepOutput:
    """Run grounded summarisation over retrieved documents.

    Never raises — failures populate ``PrepOutput.error``.

    Args:
        query: Topic to summarise.
        agent: Agent name for collection scoping.
        scope: Multi-agent scope (default shared+agent).
        tier: ``"l0"`` for 2-3 sentences, ``"l1"`` for structured overview.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or PrepDeps()
    search = d.search_fn or _defaults.default_search
    chat = d.chat_fn or _defaults.default_chat

    try:
        budget = _L0_BUDGET if tier == "l0" else _L1_BUDGET
        sr = search(query=query, agent=agent, scope=scope, budget=budget)
        context, sources = _format_context(sr)

        if not context:
            return PrepOutput(
                query=query,
                tier=tier,
                summary="No relevant documents found for this topic.",
            )

        max_tokens = _L0_MAX_TOKENS if tier == "l0" else _L1_MAX_TOKENS
        messages = _build_messages(query, tier, context)
        summary = chat(messages=messages, max_tokens=max_tokens)
        return PrepOutput(
            query=query,
            tier=tier,
            summary=summary,
            tokens=estimate_tokens(summary),
            sources=sources,
        )
    except Exception as exc:
        logger.warning("run_prep failed: %s", exc, exc_info=True)
        return PrepOutput(query=query, tier=tier, error=f"{type(exc).__name__}: {exc}")


def prep_output_to_envelope(out: PrepOutput) -> dict[str, Any]:
    """Project a ``PrepOutput`` to the JSON envelope MCP callers receive."""
    return {
        "query": out.query,
        "tier": out.tier,
        "summary": out.summary,
        "tokens": out.tokens,
        "sources": out.sources,
        "error": out.error,
    }
