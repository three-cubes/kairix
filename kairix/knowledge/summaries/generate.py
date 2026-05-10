"""
L0/L1 summary generation via gpt-4o-mini (Azure OpenAI).

L0: 1-2 sentence abstract (~100 tokens)
L1: structured overview (~500 tokens) — main topic, key points, status

Both functions raise on API failure. The batch helper (generate_summaries)
catches and logs failures per-file so callers always get partial results.

Tests inject a fake chat callable through ``SummariesDeps`` rather than
threading per-helper ``chat_fn=None`` substitution kwargs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kairix.text import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SummaryResult:
    path: str  # Source file path
    l0: str  # 1-2 sentence abstract (~100 tokens)
    l1: str | None  # Structured overview (~500 tokens), None if not requested
    model: str  # Model used
    generated_at: str  # ISO timestamp
    tokens_used: int  # Total tokens consumed


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

_L0_SYSTEM = (
    "You are a precise document summariser. "
    "Summarise in 1-2 sentences (max 100 tokens). "
    "Be specific and factual — name the main topic, key decisions or actions, "
    "and the outcome or current state."
)

_L1_SYSTEM = (
    "You are a precise document summariser. "
    "Write a structured overview (max 500 tokens). Include:\n"
    "- Main topic (1 sentence)\n"
    "- Key points or decisions (bullet list, max 5)\n"
    "- Current status or outcome (1 sentence)\n"
    "Be specific: name tools, dates, people, and decisions where present."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_n_words(text: str, n: int) -> str:
    """Return the first n whitespace-separated words of text."""
    words = text.split()
    return " ".join(words[:n])


def default_chat(messages: list[dict], max_tokens: int) -> str:  # pragma: no cover — prod wrapper around kairix._azure
    """Production chat callable — delegates to ``kairix._azure.chat_completion``.

    Wrapper exists so ``SummariesDeps.chat`` has a stable, typed default
    that doesn't import ``kairix._azure`` at module-import time.
    """
    from kairix._azure import chat_completion

    return chat_completion(messages, max_tokens=max_tokens)


@dataclass
class SummariesDeps:
    """Injectable dependencies for the summaries module.

    Each field defaults to a production implementation; tests construct
    ``SummariesDeps(chat=fake_chat)`` with a fake callable rather than
    threading per-helper ``chat_fn=None`` substitution kwargs.
    """

    chat: Callable[..., str] = field(default_factory=lambda: default_chat)


def _call_chat(
    messages: list[dict],
    api_key: str,
    endpoint: str,
    deployment: str,
    max_tokens: int,
    deps: SummariesDeps | None = None,
) -> tuple[str, int]:
    """
    Call Azure OpenAI chat completions via the shared SDK client.

    Returns (content, estimated_tokens_used). Raises on failure.

    The api_key, endpoint, and deployment parameters are accepted for backwards
    compatibility but ignored — credentials are resolved by ``kairix._azure``.
    """
    if deps is None:  # pragma: no cover — production lazy default; tests pass deps=SummariesDeps(chat=fake)
        deps = SummariesDeps()
    content = deps.chat(messages, max_tokens=max_tokens)
    # Token usage is not available from the shared client; estimate from output length.
    tokens_est = estimate_tokens(content)
    return content, tokens_est


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_l0(
    path: str,
    content: str,
    api_key: str,
    endpoint: str,
    deployment: str = "gpt-4o-mini",
    *,
    deps: SummariesDeps | None = None,
) -> str:
    """
    Generate L0 abstract for a document.

    Uses the first 800 words of content. Returns the abstract string.
    Raises on API failure.
    """
    truncated = _first_n_words(content, 800)
    messages = [
        {"role": "system", "content": _L0_SYSTEM},
        {"role": "user", "content": f"Document path: {path}\n\n{truncated}"},
    ]
    abstract, _ = _call_chat(messages, api_key, endpoint, deployment, max_tokens=150, deps=deps)
    return abstract


def generate_l1(
    path: str,
    content: str,
    api_key: str,
    endpoint: str,
    deployment: str = "gpt-4o-mini",
    *,
    deps: SummariesDeps | None = None,
) -> str:
    """
    Generate L1 structured overview for a document.

    Uses the first 2000 words of content. Returns the overview string.
    Raises on API failure.
    """
    truncated = _first_n_words(content, 2000)
    messages = [
        {"role": "system", "content": _L1_SYSTEM},
        {"role": "user", "content": f"Document path: {path}\n\n{truncated}"},
    ]
    overview, _ = _call_chat(messages, api_key, endpoint, deployment, max_tokens=600, deps=deps)
    return overview


def generate_summaries(
    paths: list[str],
    api_key: str,
    endpoint: str,
    deployment: str = "gpt-4o-mini",
    include_l1: bool = False,
    batch_size: int = 10,
    sleep_ms: int = 100,
    *,
    deps: SummariesDeps | None = None,
) -> list[SummaryResult]:
    """
    Batch generate summaries for a list of file paths.

    Reads file content, calls generate_l0 (and generate_l1 if include_l1).
    Failures on individual files are logged and skipped — never raised.
    Sleeps sleep_ms milliseconds between each file call for rate limiting.
    batch_size controls how many files are processed before each sleep.
    """
    if deps is None:  # pragma: no cover — production lazy default; tests pass deps=SummariesDeps(chat=fake)
        deps = SummariesDeps()
    results: list[SummaryResult] = []

    for i, path in enumerate(paths):
        # Sleep between batches (after the first batch_size items)
        if i > 0 and i % batch_size == 0:
            time.sleep(sleep_ms / 1000.0)

        try:
            file_path = Path(path)
            if not file_path.exists():
                logger.warning("generate_summaries: file not found — %s", path)
                continue

            content = file_path.read_text(encoding="utf-8", errors="replace")
            now = datetime.now(timezone.utc).isoformat()
            tokens_total = 0

            l0 = generate_l0(path, content, api_key, endpoint, deployment, deps=deps)
            # Rough token estimate for L0 if usage not tracked here
            tokens_total += estimate_tokens(l0)

            l1: str | None = None
            if include_l1:
                l1 = generate_l1(path, content, api_key, endpoint, deployment, deps=deps)
                tokens_total += estimate_tokens(l1)

            results.append(
                SummaryResult(
                    path=path,
                    l0=l0,
                    l1=l1,
                    model=deployment,
                    generated_at=now,
                    tokens_used=tokens_total,
                )
            )

        except Exception as exc:
            logger.error("generate_summaries: failed for %s — %s", path, exc)
            continue

        # Sleep between individual calls (within batch) as well
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

    return results
