"""Optional LLM-powered onboarding assistant.

Analyses corpus profile and provides configuration recommendations.
Falls back gracefully when no API key is available.
Never sends document content — only metadata (counts, patterns, types).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _default_chat(prompt: str, api_key: str, endpoint: str) -> str:  # pragma: no cover — prod Azure wrapper
    """Production chat callable — wraps ``kairix._azure.chat_completion``.

    Kept as a top-level function so ``OnboardingAgentDeps.chat`` has a
    stable, typed default that doesn't import ``kairix._azure`` at
    module-import time.

    The ``api_key`` and ``endpoint`` args are part of the chat-callable
    interface for symmetry with test fakes; the real ``chat_completion``
    resolves credentials internally via the kairix paths layer.
    """
    del api_key, endpoint  # the prod path resolves Azure creds internally
    from kairix._azure import chat_completion

    return chat_completion(
        [{"role": "user", "content": prompt}],
        max_tokens=200,
    )


@dataclass
class OnboardingAgentDeps:
    """Injectable dependencies for the onboarding LLM call.

    F1/F6-clean: tests pass ``OnboardingAgentDeps(chat=fake)`` instead
    of patching ``kairix.platform.setup.agent._call_llm`` or threading
    a ``*_fn=None`` kwarg. Production callers leave the kwarg unset and
    the default factory wires the real Azure chat backend.
    """

    chat: Callable[[str, str, str], str] = field(default_factory=lambda: _default_chat)


def recommend_from_profile(
    total_docs: int,
    format_counts: dict[str, int],
    date_file_pct: float,
    procedural_pct: float,
    entity_pct: float,
    api_key: str | None = None,
    endpoint: str | None = None,
    *,
    deps: OnboardingAgentDeps | None = None,
) -> dict[str, Any] | None:
    """Generate configuration recommendations from corpus profile.

    When an API key is provided, calls the LLM with corpus metadata
    (never document content) for tailored advice. Without an API key,
    returns rule-based recommendations.

    Returns a dict with recommended config values, or None on failure.
    """
    # Rule-based recommendations (always available, no LLM needed)
    rec: dict[str, Any] = {
        "fusion_strategy": "bm25_primary",
        "temporal_boost": date_file_pct > 0.15,
        "procedural_boost": procedural_pct > 0.05,
        "entity_boost": entity_pct > 0.03,
        "reasoning": [],
    }

    if date_file_pct > 0.15:
        rec["reasoning"].append(
            f"{date_file_pct:.0%} of your files have dates in their names — temporal boost enabled."
        )
    if procedural_pct > 0.05:
        rec["reasoning"].append(
            f"{procedural_pct:.0%} of your files are procedural (how-to, runbook) — procedural boost enabled."
        )
    if entity_pct > 0.03:
        rec["reasoning"].append(f"{entity_pct:.0%} of your files are in entity folders — entity boost enabled.")

    pdf_pct = format_counts.get("pdf", 0) / max(total_docs, 1)
    if pdf_pct > 0.20:
        rec["fusion_strategy"] = "rrf"
        rec["reasoning"].append(
            f"{pdf_pct:.0%} of your files are PDFs (less keyword structure) — using RRF fusion instead of BM25-primary."
        )

    if not rec["reasoning"]:
        rec["reasoning"].append("Using default settings — your corpus looks standard.")

    # LLM enhancement (optional, additive)
    if api_key and endpoint:
        if deps is None:  # pragma: no cover — production lazy default; tests pass deps=OnboardingAgentDeps(chat=fake)
            deps = OnboardingAgentDeps()
        try:
            llm_advice = _call_llm(
                total_docs,
                format_counts,
                date_file_pct,
                procedural_pct,
                api_key,
                endpoint,
                chat=deps.chat,
            )
            if llm_advice:
                rec["llm_advice"] = llm_advice
        except Exception as exc:
            logger.debug("onboarding agent LLM call failed (non-critical): %s", exc)

    return rec


def _call_llm(
    total_docs: int,
    format_counts: dict[str, int],
    date_file_pct: float,
    procedural_pct: float,
    api_key: str,
    endpoint: str,
    *,
    chat: Callable[[str, str, str], str],
) -> str:
    """Call LLM with corpus metadata for tailored advice.

    Never sends document content — only aggregate statistics.

    ``chat`` is the injected backend callable (typed shape:
    ``(prompt, api_key, endpoint) -> str``). Production callers wire
    the real Azure chat backend via ``OnboardingAgentDeps``; tests pass
    a fake chat callable.
    """
    prompt = (
        f"I have a knowledge base with {total_docs} documents. "
        f"File types: {format_counts}. "
        f"{date_file_pct:.0%} have dates in filenames. "
        f"{procedural_pct:.0%} are procedural (how-to/runbook). "
        "What search configuration would work best? "
        "Answer in 2-3 sentences."
    )
    return chat(prompt, api_key, endpoint)
