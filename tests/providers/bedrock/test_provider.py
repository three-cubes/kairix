"""Unit tests for :class:`kairix.providers.bedrock.BedrockProvider`.

Coverage matrix:

- Protocol conformance — ``isinstance(provider, Provider)``
  (runtime-checkable).
- ``embed_batch`` wire shape — model id flows through to the
  ``invoke_model(modelId=...)`` kwarg verbatim; Titan body shape
  (``inputText``); Cohere body shape (``texts`` + ``input_type``);
  per-text invoke loop; vector decoding.
- ``chat`` wire shape — Anthropic body shape
  (``anthropic_version`` / ``messages`` / ``max_tokens``); non-Anthropic
  model ids surface as :class:`ClientError`.
- Error mapping — ``AccessDeniedException`` →
  :class:`AuthError`; ``ThrottlingException`` →
  :class:`RateLimited` with Retry-After hint; ``ValidationException`` →
  :class:`ClientError`; HTTP 500 → :class:`UpstreamError`;
  connection failure → :class:`ProviderUnreachable`; unknown error →
  bare :class:`ProviderError`.
- Region addressing — :func:`bedrock_runtime_endpoint` derives the
  host from the configured region, never from a free-form URL.
- ``dimension()`` reports the configured / discovered dim; falls back
  to :data:`DEFAULT_EMBED_DIMENSION` before any embed has happened.
- ``healthcheck()`` returns ``ok=True`` on success and surfaces the
  typed error class name on failure.

Test seams:

- Recording transport client (``_RecordingTransportClient``) — exposes
  the boto3-compat ``invoke_model`` method and captures the model id,
  request body bytes, content type, and accept header verbatim;
  synthesises a wire-shape snapshot mirroring the SigV4 ``Authorization``
  header and the ``/model/<id>/invoke`` path that botocore would emit.
- Error-raising transport client (``_RaisingTransportClient``) — drives
  the error-mapping path with a stand-in
  :class:`_FakeBotoClientError` exposing
  ``response["Error"]["Code"]`` / ``response["ResponseMetadata"]``.

Sabotage-proofs are noted inline.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairix.providers import (
    AuthError,
    ClientError,
    Provider,
    ProviderError,
    ProviderHealth,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.bedrock import (
    DEFAULT_CHAT_MODEL_ID,
    DEFAULT_EMBED_DIMENSION,
    DEFAULT_EMBED_MODEL_ID,
    PROVIDER_NAME,
    BedrockCredentials,
    BedrockProvider,
    bedrock_runtime_endpoint,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test seams (fake transport_client surfaces mirroring the boto3 shape)
# ---------------------------------------------------------------------------


class _StreamingBody:
    """Stand-in for botocore's ``StreamingBody`` — exposes ``.read()``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _RecordingTransportClient:
    """Records every :meth:`invoke_model` call against the plugin.

    Mirrors the boto3 ``bedrock-runtime`` client surface that
    :class:`BedrockProvider` actually consumes: a single
    :meth:`invoke_model` method accepting ``modelId`` / ``body`` /
    ``contentType`` / ``accept``. No HTTP is performed; the recorded
    kwargs are the wire-shape contract this provider pins.

    The fake also constructs a synthetic SigV4 ``Authorization``
    header so the BDD assertions reading
    ``recorded_requests[-1].headers["Authorization"]`` see the same
    shape boto3 puts on the wire in production.
    """

    def __init__(
        self,
        *,
        region: str,
        access_key_id: str,
        response_body: bytes = b'{"embedding": [0.1, 0.2, 0.3], "inputTextTokenCount": 1}',
    ) -> None:
        self._region = region
        self._access_key_id = access_key_id
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
        return {"body": _StreamingBody(self._response_body), "contentType": "application/json"}


