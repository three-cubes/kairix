"""Embedding providers — SDK-based clients with built-in retry and rate limiting.

Turns your text into numbers that the search engine uses to find similar
content (vector embeddings). Uses the openai SDK for automatic retry,
rate-limit handling, and backoff — no manual retry logic needed.

Two providers available:
  AzureEmbedProvider — for Azure OpenAI endpoints
  OpenAIEmbedProvider — for standard OpenAI endpoints (including OpenRouter)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbedProvider(Protocol):
    """Interface for embedding text into vectors.

    Implementations handle retry, rate limiting, and backoff internally
    via the openai SDK.
    """

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        """Embed a batch of texts into vectors.

        Args:
            texts: List of text strings to embed.
            model: Model deployment name (e.g. "text-embedding-3-large").
            dims:  Embedding dimensions (e.g. 1536).

        Returns:
            List of embedding vectors (same length as texts).
        """
        ...


class AzureEmbedProvider:
    """Azure OpenAI embeddings via the openai SDK."""

    def __init__(self, endpoint: str, api_key: str, max_retries: int = 5) -> None:
        from kairix.credentials import make_openai_client

        self.client = make_openai_client(api_key, endpoint, max_retries=max_retries)

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        response = self.client.embeddings.create(input=texts, model=model, dimensions=dims)
        return [item.embedding for item in response.data]


class OpenAIEmbedProvider:
    """Standard OpenAI / OpenRouter embeddings via the openai SDK."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.openai.com/v1",
        max_retries: int = 5,
    ) -> None:
        from kairix.credentials import make_openai_client

        self.client = make_openai_client(api_key, endpoint, max_retries=max_retries)

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        response = self.client.embeddings.create(input=texts, model=model, dimensions=dims)
        return [item.embedding for item in response.data]


def _default_embed_credentials() -> object | None:
    """Production default — resolve the ``embed`` role credentials chain."""
    from kairix.credentials import get_credentials

    return get_credentials("embed")


@dataclass
class EmbedProviderDeps:
    """Injectable collaborators for :func:`get_embed_provider`.

    Canonical Deps shape (``kairix/worker.py::WorkerDeps``):

    - ``credentials``: returns a ``Credentials`` instance or ``None``.
      Defaults to ``kairix.credentials.get_credentials("embed")`` which
      checks env vars (KAIRIX_EMBED_* / KAIRIX_LLM_*), secrets file, then
      Azure Key Vault. Multi-tenant / per-call credential injection (see
      docs/architecture/provider-plugin-architecture.md) and tests pass
      their own callable; ``lambda: None`` skips credential resolution and
      exercises the OPENAI_API_KEY fallback.
    """

    credentials: Callable[[], object | None] = field(default_factory=lambda: _default_embed_credentials)


def get_embed_provider(
    *,
    deps: EmbedProviderDeps | None = None,
    env: Mapping[str, str] | None = None,
) -> EmbedProvider:
    """Get the configured embed provider.

    Resolution order:
      1. ``deps.credentials()`` — returns a ``Credentials`` instance or None
         (see :class:`EmbedProviderDeps`; production default is
         ``kairix.credentials.get_credentials("embed")``).
      2. ``OPENAI_API_KEY`` from ``env`` (defaults to ``os.environ``) — the
         backwards-compat fallback.

    Selects AzureEmbedProvider when the endpoint is an Azure URL, otherwise
    falls back to OpenAIEmbedProvider.

    ``deps`` and ``env`` are DI seams; tests pass them explicitly rather
    than mutating the process environment or stubbing get_credentials.

    Raises OSError if no credentials are available.
    """
    from kairix.credentials import Credentials

    deps = deps if deps is not None else EmbedProviderDeps()
    if env is None:
        env = os.environ

    creds = deps.credentials()

    if isinstance(creds, Credentials) and creds.api_key and creds.endpoint:
        if creds.is_azure:
            logger.debug("embed_provider: using AzureEmbedProvider")
            return AzureEmbedProvider(endpoint=creds.endpoint, api_key=creds.api_key)
        else:
            logger.debug("embed_provider: using OpenAIEmbedProvider")
            return OpenAIEmbedProvider(api_key=creds.api_key, endpoint=creds.endpoint)

    # Fall back to OPENAI_API_KEY for backwards compatibility
    openai_key = env.get("OPENAI_API_KEY")
    if openai_key:
        logger.debug("embed_provider: using OpenAIEmbedProvider (OPENAI_API_KEY fallback)")
        return OpenAIEmbedProvider(api_key=openai_key)

    raise OSError(
        "No embedding provider configured. Set KAIRIX_LLM_API_KEY + KAIRIX_LLM_ENDPOINT "
        "(or KAIRIX_EMBED_API_KEY + KAIRIX_EMBED_ENDPOINT for a separate embed provider), "
        "or OPENAI_API_KEY for OpenAI."
    )
