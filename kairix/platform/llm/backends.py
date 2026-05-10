"""
Concrete LLMBackend implementations.

AzureOpenAIBackend — thin wrapper over kairix._azure.

Tests substitute the chat/embed callables through ``LLMBackendDeps`` rather
than passing per-method ``*_fn=None`` substitution kwargs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def _default_chat(messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
    """Production chat callable — delegates to ``kairix._azure.chat_completion``."""
    from kairix._azure import chat_completion

    result: str = chat_completion(messages, max_tokens=max_tokens)
    return result


def _default_embed(text: str) -> list[float]:
    """Production embed callable — delegates to ``kairix._azure.embed_text``."""
    from kairix._azure import embed_text

    result: list[float] = embed_text(text)
    return result


@dataclass
class LLMBackendDeps:
    """Injectable dependencies for ``AzureOpenAIBackend``.

    Each field defaults to the production Azure callable; tests construct
    ``LLMBackendDeps(chat=fake_chat, embed=fake_embed)`` rather than passing
    ``chat_fn=`` / ``embed_fn=`` kwargs to the backend's constructor.
    """

    chat: Callable[..., str] = field(default_factory=lambda: _default_chat)
    embed: Callable[[str], list[float]] = field(default_factory=lambda: _default_embed)


class AzureOpenAIBackend:
    """
    LLMBackend backed by Azure OpenAI via kairix._azure.

    Delegates to the existing ``chat_completion`` and ``embed_text``
    functions which handle Key Vault secret resolution, retry logic,
    and failure-safe return values.

    This class adds no extra logic — it is purely an adapter so callers
    can program against the ``LLMBackend`` protocol without importing
    ``kairix._azure`` directly.

    # Adapter pattern: satisfies LLMBackend protocol by delegating to _azure module
    """

    def __init__(self, deps: LLMBackendDeps | None = None) -> None:
        """Construct with optional injectable dependencies for testing.

        Production callers leave ``deps`` ``None`` and the defaults wire
        to ``kairix._azure``; tests pass ``LLMBackendDeps(chat=...)``.
        """
        self._deps = deps if deps is not None else LLMBackendDeps()

    def chat(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
        """Chat completion via Azure OpenAI (gpt-4o-mini by default)."""
        return self._deps.chat(messages, max_tokens=max_tokens)

    def embed(self, text: str) -> list[float]:
        """Text embedding via Azure OpenAI (text-embedding-3-large)."""
        return self._deps.embed(text)
