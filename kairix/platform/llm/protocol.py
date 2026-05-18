"""
LLMBackend protocol — provider-agnostic interface for chat and embedding.

All kairix code that calls an LLM should accept a ``LLMBackend`` rather
than importing a concrete provider directly.  This decouples the engine
from any specific implementation and enables:

  - Swapping providers (Azure → Anthropic, local models, etc.) by
    changing the ``provider:`` field in ``kairix.config.yaml``.
  - Clean repo boundary: provider credentials stay inside the configured
    plugin (resolved via :func:`kairix.providers.get_provider`).
  - Easy test doubles (``FakeLLMBackend`` in ``tests/fakes.py``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMBackend(Protocol):
    """
    Minimal interface for an LLM provider.

    Implementations must be safe to call from multiple threads (or at
    minimum from a single long-running process) and should never raise —
    they return empty strings / empty lists on failure.
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 800,
    ) -> str:
        """
        Send a chat completion request.

        Args:
            messages:   OpenAI-compatible message list
                        (e.g. [{"role": "user", "content": "..."}])
            max_tokens: Maximum tokens in the response.

        Returns:
            The assistant reply string, or "" on failure.  Never raises.
        """
        ...

    def embed(self, text: str) -> list[float]:
        """
        Embed a text string.

        Args:
            text: The text to embed.

        Returns:
            Float vector, or [] on failure.  Never raises.
        """
        ...
