"""Ollama (local) provider plugin.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to an Ollama sidecar
(typically ``http://localhost:11434`` or ``http://ollama:11434`` inside
a compose network).

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml``:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("ollama")
   vectors = provider.embed_batch(["hello world"])

Three structural differences from the OpenAI / Foundry plugins:

- **No auth.** Ollama has no credential model — the api_key field on
  ``Credentials`` may be the empty string and ``make_provider()``
  tolerates that explicitly. Connecting to the local socket is "auth".
- **Native API path.** The wire path is ``/api/embeddings`` (NOT
  ``/v1/embeddings`` and NOT ``/openai/v1/embeddings``); the openai
  SDK does not model that surface, so this plugin ships its own minimal
  httpx-backed transport.
- **Loop-batched embed.** Ollama's ``/api/embeddings`` accepts one
  prompt per request — the plugin owns the fan-out so the Protocol
  ``embed_batch`` contract (batch in, batch out, same order) is
  preserved.

See ``docs/architecture/provider-plugin-architecture.md`` for the ADR
and ``tests/bdd/features/provider_ollama.feature`` for the wire-shape
contract this plugin pins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kairix.credentials import Credentials, get_credentials
from kairix.providers._base import Provider
from kairix.providers.ollama.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    OllamaProvider,
    OllamaTransport,
)


def make_provider(
    *,
    credentials_resolver: Callable[[str], Any] = get_credentials,
) -> Provider:
    """Construct the Ollama :class:`Provider` for entry-point discovery.

    Resolves the ``embed`` credential set via ``credentials_resolver``
    (defaults to :func:`kairix.credentials.get_credentials`) and
    constructs an :class:`OllamaProvider` against it.

    **Empty api_key is tolerated.** Ollama is unauthenticated; operators
    configure only the endpoint and model. The credential resolver
    surfaces an empty string for ``api_key`` when nothing is set, and
    this factory accepts that explicitly rather than raising. The
    plugin never emits an ``Authorization`` header in any case
    (pinned by ``provider_ollama.feature``).

    Tests pass ``credentials_resolver=lambda purpose: Credentials(...)``
    to inject a stub resolver — F1-clean (no internal patching) and
    F6-clean (production default is a real callable, not ``None``).
    """
    creds = credentials_resolver("embed")
    if not isinstance(creds, Credentials):
        raise RuntimeError(
            "ollama: embed credentials did not resolve to a Credentials "
            "instance. fix: configure the KAIRIX_EMBED_ENDPOINT / "
            "KAIRIX_EMBED_MODEL values (api_key not required for ollama); "
            "next: set provider: ollama in kairix.config.yaml once the "
            "endpoint and model are populated."
        )
    return OllamaProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "OllamaProvider",
    "OllamaTransport",
    "make_provider",
]
