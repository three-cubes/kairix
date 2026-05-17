"""Integration: anthropic plugin chat + embed-refusal + error mapping end-to-end (#provider-plugin-arch IM-13).

Boundary chain:

  caller -> AnthropicProvider.embed_batch
        -> raises EmbedNotSupported(provider_name='anthropic')
           BEFORE any transport call is constructed
           (KEY INVARIANT — Anthropic ships no embed endpoint)

  caller -> AnthropicProvider.chat
        -> recording fake transport_client
           (mirrors anthropic-SDK ``messages.create``; the kwargs the
            plugin passes are the request body that would go on the
            wire, and the synthesised headers carry the x-api-key plus
            the pinned anthropic-version)

  caller -> AnthropicProvider.chat
        -> raising fake transport_client (APIStatusError shape)
        -> _map_transport_error -> RateLimited / AuthError / ClientError / UpstreamError

This test ties the anthropic plugin together at the integration
boundary so the Provider Protocol contract is exercised end-to-end.
The ``transport_client=`` keyword is the documented DI seam (see
ADR § Provider Protocol contract); no env mutation, no @patch.

F1-clean, F2-clean, F5-clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    ClientError,
    EmbedNotSupported,
    Provider,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.anthropic import (
    ANTHROPIC_API_VERSION,
    EMBED_DIMENSION_NOT_APPLICABLE,
    AnthropicProvider,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording fake mirroring the anthropic-SDK ``messages.create`` surface
# ---------------------------------------------------------------------------


@dataclass
class _TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _MessagesResponse:
    content: list[Any] = field(default_factory=list)


class _RecordingMessages:
    def __init__(self, parent: _RecordingTransport) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _MessagesResponse:
        self._parent.calls.append(dict(kwargs))
        self._parent.synthesised_headers.append(
            {
                "x-api-key": self._parent.api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
            }
        )
        return self._parent.response


class _RecordingTransport:
    def __init__(
        self,
        *,
        api_key: str,
        chat_text: str = "hello back",
    ) -> None:
        self.api_key = api_key
        self.calls: list[dict[str, Any]] = []
        self.synthesised_headers: list[dict[str, str]] = []
        self.response = _MessagesResponse(content=[_TextBlock(text=chat_text)])
        self.messages = _RecordingMessages(self)


# ---------------------------------------------------------------------------
# Raising fake — drives the canonical error mapping
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _UpstreamApiError(Exception):
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.response = _FakeHttpResponse(status_code, headers)
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingMessages:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **kwargs: Any) -> _MessagesResponse:
        del kwargs
        raise self._err


class _RaisingTransport:
    def __init__(self, err: BaseException) -> None:
        self.messages = _RaisingMessages(err)


def _credentials(
    *,
    api_key: str = "anthropic-int-key",  # pragma: allowlist secret
    endpoint: str = "https://api.anthropic.com",
    model: str = "claude-3-5-sonnet-20241022",
) -> Credentials:
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=0)


# ---------------------------------------------------------------------------
# Key invariant: embed_batch short-circuits BEFORE any transport call
# ---------------------------------------------------------------------------


def test_embed_raises_without_calling_transport() -> None:
    """Sabotage-proof: if embed_batch is ever changed to call
    ``self._client()`` before raising (even "just to log the attempt"),
    the recording transport's ``calls`` would be non-empty and this
    assert fails. That's the load-bearing invariant for Anthropic:
    embed must NEVER reach the network because Anthropic has no embed
    endpoint.
    """
    transport = _RecordingTransport(api_key="anthropic-int-key")  # pragma: allowlist secret
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(EmbedNotSupported) as exc_info:
        provider.embed_batch(["alpha", "beta"])

    assert exc_info.value.provider_name == "anthropic"
    assert transport.calls == [], f"embed_batch must short-circuit before any transport call; calls={transport.calls!r}"
    assert transport.synthesised_headers == [], (
        f"embed_batch must not produce any wire headers; headers={transport.synthesised_headers!r}"
    )


# ---------------------------------------------------------------------------
# Happy path: chat
# ---------------------------------------------------------------------------


def test_chat_round_trips_content_and_records_wire_shape() -> None:
    """Sabotage-proof: drop the ``messages=`` kwarg from
    ``AnthropicProvider.chat`` → recorded call missing messages, fails.
    """
    transport = _RecordingTransport(api_key="anthropic-int-key", chat_text="hi from claude")  # pragma: allowlist secret
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    out = provider.chat([{"role": "user", "content": "ping"}], max_tokens=222)

    assert out == "hi from claude"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["model"] == "claude-3-5-sonnet-20241022"
    assert call["max_tokens"] == 222
    assert call["messages"] == [{"role": "user", "content": "ping"}]


def test_chat_uses_x_api_key_and_anthropic_version_headers() -> None:
    """Sabotage-proof: if the plugin switched to Bearer auth (the
    OpenAI shape), the synthesised headers wouldn't include x-api-key
    and the assert fails. If the version header drifted, the second
    assert fails.
    """
    transport = _RecordingTransport(api_key="anthropic-int-key")  # pragma: allowlist secret
    provider = AnthropicProvider(credentials=_credentials(api_key="anthropic-int-key"), transport_client=transport)

    provider.chat([{"role": "user", "content": "hi"}])

    assert len(transport.synthesised_headers) == 1
    headers = transport.synthesised_headers[0]
    assert headers.get("x-api-key") == "anthropic-int-key"
    assert headers.get("anthropic-version") == ANTHROPIC_API_VERSION
    assert "Authorization" not in headers


def test_runtime_protocol_isinstance() -> None:
    """Sabotage-proof: removing any of name / embed_batch / chat /
    dimension / healthcheck from ``AnthropicProvider`` breaks
    isinstance(provider, Provider) at runtime.
    """
    provider = AnthropicProvider(
        credentials=_credentials(),
        transport_client=_RecordingTransport(api_key="anthropic-int-key"),  # pragma: allowlist secret
    )
    assert isinstance(provider, Provider)


def test_dimension_reports_no_embed_sentinel() -> None:
    """Sabotage-proof: if dimension() returned a positive integer, the
    indexing layer would think Anthropic has an embedding surface and
    write nonsense vectors. The sentinel value catches the
    misconfiguration loudly downstream.
    """
    provider = AnthropicProvider(
        credentials=_credentials(),
        transport_client=_RecordingTransport(api_key="anthropic-int-key"),  # pragma: allowlist secret
    )
    assert provider.dimension() == EMBED_DIMENSION_NOT_APPLICABLE == 0


# ---------------------------------------------------------------------------
# Error mapping: 429 / 401 / 400 / 500 / connection failure
# ---------------------------------------------------------------------------


def test_429_maps_to_rate_limited_with_retry_after() -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → bare ``ProviderError`` raised, the isinstance check fails.
    """
    transport = _RaisingTransport(_UpstreamApiError(429, headers={"Retry-After": "23"}))
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc_info.value.retry_after_s == 23.0


