"""OpenAI-direct provider plugin.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to the OpenAI-direct API
(or any drop-in OpenAI-compatible base URL — Together, Groq, Fireworks,
local vLLM, etc).

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("openai")
   vectors = provider.embed_batch(["hello world"])

This plugin is the proof of shape that the Protocol-and-error-mapping
pattern from :mod:`kairix.providers.azure_foundry` carries over to a
non-Azure endpoint without surgery. Once green, third-party plugins
under ``[project.entry-points."kairix.providers"]`` follow the same
shell.

See ``docs/architecture/provider-plugin-architecture.md`` for the ADR
and ``tests/bdd/features/provider_openai.feature`` for the wire-shape
contract this plugin pins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kairix.credentials import Credentials, get_credentials
from kairix.providers._base import Provider
from kairix.providers.openai.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    OpenAIProvider,
)


def make_provider(
    *,
    credentials_resolver: Callable[[str], Any] = get_credentials,
) -> Provider:
    """Construct the OpenAI-direct :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via ``credentials_resolver``
    (defaults to :func:`kairix.credentials.get_credentials`) and
    constructs an :class:`OpenAIProvider` against it. The provider's
    transport client is resolved lazily via
    :func:`kairix.transport.pool.get_client` so the process-shared
    connection pool is reused across coalescer batches.

    Tests pass ``credentials_resolver=lambda purpose: Credentials(...)``
    to inject a stub resolver — F1-clean (no internal patching) and
    F6-clean (production default is a real callable, not ``None``).
    """
    creds = credentials_resolver("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "openai: embed credentials did not resolve to a Credentials "
            "instance. fix: configure kairix-embed-* or kairix-llm-* secrets "
            "per docs/operations/OPERATIONS.md; "
            "next: set provider: openai in kairix.config.yaml once secrets "
            "are populated."
        )
    return OpenAIProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "OpenAIProvider",
    "make_provider",
]
