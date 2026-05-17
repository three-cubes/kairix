"""Edge-of-helper branches in :mod:`kairix.providers.azure_foundry`.

Covers the residual lines below the F7 90% floor in
``kairix/providers/azure_foundry/provider.py``:

- ``_status_code_of`` second branch (code via ``err.response.status_code``);
- ``_retry_after_of`` defensive branches (no headers / headers.get
  raises / unparseable value);
- ``_is_connection_failure`` class-name branch (``APITimeoutError``);
- ``_client()`` lazy-build branch — the production path that resolves
  the openai-compat client via ``kairix.credentials.make_openai_client``;
- ``dimension()`` ``credentials.dims`` fallback.

All branches driven through the public surface (``chat``, ``embed_batch``,
``dimension``). No private-name imports, no ``@patch``.
"""

from __future__ import annotations

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
)
from kairix.providers.azure_foundry import AzureFoundryProvider


class _RaisingEmbeddings:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **_kwargs: object) -> object:
        raise self._err


class _RaisingChatCompletions:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **_kwargs: object) -> object:
        raise self._err


class _RaisingChat:
    def __init__(self, err: BaseException) -> None:
        self.completions = _RaisingChatCompletions(err)


class _RaisingTransport:
    def __init__(self, err: BaseException) -> None:
        self.embeddings = _RaisingEmbeddings(err)
        self.chat = _RaisingChat(err)


def _creds(*, dims: int = 1536) -> Credentials:
    return Credentials(
        api_key="foundry-test-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com",
        model="text-embedding-3-large",
        dims=dims,
    )


@pytest.mark.unit
def test_status_code_extracted_from_response_when_top_level_attribute_absent() -> None:
    """Error with only ``response.status_code`` still maps via the 429 path.

    Sabotage-proof: removing the ``response = getattr(err, "response",
    ...)`` block in ``_status_code_of`` routes to bare ProviderError;
    ``pytest.raises(RateLimited)`` fails.
    """

    class _Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

    class _ResponseOnlyError(Exception):
        def __init__(self) -> None:
            self.response = _Response(429)
            super().__init__("response-only 429")

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_ResponseOnlyError()),
    )

    with pytest.raises(RateLimited):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_absent() -> None:
    """No ``headers`` on response → silent None.

    Sabotage-proof: removing the ``if headers is None: return None``
    guard surfaces AttributeError on the next ``headers.get`` call;
    RateLimited isn't reached.
    """

    class _ResponseNoHeaders:
        def __init__(self) -> None:
            self.status_code = 429

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _ResponseNoHeaders()
            super().__init__("429 no headers")

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_get_raises() -> None:
    """``headers.get`` raising → retry hint is silently None.

    Sabotage-proof: removing the ``except Exception: return None`` block
    propagates RuntimeError; ``pytest.raises(RateLimited)`` fails.
    """

    class _BrokenHeaders:
        def get(self, _key: str, _default: object = None) -> object:
            raise RuntimeError("broken headers")

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = _BrokenHeaders()

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 broken headers")

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_value_is_unparseable() -> None:
    """Non-numeric Retry-After is silently dropped.

    Sabotage-proof: removing the ``except (TypeError, ValueError):
    return None`` block lets float() raise ValueError; the exception
    surfaces and ``pytest.raises(RateLimited)`` fails.
    """
    headers = {"Retry-After": "not-a-number"}

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = headers

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 bad retry-after")

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_api_timeout_error_class_name_maps_to_provider_unreachable() -> None:
    """Exception class named ``APITimeoutError`` → ProviderUnreachable.

    Sabotage-proof: removing ``"APITimeoutError"`` from the recognised
    class-name set routes through bare ProviderError;
    ``pytest.raises(ProviderUnreachable)`` fails.
    """

    class APITimeoutError(Exception):
        pass

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(APITimeoutError("timeout")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_chat_uses_lazily_resolved_client_when_transport_not_supplied() -> None:
    """When ``transport_client=None`` the plugin resolves an openai-compat client.

    The lazy ``from kairix.credentials import make_openai_client`` plus
    the call drive the production-path lines 239-241. The constructed
    client is a real OpenAI SDK object (deps installed in test env); we
    don't hit the network because the fake api_key surfaces an auth
    error before any HTTP request leaves the process — but the
    production wiring is exercised.

    Sabotage-proof: removing the ``return make_openai_client(...)`` line
    means ``_client()`` returns ``None`` and the next attribute access
    surfaces a TypeError that doesn't pattern-match ProviderError; the
    typed-error contract is broken and ``pytest.raises(ProviderError)``
    fails.
    """
    provider = AzureFoundryProvider(credentials=_creds(), transport_client=None)

    with pytest.raises(ProviderError):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_dimension_returns_default_when_neither_captured_nor_configured() -> None:
    """``dims=0`` + no captured embed → returns DEFAULT_EMBED_DIMENSION.

    Sabotage-proof: removing the ``return DEFAULT_EMBED_DIMENSION``
    fall-through makes dimension() return ``None``; the int assertion
    fails.
    """
    provider = AzureFoundryProvider(credentials=_creds(dims=0), transport_client=None)

    out = provider.dimension()

    assert out == 1536  # DEFAULT_EMBED_DIMENSION


@pytest.mark.unit
def test_unknown_class_with_no_status_falls_back_to_bare_provider_error() -> None:
    """Drives ``_status_code_of`` ``return None`` (line 116) plus
    ``_map_transport_error`` fall-through.

    Sabotage-proof: removing the ``return ProviderError(...)``
    fall-through means unknown errors propagate; the typed-error
    contract is broken.
    """

    class _UnknownError(Exception):
        pass

    provider = AzureFoundryProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_UnknownError("unknown")),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])

    assert type(exc_info.value) is ProviderError
    assert not isinstance(exc_info.value, AuthError)
