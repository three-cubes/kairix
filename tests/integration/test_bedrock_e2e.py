"""Integration: bedrock plugin embed + chat + error mapping end-to-end (#provider-plugin-arch IM-10).

Boundary chain:

  caller -> BedrockProvider.embed_batch / .chat
        -> recording fake transport_client (records the kwargs +
           synthesises a wire-shape snapshot mirroring what boto3's
           bedrock-runtime client would put on the wire after SigV4
           signing)

  caller -> BedrockProvider.embed_batch
        -> raising fake transport_client (raises a
           ``botocore.exceptions.ClientError``-shaped exception)
        -> _map_transport_error -> RateLimited / AuthError /
           UpstreamError / ProviderUnreachable / ClientError

This integration test drives the plugin through its production
constructor + production methods; the only test seam is the
``transport_client=`` keyword call-out documented in the ADR as the
plugin's DI seam. No env monkeypatch, no @patch on internals.

The unit tests under ``tests/providers/bedrock/`` cover the helper-
level branches; this file ties the whole plugin together at the
integration boundary so the Provider Protocol contract is exercised
end-to-end (embed → record → assertion + error → typed-mapping →
assertion) and the region-derived host is correct in alternate regions.

F1-clean, F2-clean, F5-clean.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairix.providers import (
    AuthError,
    ClientError,
    Provider,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.bedrock import (
    BedrockCredentials,
    BedrockProvider,
    bedrock_runtime_endpoint,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording fake mirroring the boto3 bedrock-runtime surface
# ---------------------------------------------------------------------------


class _StreamingBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _RecordingTransport:
    """boto3-compat fake — records every ``invoke_model`` call."""

    def __init__(self, response_body: bytes) -> None:
        self._response_body = response_body
        self.calls: list[dict[str, Any]] = []

    def invoke_model(
        self,
        *,
        modelId: str,  # noqa: N803 — boto3 method signature pinned by AWS SDK
        body: bytes,
        contentType: str,  # noqa: N803 — boto3 method signature pinned by AWS SDK
        accept: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "modelId": modelId,
                "body": body,
                "contentType": contentType,
                "accept": accept,
            }
        )
        return {"body": _StreamingBody(self._response_body)}


# ---------------------------------------------------------------------------
# Raising fake — drives the canonical error mapping
# ---------------------------------------------------------------------------


class _FakeBotoClientError(Exception):
    """Stand-in for ``botocore.exceptions.ClientError``."""

    def __init__(
        self,
        *,
        code: str,
        status: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.response = {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": headers or {},
            },
        }
        super().__init__(f"Bedrock {code} (HTTP {status})")


class _RaisingTransport:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def invoke_model(self, **_: Any) -> dict[str, Any]:
        raise self._err


def _credentials(
    *,
    access_key_id: str = "AKIA-INT-TEST",  # pragma: allowlist secret
    secret_access_key: str = "secret-int-test",  # pragma: allowlist secret
    region: str = "us-east-1",
    embed_model_id: str = "amazon.titan-embed-text-v2:0",
    chat_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
    dims: int = 1024,
) -> BedrockCredentials:
    return BedrockCredentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=region,
        embed_model_id=embed_model_id,
        chat_model_id=chat_model_id,
        dims=dims,
    )


# ---------------------------------------------------------------------------
# Happy path: embed + chat
# ---------------------------------------------------------------------------


def test_embed_round_trips_titan_vector_and_records_wire_shape() -> None:
    """Sabotage-proof: drop the modelId kwarg from
    :meth:`BedrockProvider._invoke` → recorded call has no modelId key,
    the assertion below fails.
    """
    transport = _RecordingTransport(response_body=b'{"embedding": [0.7, 0.8, 0.9], "inputTextTokenCount": 1}')
    provider = BedrockProvider(
        credentials=_credentials(embed_model_id="amazon.titan-embed-text-v2:0"),
        transport_client=transport,
    )

    vectors = provider.embed_batch(["alpha"])

    assert vectors == [[0.7, 0.8, 0.9]]
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["modelId"] == "amazon.titan-embed-text-v2:0"
    body = json.loads(call["body"].decode("utf-8"))
    assert body == {"inputText": "alpha"}


def test_chat_round_trips_anthropic_response_and_records_wire_shape() -> None:
    """Sabotage-proof: drop the max_tokens kwarg from
    :meth:`BedrockProvider.chat` → recorded body has no max_tokens,
    fails.
    """
    transport = _RecordingTransport(response_body=b'{"content": [{"type": "text", "text": "hi from bedrock"}]}')
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    out = provider.chat([{"role": "user", "content": "ping"}], max_tokens=123)

    assert out == "hi from bedrock"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    body = json.loads(call["body"].decode("utf-8"))
    assert body["max_tokens"] == 123
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["anthropic_version"] == "bedrock-2023-05-31"


def test_runtime_protocol_isinstance() -> None:
    """The instantiated provider satisfies the runtime-checkable Protocol."""
    provider = BedrockProvider(
        credentials=_credentials(),
        transport_client=_RecordingTransport(b'{"embedding": [0.0]}'),
    )
    assert isinstance(provider, Provider)


def test_region_drives_endpoint_host() -> None:
    """Sabotage-proof: if region travelled via a free-form endpoint URL
    rather than the dedicated config key, an alternate-region config
    would silently fall back. The helper derives the host from region
    verbatim.
    """
    assert bedrock_runtime_endpoint("eu-central-1") == "https://bedrock-runtime.eu-central-1.amazonaws.com"


# ---------------------------------------------------------------------------
# Error mapping: ThrottlingException / AccessDenied / ValidationException /
# 5xx / connection failure
# ---------------------------------------------------------------------------


def test_throttling_exception_maps_to_rate_limited_with_retry_after() -> None:
    """Sabotage-proof: drop the ThrottlingException branch in
    ``_map_transport_error`` → error becomes bare ProviderError, the
    isinstance check fails.
    """
    err = _FakeBotoClientError(code="ThrottlingException", status=429, headers={"retry-after": "15"})
    transport = _RaisingTransport(err)
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(RateLimited) as exc_info:
        provider.embed_batch(["x"])
    assert exc_info.value.retry_after_s == 15.0


def test_access_denied_exception_maps_to_auth_error() -> None:
    """Sabotage-proof: drop the AccessDeniedException branch → bare
    ProviderError raised, the isinstance(AuthError) check fails.
    """
    err = _FakeBotoClientError(code="AccessDeniedException", status=403)
    transport = _RaisingTransport(err)
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(AuthError) as exc_info:
        provider.embed_batch(["x"])
    assert "bedrock" in str(exc_info.value).lower()


def test_validation_exception_maps_to_client_error() -> None:
    """Sabotage-proof: drop the ValidationException branch → bad
    operator config surfaces as bare ProviderError, losing the
    distinction transport retry policy uses to short-circuit.
    """
    err = _FakeBotoClientError(code="ValidationException", status=400)
    transport = _RaisingTransport(err)
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(ClientError):
        provider.embed_batch(["x"])


def test_500_maps_to_upstream_error() -> None:
    """Sabotage-proof: drop the 5xx branch → bare ProviderError."""
    err = _FakeBotoClientError(code="InternalServerException", status=503)
    transport = _RaisingTransport(err)
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(UpstreamError) as exc_info:
        provider.embed_batch(["x"])
    assert exc_info.value.status_code == 503


def test_connection_failure_maps_to_provider_unreachable() -> None:
    """Sabotage-proof: drop the connection-failure branch → bare
    ProviderError, the isinstance check fails.
    """
    transport = _RaisingTransport(ConnectionError("DNS resolution failed"))
    provider = BedrockProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(ProviderUnreachable) as exc_info:
        provider.embed_batch(["x"])
    assert "bedrock" in str(exc_info.value).lower()
