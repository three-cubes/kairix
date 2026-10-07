"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`OllamaTransport`.

``OllamaTransport.post(path, json)`` is the single HTTP seam the Ollama
provider plugin speaks through. The typical production failure is a
stopped ``ollama serve`` sidecar: the transport raises a connection-level
error, and the plugin must translate it into the canonical
:class:`~kairix.providers.ProviderUnreachable` carrying the configured
endpoint — so the transport-layer retry policy and ``probe-config`` see
the same vocabulary for every provider.

One body, two transports (F43 limb 2), both driven through the real
:class:`OllamaProvider`:

* the real httpx-backed transport the plugin builds when no
  ``transport_client`` is injected, pointed at an out-of-range port so
  address resolution fails locally (no socket is opened);
* the canonical :class:`tests.fakes.FakeOllamaTransport` raising
  ``ConnectionRefusedError`` (the OS error a stopped sidecar produces).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.credentials import Credentials
from kairix.providers import ProviderUnreachable
from kairix.providers.ollama import OllamaProvider
from tests.fakes import FakeOllamaTransport

pytestmark = pytest.mark.contract

# Port 99999 is outside the TCP range: resolution fails before any connect.
_ENDPOINT = "http://127.0.0.1:99999"


def _credentials() -> Credentials:
    return Credentials(api_key="", endpoint=_ENDPOINT, model="nomic-embed-text", dims=768)


_FACTORIES: list[Callable[[], OllamaProvider]] = [
    lambda: OllamaProvider(_credentials()),
    lambda: OllamaProvider(
        _credentials(),
        transport_client=FakeOllamaTransport(raises=ConnectionRefusedError("F68-ollama-sidecar-stopped")),
    ),
]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-httpx", "fake"])
def test_post_unavailable_sidecar_maps_to_provider_unreachable(factory: Callable[[], OllamaProvider]) -> None:
    """A dead sidecar surfaces as ``ProviderUnreachable`` naming the endpoint,
    and ``healthcheck`` reports ``ok=False`` with that canonical class name.

    Sabotage proof: in ``kairix/providers/ollama/provider.py::_map_transport_error``
    move the ``_is_connection_failure`` branch below the status-code checks
    and make it return ``ProviderError`` instead of ``ProviderUnreachable``.
    Re-run: both cases fail on the ``pytest.raises(ProviderUnreachable)``
    and on ``health.error``. Restored.
    """
    provider = factory()

    with pytest.raises(ProviderUnreachable, match=r"127\.0\.0\.1:99999"):
        provider.embed_batch(["is the sidecar up?"])

    health = provider.healthcheck()
    assert health.ok is False
    assert health.error == "ProviderUnreachable"
    assert health.endpoint == _ENDPOINT