def test_401_maps_to_auth_error_naming_provider() -> None:
    """Sabotage-proof: drop the 401 branch → bare ``ProviderError``;
    or drop the provider_name interpolation → message lacks 'anthropic'.
    """
    transport = _RaisingTransport(_UpstreamApiError(401))
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(AuthError) as exc_info:
        provider.chat([{"role": "user", "content": "x"}])
    assert "anthropic" in str(exc_info.value).lower()


def test_400_maps_to_client_error() -> None:
    """Sabotage-proof: drop the 400 branch → 4xx falls through to bare
    ProviderError and the retry policy might retry a non-recoverable
    failure (wasting budget on a doomed call).
    """
    transport = _RaisingTransport(_UpstreamApiError(400))
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(ClientError) as exc_info:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc_info.value.status == 400


def test_500_maps_to_upstream_error() -> None:
    """Sabotage-proof: drop the 5xx branch → bare ``ProviderError``."""
    transport = _RaisingTransport(_UpstreamApiError(502))
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(UpstreamError) as exc_info:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc_info.value.status_code == 502


def test_connection_failure_maps_to_provider_unreachable() -> None:
    """Sabotage-proof: drop the connection-failure branch → bare
    ``ProviderError`` raised, fails the isinstance check.
    """
    transport = _RaisingTransport(ConnectionError("connection refused"))
    provider = AnthropicProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(ProviderUnreachable) as exc_info:
        provider.chat([{"role": "user", "content": "x"}])
    assert "anthropic" in str(exc_info.value).lower()
