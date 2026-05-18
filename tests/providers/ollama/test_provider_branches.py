"""Edge-of-helper branches in :mod:`kairix.providers.ollama`.

Covers the residual lines below the F7 90% floor in
``kairix/providers/ollama/provider.py``:

- ``_status_code_of`` second branch (code via ``err.response.status_code``);
- ``_is_connection_failure`` class-name branch (``ConnectError`` /
  ``ConnectTimeout``);
- ``_client()`` lazy-build branch — production-path
  ``_HttpxOllamaTransport(...)`` construction;
- ``_HttpxOllamaTransport.__init__`` + ``post`` body — by exercising
  the lazy production transport against an unreachable endpoint;
- ``dimension()`` ``credentials.dims`` fallback.

All branches driven through the public ``OllamaProvider`` surface.
"""

from __future__ import annotations

import socket

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    ClientError,
    ProviderError,
    ProviderUnreachable,
    UpstreamError,
)
from kairix.providers.ollama import OllamaProvider


class _RaisingTransport:
    """A minimal OllamaTransport that always raises."""

    def __init__(self, err: BaseException) -> None:
        self._err = err

    def post(self, path: str, json: dict[str, object]) -> dict[str, object]:
        # Parameter names mirror the OllamaTransport Protocol exactly —
        # the provider invokes ``client.post(path, json=...)`` and
        # renamed kwargs would break protocol conformance even though
        # the values are unused here (we always raise).
        del path, json
        raise self._err


def _creds(*, endpoint: str = "http://localhost:11434", dims: int = 0) -> Credentials:
    return Credentials(
        api_key="",  # Ollama has no auth
        endpoint=endpoint,
        model="nomic-embed-text",
        dims=dims,
    )


@pytest.mark.unit
def test_status_code_extracted_from_response_when_top_level_attribute_absent() -> None:
    """Error with only ``response.status_code`` still maps via 4xx.

    Sabotage-proof: removing the ``response = getattr(err, "response",
    ...)`` block in ``_status_code_of`` routes to bare ProviderError;
    the typed assertion (ClientError) fails.
    """

    class _Response:
        def __init__(self, status: int) -> None:
            self.status_code = status

    class _ResponseOnlyError(Exception):
        def __init__(self) -> None:
            self.response = _Response(404)
            super().__init__("response-only 404")

    provider = OllamaProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_ResponseOnlyError()),
    )

    with pytest.raises(ClientError):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_connect_error_class_name_maps_to_provider_unreachable() -> None:
    """Exception class named ``ConnectError`` → ProviderUnreachable.

    Sabotage-proof: removing ``"ConnectError"`` from the recognised
    class-name set in ``_is_connection_failure`` routes through bare
    ProviderError; ``pytest.raises(ProviderUnreachable)`` fails.
    """

    class ConnectError(Exception):
        pass

    provider = OllamaProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(ConnectError("no socket")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_connect_timeout_class_name_maps_to_provider_unreachable() -> None:
    """Exception class named ``ConnectTimeout`` → ProviderUnreachable.

    Sabotage-proof: removing ``"ConnectTimeout"`` from the recognised
    class-name set routes through bare ProviderError.
    """

    class ConnectTimeout(Exception):  # noqa: N818 — class name must match the production whitelist in ``_is_connection_failure`` exactly
        pass

    provider = OllamaProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(ConnectTimeout("timeout")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_500_maps_to_upstream_error() -> None:
    """5xx surfaces as UpstreamError with status_code.

    Sabotage-proof: removing the 5xx branch routes to bare
    ProviderError; the type assertion fails.
    """

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 503
            super().__init__("503")

    provider = OllamaProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(UpstreamError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.status_code == 503


@pytest.mark.unit
def test_dimension_returns_default_when_neither_captured_nor_configured() -> None:
    """``dims=0`` + no captured embed → returns DEFAULT_EMBED_DIMENSION.

    Sabotage-proof: removing the ``return DEFAULT_EMBED_DIMENSION``
    fall-through makes dimension() return ``None``; the int assertion
    fails.

    Drives line 361 / DEFAULT_EMBED_DIMENSION (768 for Ollama).
    """
    provider = OllamaProvider(credentials=_creds(dims=0), transport_client=None)

    assert provider.dimension() == 768  # DEFAULT_EMBED_DIMENSION


# ---------------------------------------------------------------------------
# Production-path lazy transport — drives _HttpxOllamaTransport + .post
# ---------------------------------------------------------------------------


def _find_unused_port() -> int:
    """Open a transient socket bound to ephemeral port, read its number, close.

    Used to construct an endpoint URL guaranteed to be unreachable
    (no listener bound) so the lazily-built ``_HttpxOllamaTransport.post``
    immediately fails with httpx ``ConnectError``. Kept local so the
    test stays hermetic — no fixture, no module-level network state.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.unit
def test_chat_uses_lazy_httpx_transport_when_none_supplied() -> None:
    """When ``transport_client=None`` the provider builds a real httpx transport.

    Pointing at a known-closed loopback port lets us drive the
    production lazy-construction path AND the actual ``httpx.post``
    call — but the OS refuses the socket immediately so the call
    surfaces as ``ProviderUnreachable`` (not a hang).

    This drives lines 255 (``return _HttpxOllamaTransport(...)``),
    406-407 (``__init__``), 416-422 (lazy httpx import + post + the
    ConnectError catch).

    Sabotage-proof: removing the ``return _HttpxOllamaTransport(...)``
    line means ``_client()`` returns ``None``; the next attribute
    access (``.post(...)``) surfaces a TypeError that doesn't
    pattern-match ProviderError.
    """
    port = _find_unused_port()
    provider = OllamaProvider(
        credentials=_creds(endpoint=f"http://127.0.0.1:{port}"),
        transport_client=None,
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_unknown_class_with_no_status_falls_back_to_bare_provider_error() -> None:
    """Drives ``_status_code_of`` ``return None`` (line 135) plus the
    fall-through ``ProviderError`` branch.

    Sabotage-proof: removing the fall-through ``return ProviderError(...)``
    makes unknown errors propagate unchanged; the typed contract is
    broken.
    """

    class _UnknownError(Exception):
        pass

    provider = OllamaProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_UnknownError("unknown")),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])

    assert type(exc_info.value) is ProviderError
