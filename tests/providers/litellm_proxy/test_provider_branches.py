"""Edge-of-helper branches in :mod:`kairix.providers.litellm_proxy`.

Covers the residual lines below the F7 90% floor:

- ``_status_code_of`` second branch (code via ``err.response.status_code``);
- ``_retry_after_of`` defensive branches (no headers / headers.get
  raises / unparseable value);
- ``_is_connection_failure`` class-name branch (``APITimeoutError``);
- ``_client()`` lazy-build branch via ``kairix.transport.pool.get_client``;
- ``dimension()`` ``credentials.dims`` fallback.

All branches driven through the public surface. No private-name imports.
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
from kairix.providers.litellm_proxy import LiteLLMProxyProvider


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
        api_key="litellm-virtual-key",  # pragma: allowlist secret
        endpoint="http://localhost:4000/v1",
        model="azure/gpt-4o-mini",
        dims=dims,
    )


@pytest.mark.unit
def test_status_code_extracted_from_response_when_top_level_attribute_absent() -> None:
    """Error with only ``response.status_code`` still maps via 429.

    Sabotage-proof: removing the response-fallback block surfaces bare
    ProviderError; the typed assertion fails.
    """

    class _Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.response = _Response(429)
            super().__init__("response-only 429")

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_absent() -> None:
    """No ``headers`` attribute → silent None.

    Sabotage-proof: removing the ``if headers is None: return None``
    guard surfaces AttributeError.
    """

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 no headers")

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_get_raises() -> None:
    """``headers.get`` raising → silent None.

    Sabotage-proof: removing the ``except Exception: return None``
    propagates the RuntimeError.
    """

    class _BrokenHeaders:
        def get(self, _key: str, _default: object = None) -> object:
            raise RuntimeError("broken")

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = _BrokenHeaders()

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 broken headers")

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_value_is_unparseable() -> None:
    """Non-numeric Retry-After → silent None.

    Sabotage-proof: removing the ``except (TypeError, ValueError):
    return None`` propagates ValueError.
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

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_api_timeout_error_class_name_maps_to_provider_unreachable() -> None:
    """``APITimeoutError``-named exception → ProviderUnreachable.

    Sabotage-proof: dropping ``"APITimeoutError"`` from the recognised
    class-name set routes through bare ProviderError.
    """

    class APITimeoutError(Exception):
        pass

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(APITimeoutError("timeout")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_chat_uses_lazily_resolved_client_when_transport_not_supplied() -> None:
    """When ``transport_client=None`` the plugin resolves via the pool.

    Drives the production-path lines (236-242). The real openai-compat
    client is constructed; the network is not actually contacted
    because the fake api_key surfaces an auth error first.

    Sabotage-proof: removing the ``return get_client(...)`` line means
    ``_client()`` returns ``None``; next attribute access surfaces a
    TypeError that doesn't pattern-match ProviderError.
    """
    provider = LiteLLMProxyProvider(credentials=_creds(), transport_client=None)

    with pytest.raises(ProviderError):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_dimension_returns_default_when_neither_captured_nor_configured() -> None:
    """``dims=0`` + no captured embed → returns DEFAULT_EMBED_DIMENSION.

    Sabotage-proof: removing the ``return DEFAULT_EMBED_DIMENSION``
    fall-through makes dimension() return ``None``.
    """
    provider = LiteLLMProxyProvider(credentials=_creds(dims=0), transport_client=None)

    assert provider.dimension() == 1536  # DEFAULT_EMBED_DIMENSION


@pytest.mark.unit
def test_unknown_class_with_no_status_falls_back_to_bare_provider_error() -> None:
    """``_status_code_of`` returns None + bare ProviderError.

    Sabotage-proof: removing the ``return ProviderError(...)``
    fall-through means unknown errors propagate unchanged.
    """

    class _UnknownError(Exception):
        pass

    provider = LiteLLMProxyProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_UnknownError("unknown")),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])

    assert type(exc_info.value) is ProviderError
    assert not isinstance(exc_info.value, AuthError)
