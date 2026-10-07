"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EmbedderProtocol`.

``kairix.quality.contracts.embed.EmbedderProtocol`` is the quality-layer
embedding contract (``embed`` / ``embed_as_bytes`` / ``dimension``). No
production class declares it directly, so the contract is proved over a
thin conforming adapter around the production single-text embedding path
(:class:`ProviderEmbeddingService`) and the plugin's ``dimension()``.

When the provider is unreachable the contract's failure shapes must hold:
``embed`` returns ``[]``, ``embed_as_bytes`` returns ``None`` (never an
empty / truncated blob a vector index would accept), and ``dimension``
keeps reporting the configured width.

One body per method, two providers under the adapter (F43 limb 2):

* the real :class:`OllamaProvider` whose transport refuses the connection;
* the canonical :class:`tests.fakes.FakeProvider` raising
  ``ProviderUnreachable``.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import ProviderUnreachable
from kairix.providers.ollama import OllamaProvider
from kairix.quality.contracts.embed import EmbedderProtocol
from kairix.transport.cache.embed_cache import EmbedCache
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeOllamaTransport, FakeProvider

pytestmark = pytest.mark.contract

_DIMS = 1536


class _ServiceEmbedder:
    """``EmbedderProtocol`` over the production embedding service + plugin."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        cache = EmbedCache()
        self._service = ProviderEmbeddingService(
            provider,
            existing_coalescer_fn=lambda: None,
            coalescer_factory=lambda **_kwargs: None,
            get_embed_cache_fn=lambda: cache,
        )

    def embed(self, text: str) -> list[float]:
        return self._service.embed(text)

    def embed_as_bytes(self, text: str) -> bytes | None:
        vector = self.embed(text)
        if not vector:
            return None
        return struct.pack(f"<{len(vector)}f", *vector)

    def dimension(self) -> int:
        return int(self._provider.dimension())


_FACTORIES: list[Callable[[], Any]] = [
    lambda: OllamaProvider(
        Credentials(api_key="", endpoint="http://ollama.invalid:11434", model="nomic-embed-text", dims=_DIMS),
        transport_client=FakeOllamaTransport(raises=ConnectionRefusedError("F68-sidecar-stopped")),
    ),
    lambda: FakeProvider(dim=_DIMS, embed_raises=ProviderUnreachable("F68 embed endpoint unreachable")),
]
_IDS = ["real-ollama", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_embed_unavailable_provider_returns_empty_vector(factory: Callable[[], Any]) -> None:
    """``embed`` returns ``[]`` rather than raising when the provider is down.

    Sabotage proof: in ``ProviderEmbeddingService.embed`` replace the
    direct-dispatch ``except Exception`` body's ``return []`` with
    ``raise``. Re-run: both cases fail with ``ProviderUnreachable``.
    Restored.
    """
    embedder = _ServiceEmbedder(factory())
    assert isinstance(embedder, EmbedderProtocol)

    assert embedder.embed("release checklist") == []


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_embed_as_bytes_unavailable_provider_returns_none_not_empty_blob(factory: Callable[[], Any]) -> None:
    """``embed_as_bytes`` returns ``None`` — never ``b""``, which a vector
    store would happily persist as a zero-dimension row.

    Sabotage proof: in ``ProviderEmbeddingService.embed`` change the
    direct-dispatch ``except Exception`` body's ``return []`` to
    ``return [0.0]``. Re-run: both cases fail because a 4-byte blob comes
    back instead of ``None``. Restored.
    """
    embedder = _ServiceEmbedder(factory())

    assert embedder.embed_as_bytes("release checklist") is None


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_dimension_unavailable_provider_still_reports_configured_width(factory: Callable[[], Any]) -> None:
    """After a failed embed, ``dimension`` still reports the configured
    width so index-shape checks don't collapse to 0.

    Sabotage proof: make ``OllamaProvider.dimension`` return
    ``DEFAULT_EMBED_DIMENSION`` unconditionally. Re-run: the real case
    fails (768 != 1536). Restored.
    """
    embedder = _ServiceEmbedder(factory())
    assert embedder.embed("release checklist") == []

    assert embedder.dimension() == _DIMS
