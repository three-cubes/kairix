"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Provider`.

``Provider`` is the provider-plugin Protocol every LLM / embed endpoint
family implements. When the endpoint is down, plugins raise the canonical
:class:`~kairix.providers.ProviderUnreachable`; the transport adapters that
core code consumes (:class:`ProviderEmbeddingService`,
:class:`ProviderChatBackend`) must turn that into the "never raises"
shapes callers short-circuit on (``[]`` per text, ``""``), while
``dimension()`` keeps reporting the configured width and
``healthcheck()`` reports ``ok=False`` with the canonical class name.

One body per method, two implementations (F43 limb 2):

* the real :class:`OllamaProvider` whose ``transport_client`` seam is a
  :class:`tests.fakes.FakeOllamaTransport` raising
  ``ConnectionRefusedError`` (a stopped sidecar);
* the canonical :class:`tests.fakes.FakeProvider` configured to raise
  ``ProviderUnreachable`` / report an unhealthy endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import ProviderHealth, ProviderUnreachable
from kairix.providers.ollama import OllamaProvider
from kairix.transport.embed_service import ProviderChatBackend, ProviderEmbeddingService
from tests.fakes import FakeOllamaTransport, FakeProvider

pytestmark = pytest.mark.contract

_ENDPOINT = "http://ollama.invalid:11434"
_DIMS = 1536


def _real_unreachable() -> Any:
    return OllamaProvider(
        Credentials(api_key="", endpoint=_ENDPOINT, model="nomic-embed-text", dims=_DIMS),
        transport_client=FakeOllamaTransport(raises=ConnectionRefusedError("F68-sidecar-stopped")),
    )


def _fake_unreachable() -> Any:
    down = ProviderUnreachable(f"F68 endpoint unreachable at {_ENDPOINT!r}")
    return FakeProvider(
        name="ollama",
        dim=_DIMS,
        embed_raises=down,
        chat_raises=down,
        health=ProviderHealth(ok=False, endpoint=_ENDPOINT, error="ProviderUnreachable"),
    )


_FACTORIES: list[Callable[[], Any]] = [_real_unreachable, _fake_unreachable]
_IDS = ["real-ollama", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_embed_batch_raises_embedding_service_returns_empty_vector_per_text(factory: Callable[[], Any]) -> None:
    """The plugin raises ``ProviderUnreachable``; the embedding service
    returns one ``[]`` per input text so callers attribute failure per text.

    Sabotage proof: in ``ProviderEmbeddingService.embed_batch`` replace the
    ``except Exception`` body's ``return [[] for _ in texts]`` with
    ``raise``. Re-run: both cases fail with ``ProviderUnreachable``.
    Restored.
    """
    provider = factory()

    with pytest.raises(ProviderUnreachable):
        provider.embed_batch(["alpha"])

    service = ProviderEmbeddingService(provider)
    assert service.embed_batch(["alpha", "beta"]) == [[], []]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_chat_raises_chat_backend_returns_empty_reply(factory: Callable[[], Any]) -> None:
    """The plugin raises on chat; ``ProviderChatBackend`` returns ``""``.

    Sabotage proof: in ``ProviderChatBackend.chat`` replace the
    ``except Exception`` body's ``return ""`` with ``raise``. Re-run: both
    cases fail with ``ProviderUnreachable``. Restored.
    """
    provider = factory()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "summarise the knowledge store"}]

    with pytest.raises(ProviderUnreachable):
        provider.chat(messages)

    assert ProviderChatBackend(provider).chat(messages, max_tokens=64) == ""


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_dimension_unavailable_endpoint_still_reports_configured_width(factory: Callable[[], Any]) -> None:
    """After a failed embed the plugin still reports the configured width
    (never 0 / the width of an empty vector).

    Sabotage proof: make ``OllamaProvider.dimension`` return
    ``DEFAULT_EMBED_DIMENSION`` unconditionally. Re-run: the real case
    fails (768 != 1536). Restored.
    """
    provider = factory()
    assert ProviderEmbeddingService(provider).embed_batch(["alpha"]) == [[]]

    assert provider.dimension() == _DIMS


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_healthcheck_unavailable_endpoint_reports_not_ok_with_canonical_error(factory: Callable[[], Any]) -> None:
    """``healthcheck`` returns ``ok=False`` with ``error="ProviderUnreachable"``
    for the configured endpoint — the stable shape ``probe-config`` renders.

    Sabotage proof: in ``OllamaProvider.healthcheck`` change
    ``error=type(err).__name__`` to ``error=str(err)``. Re-run: the real
    case fails on the ``health.error`` assertion. Restored.
    """
    health = factory().healthcheck()

    assert health.ok is False
    assert health.error == "ProviderUnreachable"
    assert health.endpoint == _ENDPOINT
    assert health.warm_ms is None
