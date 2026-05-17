"""
kairix.platform.llm — LLM backend abstraction layer.

Provides a ``LLMBackend`` protocol so kairix components can call
chat/embed APIs without a hard dependency on any specific provider.

Built-in backends:
  AzureOpenAIBackend — historical name retained; delegates to the
  ``Provider`` plugin configured via ``kairix.config.yaml``'s
  ``provider:`` field (Azure / OpenAI / AWS Bedrock / LiteLLM proxy /
  custom plugin). Credential resolution is owned by the plugin.

Usage::

    from kairix.platform.llm import get_default_backend

    backend = get_default_backend()
    reply = backend.chat(messages=[{"role": "user", "content": "Hello"}])

Or inject a specific backend for testing::

    from kairix.platform.llm.backends import AzureOpenAIBackend, LLMBackendDeps
    backend = AzureOpenAIBackend(deps=LLMBackendDeps(chat=fake_chat))
"""

from kairix.platform.llm.backends import AzureOpenAIBackend
from kairix.platform.llm.protocol import LLMBackend

__all__ = ["AzureOpenAIBackend", "LLMBackend", "get_default_backend"]


def get_default_backend() -> LLMBackend:
    """Return the default backend backed by the configured provider plugin."""
    return AzureOpenAIBackend()
