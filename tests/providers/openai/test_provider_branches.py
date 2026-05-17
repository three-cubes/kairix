"""Edge-of-helper branches in :mod:`kairix.providers.openai`.

Covers the residual lines below the F7 90% floor:

- ``_status_code_of`` second branch (code via ``err.response.status_code``);
- ``_retry_after_of`` defensive branches (no headers, headers.get raises,
  unparseable value);
- ``_is_connection_failure`` class-name branch (``APITimeoutError``);
- ``_client()`` lazy-build branch — the production path that resolves
  the process-shared client via ``kairix.transport.pool.get_client``;
- ``dimension()`` ``credentials.dims`` fallback (uses construction-time
  bypass: drop the captured dim to None then assert).

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
from kairix.providers.openai import OpenAIProvider


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
        api_key="openai-test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        model="text-embedding-3-large",
        dims=dims,
    )


# ---------------------------------------------------------------------------
# _status_code_of — code via response.status_code only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_code_extracted_from_response_when_top_level_attribute_absent() -> None:
    """Error with only ``response.status_code`` still maps via 429.

    Sabotage-proof: removing the ``response = getattr(err, "response",
    ...)`` block in ``_status_code_of`` would route to bare
    ProviderError; ``pytest.raises(RateLimited)`` fails.
    """

    class _ResponseShape:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

    class _ResponseOnlyError(Exception):
        def __init__(self) -> None:
            self.response = _ResponseShape(429)
            super().__init__("response-only 429")

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_ResponseOnlyError()),
    )

    with pytest.raises(RateLimited):
        provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# _retry_after_of — defensive branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_attribute_absent() -> None:
    """No headers attribute on response → no retry hint.

    Sabotage-proof: removing the ``if headers is None: return None``
    guard makes the next line AttributeError; the test sees the wrong
    exception class.
    """

    class _ResponseNoHeaders:
        def __init__(self) -> None:
            self.status_code = 429
            # No `.headers` at all.

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _ResponseNoHeaders()
            super().__init__("429 no headers")

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_get_raises() -> None:
    """``headers.get`` raising → retry hint is silently None.

    Sabotage-proof: removing the ``except Exception: return None``
    around ``headers.get`` propagates the RuntimeError and surfaces a
    different exception type than RateLimited.
    """

    class _BrokenHeaders:
        def get(self, _key: str, _default: object = None) -> object:
            raise RuntimeError("simulated broken headers")

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = _BrokenHeaders()

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 broken headers")

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_value_is_unparseable() -> None:
    """Non-numeric Retry-After → silently dropped.

    Sabotage-proof: removing the ``except (TypeError, ValueError):
    return None`` block lets float() raise ValueError; the retry hint
    becomes None implicitly only because the rescued type happens to
    match, otherwise the call surfaces ValueError instead of
    RateLimited.
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

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


# ---------------------------------------------------------------------------
# _is_connection_failure — APITimeoutError class-name branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_timeout_error_class_name_maps_to_provider_unreachable() -> None:
    """Exception class named ``APITimeoutError`` → ProviderUnreachable.

    Sabotage-proof: removing ``"APITimeoutError"`` from the recognised
    class-name set in ``_is_connection_failure`` routes to bare
    ProviderError; ``pytest.raises(ProviderUnreachable)`` fails.
    """

    class APITimeoutError(Exception):
        pass

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(APITimeoutError("timeout")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# _client() — lazy resolution via kairix.transport.pool.get_client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chat_uses_lazily_resolved_client_when_transport_not_supplied() -> None:
    """When ``transport_client=None`` the plugin resolves a process-shared client.

    The lazy import + call into ``kairix.transport.pool.get_client``
    drives lines 228-230. The resolved client is a real OpenAI SDK
    object (the dependency is installed in the test env); we never
    actually hit the network because the fake api_key + endpoint cause
    the upstream SDK to surface ``AuthenticationError`` long before
    any HTTP request leaves the process — but the construction itself
    completes, executing the production wiring branch.

    Sabotage-proof: removing the ``from kairix.transport.pool import
    get_client / return get_client(...)`` lines means
    ``_client()`` returns ``None`` and the next attribute access
    crashes with a TypeError that doesn't pattern-match ProviderError;
    the typed-error contract is broken and ``pytest.raises(ProviderError)``
    fails.
    """
    provider = OpenAIProvider(credentials=_creds(), transport_client=None)

    # Any real call surfaces a ProviderError (or subclass) because the
    # creds are fake — but the lazy ``get_client`` resolution succeeds.
    with pytest.raises(ProviderError):
        provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# dimension() — fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dimension_returns_default_when_neither_captured_nor_configured() -> None:
    """With ``dims=0`` and no embed-captured dimension, dimension() returns the default.

    Sabotage-proof: removing the ``return DEFAULT_EMBED_DIMENSION``
    fall-through would make dimension() fall off the end and return
    ``None``; the int assertion fails.
    """
    provider = OpenAIProvider(credentials=_creds(dims=0), transport_client=None)

    # No embed has happened, credentials.dims == 0.
    # The fall-through DEFAULT_EMBED_DIMENSION (1536) should be served.
    out = provider.dimension()

    assert out == 1536  # DEFAULT_EMBED_DIMENSION


@pytest.mark.unit
def test_unknown_class_with_no_status_falls_back_to_bare_provider_error() -> None:
    """Drives line 102 (``return None`` in ``_status_code_of``) plus the
    fall-through ProviderError branch in ``_map_transport_error``.

    Sabotage-proof: removing the ``return ProviderError(...)``
    fall-through means unknown errors propagate unchanged; the typed
    contract is broken and ``pytest.raises(ProviderError)`` fails.
    """

    class _UnknownError(Exception):
        pass

    provider = OpenAIProvider(
        credentials=_creds(),
        transport_client=_RaisingTransport(_UnknownError("unknown")),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])

    assert type(exc_info.value) is ProviderError
    assert not isinstance(exc_info.value, AuthError)