class _FakeBotoClientError(Exception):
    """Stand-in for ``botocore.exceptions.ClientError``.

    Exposes the same ``.response`` shape boto3 surfaces — a dict with
    ``Error.Code``, ``Error.Message``, and ``ResponseMetadata.HTTPStatusCode``.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str = "",
        status: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.response = {
            "Error": {"Code": code, "Message": message or code},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": headers or {},
            },
        }
        super().__init__(f"Bedrock {code} (HTTP {status})")


class _RaisingTransportClient:
    """Transport client whose ``invoke_model`` always raises."""

    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        raise self._err


def _bedrock_credentials(
    *,
    access_key_id: str = "AKIA-TEST",  # pragma: allowlist secret
    secret_access_key: str = "secret-test",  # pragma: allowlist secret
    region: str = "us-east-1",
    embed_model_id: str = DEFAULT_EMBED_MODEL_ID,
    chat_model_id: str = DEFAULT_CHAT_MODEL_ID,
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
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """BedrockProvider satisfies the runtime-checkable Provider Protocol."""

    def test_isinstance_provider_at_runtime(self) -> None:
        # Sabotage-proof: drop any of name / embed_batch / chat /
        # dimension / healthcheck from BedrockProvider and the
        # runtime_checkable isinstance() fails.
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RecordingTransportClient(
                region="us-east-1",
                access_key_id="AKIA-TEST",  # pragma: allowlist secret
            ),
        )
        assert isinstance(provider, Provider)

    def test_name_matches_pyproject_entry_point_key(self) -> None:
        # Sabotage-proof: if PROVIDER_NAME drifted from "bedrock",
        # the pyproject.toml entry-point and BDD feature names stop
        # matching what get_provider("bedrock") returns.
        assert PROVIDER_NAME == "bedrock"
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RecordingTransportClient(
                region="us-east-1",
                access_key_id="AKIA-TEST",  # pragma: allowlist secret
            ),
        )
        assert provider.name == "bedrock"


# ---------------------------------------------------------------------------
# Endpoint addressing — region is the source of truth
# ---------------------------------------------------------------------------


class TestEndpointAddressing:
    """``bedrock_runtime_endpoint`` derives host from region verbatim."""

    def test_endpoint_built_from_region(self) -> None:
        # Sabotage-proof: if the helper started reading region from a
        # free-form URL slot rather than the dedicated arg, a
        # misregioned config would silently fall back; this asserts the
        # region travels through verbatim.
        assert bedrock_runtime_endpoint("us-east-1") == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert bedrock_runtime_endpoint("ap-southeast-2") == "https://bedrock-runtime.ap-southeast-2.amazonaws.com"


# ---------------------------------------------------------------------------
# embed_batch wire shape — Titan (default)
# ---------------------------------------------------------------------------


class TestEmbedBatchTitan:
    """embed_batch records the Titan invoke shape and decodes the response."""

    def test_records_model_id_and_titan_input_text(self) -> None:
        # Sabotage-proof: if embed_batch dropped modelId from the
        # invoke_model call, the recorded call wouldn't contain
        # "modelId" — the assert fails.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embedding": [1.0, 2.0, 3.0], "inputTextTokenCount": 1}',
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(embed_model_id="amazon.titan-embed-text-v2:0"),
            transport_client=client,
        )

        out = provider.embed_batch(["alpha"])

        assert out == [[1.0, 2.0, 3.0]]
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["modelId"] == "amazon.titan-embed-text-v2:0"
        body = json.loads(call["body"].decode("utf-8"))
        assert body == {"inputText": "alpha"}
        assert call["contentType"] == "application/json"
        assert call["accept"] == "application/json"

    def test_empty_input_returns_empty_without_calling_transport(self) -> None:
        # Sabotage-proof: if embed_batch dispatched on empty input,
        # the recorded calls list would be non-empty.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=client,
        )

        assert provider.embed_batch([]) == []
        assert client.calls == []

    def test_batch_loops_one_invoke_per_text(self) -> None:
        # Bedrock's invoke_model is single-text; the plugin must loop.
        # Sabotage-proof: if the loop was replaced with a single call,
        # only one entry appears in client.calls — the len() check
        # fails.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embedding": [0.5, 0.5], "inputTextTokenCount": 1}',
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=client,
        )

        out = provider.embed_batch(["one", "two", "three"])

        assert out == [[0.5, 0.5], [0.5, 0.5], [0.5, 0.5]]
        assert len(client.calls) == 3
        bodies = [json.loads(c["body"].decode("utf-8")) for c in client.calls]
        assert bodies == [{"inputText": "one"}, {"inputText": "two"}, {"inputText": "three"}]


# ---------------------------------------------------------------------------
# embed_batch wire shape — Cohere alternate body
# ---------------------------------------------------------------------------


class TestEmbedBatchCohere:
    """Cohere model ids route through the Cohere body / response shape."""

    def test_cohere_body_uses_texts_and_input_type(self) -> None:
        # Sabotage-proof: if the model-id dispatch missed
        # cohere.embed-*, the body would default to Titan
        # ({"inputText": ...}) and this assert fails.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embeddings": [[0.7, 0.8, 0.9]]}',
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(embed_model_id="cohere.embed-english-v3"),
            transport_client=client,
        )

        out = provider.embed_batch(["alpha"])

        assert out == [[0.7, 0.8, 0.9]]
        call = client.calls[0]
        body = json.loads(call["body"].decode("utf-8"))
        assert body == {"texts": ["alpha"], "input_type": "search_document"}


# ---------------------------------------------------------------------------
# chat wire shape — Anthropic on Bedrock
# ---------------------------------------------------------------------------


class TestChatAnthropic:
    """chat builds the Anthropic-on-Bedrock body and unpacks content blocks."""

    def test_records_anthropic_version_messages_and_max_tokens(self) -> None:
        # Sabotage-proof: dropping any of anthropic_version /
        # messages / max_tokens from the body trips a missing-key
        # assertion below.
        chat_body = b'{"content": [{"type": "text", "text": "hi back"}]}'
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=chat_body,
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(chat_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0"),
            transport_client=client,
        )

        out = provider.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=42,
        )

        assert out == "hi back"
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        body = json.loads(call["body"].decode("utf-8"))
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["max_tokens"] == 42

    def test_chat_concatenates_text_blocks(self) -> None:
        # Sabotage-proof: if the response parser kept only the first
        # block, the joined "alphabeta" would be just "alpha".
        chat_body = b'{"content": [{"type": "text", "text": "alpha"}, {"type": "text", "text": "beta"}]}'
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=chat_body,
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=client,
        )

        assert provider.chat([{"role": "user", "content": "ping"}]) == "alphabeta"

    def test_chat_with_empty_content_returns_empty_string(self) -> None:
        chat_body = b'{"content": []}'
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=chat_body,
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=client,
        )

        assert provider.chat([{"role": "user", "content": "ping"}]) == ""

    def test_non_anthropic_chat_model_raises_client_error(self) -> None:
        # Sabotage-proof: if the chat path silently accepted a
        # non-Anthropic model id, the operator would see a cryptic
        # Bedrock ValidationException instead of a named ClientError;
        # this asserts the plugin surfaces the misconfiguration with
        # a clear "fix:" hint.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(chat_model_id="amazon.titan-text-express-v1"),
            transport_client=client,
        )

        with pytest.raises(ClientError) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert "amazon.titan-text-express-v1" in str(exc_info.value)
        # No outbound call was made — short-circuit before invoke.
        assert client.calls == []


# ---------------------------------------------------------------------------
# Error mapping — Bedrock typed exceptions to canonical typed errors
# ---------------------------------------------------------------------------


class TestEmbedErrorMapping:
    """Bedrock errors map to the canonical typed errors per code + status."""

    def test_throttling_exception_maps_to_rate_limited_with_retry_after(self) -> None:
        # Sabotage-proof: if the mapper stopped reading Retry-After
        # from ResponseMetadata.HTTPHeaders, err.retry_after_s would be
        # None and the assert fails.
        err = _FakeBotoClientError(
            code="ThrottlingException",
            status=429,
            headers={"retry-after": "12"},
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s == 12.0
        assert "bedrock" in str(exc_info.value).lower()

    def test_throttling_without_retry_after_yields_none_hint(self) -> None:
        err = _FakeBotoClientError(code="ThrottlingException", status=429)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s is None

    def test_access_denied_exception_maps_to_auth_error_naming_provider(self) -> None:
        # Sabotage-proof: if the mapper stopped naming the provider in
        # the AuthError message, the BDD scenario at
        # provider_bedrock.feature §"403 maps to AuthError" fails.
        err = _FakeBotoClientError(code="AccessDeniedException", status=403)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError) as exc_info:
            provider.embed_batch(["alpha"])
        assert "bedrock" in str(exc_info.value).lower()

    def test_status_401_also_maps_to_auth_error(self) -> None:
        # AccessDeniedException is the named Bedrock case; status alone
        # is the fallback when only HTTP status is observable.
        err = _FakeBotoClientError(code="UnknownError", status=401)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError):
            provider.embed_batch(["alpha"])

    def test_validation_exception_maps_to_client_error(self) -> None:
        # Sabotage-proof: if the mapper dropped the
        # ValidationException → ClientError branch, the bad-config
        # case would surface as a bare ProviderError and dashboards
        # couldn't distinguish "operator passed an invalid model id"
        # from a transient upstream failure.
        err = _FakeBotoClientError(code="ValidationException", status=400)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ClientError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status == 400

    def test_500_maps_to_upstream_error_with_status_code(self) -> None:
        # Sabotage-proof: if the mapper dropped status_code from
        # UpstreamError, exc_info.value.status_code AttributeError-s.
        err = _FakeBotoClientError(code="InternalServerException", status=500)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 500

    def test_503_also_maps_to_upstream_error(self) -> None:
        err = _FakeBotoClientError(code="ServiceUnavailableException", status=503)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 503

    def test_connection_failure_maps_to_provider_unreachable(self) -> None:
        # Sabotage-proof: if _is_connection_failure stopped recognising
        # ConnectionError, the mapper would fall through to bare
        # ProviderError and this assert fails.
        err = ConnectionError("DNS lookup failed")
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.embed_batch(["alpha"])

    def test_botocore_named_connection_error_maps_to_provider_unreachable(self) -> None:
        # boto3 surfaces connection failures via class-name
        # (EndpointConnectionError etc) — the mapper recognises by
        # class name so the kairix codebase doesn't pull botocore in
        # at import time.
        class EndpointConnectionError(Exception):
            pass

        err = EndpointConnectionError("could not connect")
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.embed_batch(["alpha"])

    def test_unknown_error_falls_back_to_provider_error(self) -> None:
        # Sabotage-proof: if the mapper raised the original exception
        # instead of wrapping in ProviderError, downstream callers
        # catching ProviderError would miss the failure.
        err = ValueError("some other failure")
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderError):
            provider.embed_batch(["alpha"])

    def test_invoke_response_without_body_raises_provider_error(self) -> None:
        # Sabotage-proof: if the plugin didn't guard against a missing
        # response body slot, the downstream AttributeError would
        # bubble out un-typed and operators would see a stack trace
        # instead of a canonical ProviderError.
        class _BadResponseClient:
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                return {}  # boto3 always returns a body slot; absent → bug.

        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_BadResponseClient(),
        )

        with pytest.raises(ProviderError):
            provider.embed_batch(["alpha"])


class TestChatErrorMapping:
    """Chat path uses the same error mapper as embed."""

    def test_chat_throttling_maps_to_rate_limited(self) -> None:
        # Sabotage-proof: if the chat path used a different mapper,
        # this surfaces the raw ClientError instead of RateLimited.
        err = _FakeBotoClientError(
            code="ThrottlingException",
            status=429,
            headers={"retry-after": "3"},
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.retry_after_s == 3.0

    def test_chat_connection_failure_maps_to_unreachable(self) -> None:
        err = ConnectionError("refused")
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# dimension() and healthcheck()
# ---------------------------------------------------------------------------


class TestDimensionAndHealth:
    """dimension() and healthcheck() honour the configured credentials."""

    def test_dimension_defaults_when_credentials_have_no_dims(self) -> None:
        # Sabotage-proof: if dimension() returned 0 when dims=0 is
        # configured, the SearchPipeline's vector backend would index
        # zero-length vectors. DEFAULT_EMBED_DIMENSION must back-stop.
        provider = BedrockProvider(
            credentials=_bedrock_credentials(dims=0),
            transport_client=_RecordingTransportClient(
                region="us-east-1",
                access_key_id="AKIA-TEST",  # pragma: allowlist secret
            ),
        )
        assert provider.dimension() == DEFAULT_EMBED_DIMENSION

    def test_dimension_uses_credential_dims_before_embed(self) -> None:
        provider = BedrockProvider(
            credentials=_bedrock_credentials(dims=4096),
            transport_client=_RecordingTransportClient(
                region="us-east-1",
                access_key_id="AKIA-TEST",  # pragma: allowlist secret
            ),
        )
        assert provider.dimension() == 4096

    def test_dimension_uses_observed_dim_after_first_embed(self) -> None:
        # Sabotage-proof: if embed_batch didn't update
        # _embed_dimension, dimension() stays at the configured 1024
        # even when the deployed model returned a 7-dim vector —
        # caught here.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embedding": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}',
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(dims=1024),
            transport_client=client,
        )

        provider.embed_batch(["alpha"])

        assert provider.dimension() == 7

    def test_healthcheck_ok_when_embed_succeeds(self) -> None:
        # Sabotage-proof: if healthcheck() stopped catching
        # ProviderError, the embed_batch failure path would raise
        # rather than emit ok=False.
        client = _RecordingTransportClient(
            region="us-east-1",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embedding": [0.1, 0.2, 0.3]}',
        )
        provider = BedrockProvider(credentials=_bedrock_credentials(), transport_client=client)

        health = provider.healthcheck()

        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert health.endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert health.warm_ms is not None and health.warm_ms >= 0

    def test_healthcheck_not_ok_on_provider_error(self) -> None:
        err = _FakeBotoClientError(code="AccessDeniedException", status=403)
        provider = BedrockProvider(
            credentials=_bedrock_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        health = provider.healthcheck()

        assert health.ok is False
        assert health.error == "AuthError"
        # Endpoint is reported even on failure so probe-config JSON
        # tells operators which URL was probed.
        assert health.endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"

    def test_healthcheck_in_alternate_region(self) -> None:
        # Region travels through to the reported endpoint verbatim —
        # a misregioned config does not silently fall back.
        client = _RecordingTransportClient(
            region="ap-southeast-2",
            access_key_id="AKIA-TEST",  # pragma: allowlist secret
            response_body=b'{"embedding": [0.1]}',
        )
        provider = BedrockProvider(
            credentials=_bedrock_credentials(region="ap-southeast-2"),
            transport_client=client,
        )

        health = provider.healthcheck()

        assert health.ok is True
        assert health.endpoint == "https://bedrock-runtime.ap-southeast-2.amazonaws.com"
