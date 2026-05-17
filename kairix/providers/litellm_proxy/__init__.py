"""LiteLLM-proxy provider plugin.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to a LiteLLM proxy sidecar
(https://github.com/BerriAI/litellm) — the operator runs the proxy in
front of N upstream LLM providers (Azure / OpenAI / Bedrock / Anthropic
/ Ollama / etc.) and kairix talks to the proxy's OpenAI-compatible
endpoint via a proxy-minted virtual key.

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("litellm_proxy")
   vectors = provider.embed_batch(["hello world"])

Structurally this plugin is the OpenAI-direct plugin's twin (same Bearer
auth + ``/embeddings`` / ``/chat/completions`` paths + same error
mapping). The differences are documentary (model ids may carry a
``<upstream>/<name>`` prefix that the proxy understands) and operational
(the operator runs a sidecar separately). F27 forbids importing from
:mod:`kairix.providers.openai` — the implementation is copied, not
shared, so each plugin stays independently shippable.

See ``docs/architecture/provider-plugin-architecture.md`` for the ADR
and ``tests/bdd/features/provider_litellm_proxy.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from kairix.providers._base import Provider
from kairix.providers.litellm_proxy.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    LiteLLMProxyProvider,
)


def make_provider() -> Provider:
    """Construct the LiteLLM-proxy :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via
    :func:`kairix.credentials.get_credentials` (which already encodes
    the vault-agent → env → Azure Key Vault fallback) and constructs
    a :class:`LiteLLMProxyProvider` against it. The credential's
    ``api_key`` is the LiteLLM virtual key (operator-minted via the
    proxy's key-management surface); ``endpoint`` is the proxy URL
    (e.g. ``http://localhost:4000/v1``). The provider's transport
    client is resolved lazily via :func:`kairix.transport.pool.get_client`
    so the process-shared connection pool is reused across coalescer
    batches.

    Tests should NOT call ``make_provider()``; they construct
    :class:`LiteLLMProxyProvider` directly with a
    :class:`~kairix.credentials.Credentials` test instance and (where
    relevant) a recording ``transport_client``. This factory exists
    purely to satisfy the entry-point discovery contract.
    """
    from kairix.credentials import Credentials, get_credentials

    creds = get_credentials("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "litellm_proxy: embed credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-embed-* or kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: re-run with KAIRIX_PROVIDER=litellm_proxy once the proxy "
            "URL and virtual key are populated."
        )
    return LiteLLMProxyProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "LiteLLMProxyProvider",
    "make_provider",
]
