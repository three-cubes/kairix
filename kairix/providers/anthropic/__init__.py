"""Anthropic provider plugin — chat-only via the Messages API.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to Anthropic's
``POST /v1/messages`` endpoint (``api.anthropic.com``).

Anthropic is the chat-only family in the provider matrix —
:meth:`AnthropicProvider.embed_batch` raises
:class:`kairix.providers.EmbedNotSupported` immediately, before any
outbound request is constructed, because Anthropic ships no embeddings
endpoint at all. Operators wanting embed alongside Anthropic chat
combine ``anthropic`` (chat) with a separate embed provider
(typically ``openai``).

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("anthropic")
   reply = provider.chat([{"role": "user", "content": "hi"}])

Per the ADR, each plugin owns its credential pattern. Anthropic's
pattern is "api key from env / file / Azure Key Vault via the existing
:func:`kairix.credentials.get_credentials` chain" — same as openai and
azure_foundry. Because anthropic is chat-LLM (not embed),
``make_provider()`` resolves the ``llm`` credential purpose rather than
``embed``.

See ``docs/architecture/provider-plugin-architecture.md`` for the ADR
and ``tests/bdd/features/provider_anthropic.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kairix.credentials import Credentials, get_credentials
from kairix.providers._base import Provider
from kairix.providers.anthropic.provider import (
    ANTHROPIC_API_VERSION,
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_ENDPOINT,
    EMBED_DIMENSION_NOT_APPLICABLE,
    PROVIDER_NAME,
    AnthropicProvider,
)


def make_provider(
    *,
    credentials_resolver: Callable[[str], Any] = get_credentials,
) -> Provider:
    """Construct the Anthropic :class:`Provider` for entry-point discovery.

    Resolves the ``llm`` credential set via ``credentials_resolver``
    (defaults to :func:`kairix.credentials.get_credentials`) and
    constructs an :class:`AnthropicProvider` against it. Anthropic is
    chat-LLM (not embed) so the resolver is asked for ``llm`` rather
    than ``embed`` — the per-plugin credential pattern documented in
    the provider-plugin ADR.

    Tests pass ``credentials_resolver=lambda purpose: Credentials(...)``
    to inject a stub resolver — F1-clean (no internal patching) and
    F6-clean (production default is a real callable, not ``None``).
    """
    creds = credentials_resolver("llm")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "anthropic: llm credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: set provider: anthropic in kairix.config.yaml once "
            "secrets are populated."
        )
    return AnthropicProvider(credentials=creds)


__all__ = [
    "ANTHROPIC_API_VERSION",
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_ENDPOINT",
    "EMBED_DIMENSION_NOT_APPLICABLE",
    "PROVIDER_NAME",
    "AnthropicProvider",
    "make_provider",
]
