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

logger = logging.getLogger(__name__)


_L0_BUDGET = 1500
_L1_BUDGET = 3000
_L0_MAX_TOKENS = 150
_L1_MAX_TOKENS = 600


def _default_search(**kwargs: Any) -> Any:
    from kairix.core.factory import build_search_pipeline

    pipeline = build_search_pipeline()
    return pipeline.search(**kwargs)


def _default_chat(**kwargs: Any) -> str:
    from kairix.paths import provider_name
    from kairix.providers import get_provider
    from kairix.transport.embed_service import ProviderChatBackend

    name = provider_name()
    if name is None:
        raise ValueError("kairix.config.yaml is missing the required 'provider:' field")
    backend = ProviderChatBackend(get_provider(name))
    return backend.chat(**kwargs)


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
    """Injectable dependencies for ``run_prep``.

    Non-Optional fields wired to production defaults via ``default_factory``
    — eliminates the ``Optional[Callable]`` mypy regression class flagged
    in #204. Tests construct ``PrepDeps(search_fn=fake, chat_fn=fake)``
    with explicit overrides; ``PrepDeps()`` with no kwargs resolves to
    the production callables defined above.
    """

    search_fn: Callable[..., Any] = field(default_factory=lambda: _default_search)
    chat_fn: Callable[..., str] = field(default_factory=lambda: _default_chat)


_GROUND_RULES = (
    "If the documents do not contain information about the topic, "
    'reply with exactly: "No relevant content found in the knowledge store." '
    "Do NOT fabricate, infer, or fill in plausible-sounding details. "
    "Do NOT add information that is not in the documents."
)


def _build_messages(query: str, tier: str, context: str) -> list[dict[str, str]]:
    if tier == "l0":
        system = (
            "You are a concise knowledge assistant. Based ONLY on the provided documents, "
            "summarise what is known about the topic in 2-3 sentences. "
            f"{_GROUND_RULES}"
        )
    else:
        system = (
            "You are a knowledge assistant. Based ONLY on the provided documents, "
            "provide a structured overview of the topic. "
            f"{_GROUND_RULES}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Topic: {query}\n\nDocuments:\n{context}"},
    ]


# Without this floor, a top-5 hit with a 12-character snippet ("see ref-001")
# gets fed to the LLM as "context" — the model treats it as authoritative and
# hallucinates to fill the gap (#254 dogfood). 40 chars is empirical: an actual
# sentence-worth of grounding; anything shorter is title-equivalent.
_MIN_USEFUL_SNIPPET_CHARS = 40


def _format_context(search_result: Any) -> tuple[str, list[str]]:
    """Project a SearchResult's top 5 hits into a context string + source titles.

    Only hits with non-trivial snippet content (≥ ``_MIN_USEFUL_SNIPPET_CHARS``)
    are included in the LLM context. Hits with empty/title-only content are
    still returned in ``sources`` if at least one usable hit exists, so the
    operator can see what the retrieval found even when its content was thin.
    Returns ``("", [])`` when no hit has usable snippet content — the caller
    treats this as "no relevant documents" rather than calling the LLM.
    """
    parts: list[str] = []
    sources: list[str] = []
    for budgeted in getattr(search_result, "results", [])[:5]:
        inner = getattr(budgeted, "result", None)
        if inner is None:
            continue
        title = getattr(inner, "title", "") or getattr(inner, "path", "")
        snippet = (getattr(budgeted, "content", "") or "").strip()
        if len(snippet) < _MIN_USEFUL_SNIPPET_CHARS:
            continue
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
    search = d.search_fn
    chat = d.chat_fn

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
