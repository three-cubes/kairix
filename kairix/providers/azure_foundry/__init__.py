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

Until IM-1 rewires :mod:`kairix._azure` to delegate here, the legacy
module stays the production caller; this plugin is reachable but not
yet on the hot path.

See ``docs/architecture/provider-plugin-architecture.md`` for the
ADR and ``tests/bdd/features/provider_azure_foundry.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from kairix.providers._base import Provider
from kairix.providers.azure_foundry.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    AzureFoundryProvider,
    normalize_foundry_endpoint,
)


def make_provider() -> Provider:
    """Construct the Azure Foundry :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via
    :func:`kairix.credentials.get_credentials` (which already encodes
    the vault-agent → env → Azure Key Vault fallback) and constructs
    an :class:`AzureFoundryProvider` against it.

    Tests should NOT call ``make_provider()``; they construct
    :class:`AzureFoundryProvider` directly with a
    :class:`~kairix.credentials.Credentials` test instance and (where
    relevant) a recording ``transport_client``. This factory exists
    purely to satisfy the entry-point discovery contract.
    """
    from kairix.credentials import Credentials, get_credentials

    creds = get_credentials("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "azure_foundry: embed credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-embed-* or kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: re-run with KAIRIX_PROVIDER=azure_foundry once secrets are populated."
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
