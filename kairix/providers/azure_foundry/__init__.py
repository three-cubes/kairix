"""Azure Foundry provider plugin.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to Azure AI Foundry's
OpenAI-compatible endpoint (``/openai/v1`` alias on
``<resource>.services.ai.azure.com``).

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("azure_foundry")
   vectors = provider.embed_batch(["hello world"])

This plugin is on the hot path — the factory wires
``ProviderEmbeddingService(get_provider("azure_foundry"))`` whenever
``provider: azure_foundry`` is set in ``kairix.config.yaml``.

See ``docs/architecture/provider-plugin-architecture.md`` for the
ADR and ``tests/bdd/features/provider_azure_foundry.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kairix.credentials import Credentials, get_credentials
from kairix.providers._base import Provider
from kairix.providers.azure_foundry.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    AzureFoundryProvider,
    normalize_foundry_endpoint,
)


def make_provider(
    *,
    credentials_resolver: Callable[[str], Any] = get_credentials,
) -> Provider:
    """Construct the Azure Foundry :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via ``credentials_resolver``
    (defaults to :func:`kairix.credentials.get_credentials`, which
    encodes the vault-agent → env → Azure Key Vault fallback) and
    constructs an :class:`AzureFoundryProvider` against it.

    Tests pass ``credentials_resolver=lambda purpose: Credentials(...)``
    to inject a stub resolver — F1-clean (no internal patching) and
    F6-clean (production default is a real callable, not ``None``).
    """
    creds = credentials_resolver("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "azure_foundry: embed credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-embed-* or kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: set provider: azure_foundry in kairix.config.yaml once "
            "secrets are populated."
        )
    return AzureFoundryProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "AzureFoundryProvider",
    "make_provider",
    "normalize_foundry_endpoint",
]
