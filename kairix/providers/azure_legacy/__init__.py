"""Azure-legacy provider plugin (Azure OpenAI Service, pre-Foundry).

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to the legacy Azure OpenAI
Service endpoint shape (``https://<resource>.openai.azure.com``) — the
URL family Microsoft shipped before Azure AI Foundry consolidated
everything behind ``services.ai.azure.com``. Many enterprise tenants
still ride the legacy endpoint today; this plugin keeps them on a
first-class path without forcing them to rewrite their configured URL.

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("azure_legacy")
   vectors = provider.embed_batch(["hello world"])

Distinct from :mod:`kairix.providers.azure_foundry`: the legacy plugin
uses the ``openai.AzureOpenAI`` SDK class (not the generic
``OpenAI(base_url=...)`` form) and emits the Azure-specific
``api-version`` query parameter on every call. Constructing the legacy
plugin against a Foundry-shaped endpoint fails fast with an actionable
:class:`ValueError` pointing at ``provider: azure_foundry``.

See ``docs/architecture/provider-plugin-architecture.md`` for the ADR
and ``tests/bdd/features/provider_azure_legacy.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from kairix.providers._base import Provider
from kairix.providers.azure_legacy.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    AzureLegacyProvider,
)


def make_provider() -> Provider:
    """Construct the Azure-legacy :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via
    :func:`kairix.credentials.get_credentials` (which already encodes
    the vault-agent → env → Azure Key Vault fallback) and constructs
    an :class:`AzureLegacyProvider` against it. The provider's transport
    client is resolved lazily via :func:`kairix.transport.pool.get_client`
    so the process-shared connection pool is reused across coalescer
    batches.

    Tests should NOT call ``make_provider()``; they construct
    :class:`AzureLegacyProvider` directly with a
    :class:`~kairix.credentials.Credentials` test instance and (where
    relevant) a recording ``transport_client``. This factory exists
    purely to satisfy the entry-point discovery contract.
    """
    from kairix.credentials import Credentials, get_credentials

    creds = get_credentials("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "azure_legacy: embed credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-embed-* or kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: re-run with KAIRIX_PROVIDER=azure_legacy once secrets are populated."
        )
    return AzureLegacyProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "AzureLegacyProvider",
    "make_provider",
]
