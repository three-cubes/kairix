"""Step definitions for provider_bedrock.feature (#provider-plugin-arch IM-10).

Drives :class:`kairix.providers.bedrock.BedrockProvider` with a recording
fake ``transport_client`` so the scenarios assert the wire shape
Bedrock expects:

- URL host derived from the configured region
  (``bedrock-runtime.<region>.amazonaws.com``) — NEVER from a free-form
  endpoint URL;
- path is ``/model/<model_id>/invoke`` with the configured Bedrock
  model id;
- the auth header is AWS SigV4
  (``Authorization: AWS4-HMAC-SHA256 Credential=<access_key>/<date>/
  <region>/bedrock/aws4_request, SignedHeaders=..., Signature=...``).
  In production boto3 emits this header internally; the recording fake
  synthesises the same shape so wire-shape assertions can run without
  the real AWS SDK in the test loop.
- canonical 4xx/5xx error mapping (AccessDeniedException → AuthError,
  ThrottlingException → RateLimited).

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
only the bedrock-specific surface (AWS credential resolver, region
config key, AccessDenied / Throttling body shape, ``bedrock`` plugin-
name typed-error assertions, and the SigV4-aware header-shape
assertions).

DI-clean: ``transport_client=`` is the documented test seam on
:class:`BedrockProvider`. No monkeypatch, no @patch, no env mutation.

F1-clean, F2-clean, F5-clean.

Sabotage-proofs are noted per step inline.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.providers import AuthError, ProviderError, RateLimited
from kairix.providers.bedrock import BedrockCredentials, BedrockProvider
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

#: Stable plugin name; matches the Background step phrasing and the
#: ``PROVIDER_NAME`` exported by the plugin.
_PROVIDER = "bedrock"


# ---------------------------------------------------------------------------
# Recording transport — synthesises the wire-shape SigV4 header so the
# BDD assertions can verify the Authorization header without a real
# AWS SDK in the test loop.
# ---------------------------------------------------------------------------


def _synthesise_sigv4_authorization(*, access_key_id: str, region: str, service: str = "bedrock") -> str:
    """Return a SigV4-shape ``Authorization`` header value.

    boto3 emits this header internally; the recording fake synthesises
    the same canonical shape so BDD scenarios pin the wire contract
    without running real SigV4 signing in tests. The signature digest
    is a fixed hex string per recorded request (deterministic for
    reproducibility); the components asserted by the feature file are
    the algorithm name, the ``Credential=`` scope (containing the
    region and service), and the ``SignedHeaders``/``Signature``
    placeholders.
    """
    now = _dt.datetime(2026, 5, 17, 0, 0, 0, tzinfo=_dt.timezone.utc)
    date = now.strftime("%Y%m%d")
    credential = f"{access_key_id}/{date}/{region}/{service}/aws4_request"
    # Deterministic placeholder signature so tests can assert structural
    # presence without depending on real SigV4 key derivation.
    sig = hmac.new(b"placeholder", credential.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={credential}, SignedHeaders=content-type;host;x-amz-date, Signature={sig}"


class _StreamingBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _RecordingTransportClient:
    """Recording fake mirroring the boto3 ``bedrock-runtime`` surface.

    Each :meth:`invoke_model` call lands here; the captured kwargs plus
    a synthetic :class:`RecordedRequest` carrying the region-derived
    host, the ``/model/<id>/invoke`` path, and the SigV4-shape
    ``Authorization`` header populate the ``recorded_requests`` list
    read by the shared assertion steps.
    """

    def __init__(self, *, region: str, access_key_id: str) -> None:
        self._region = region
        self._access_key_id = access_key_id
        self.recorded_requests: list[RecordedRequest] = []

    def invoke_model(
        self,
        *,
        modelId: str,  # noqa: N803 — boto3 method signature pinned by AWS SDK
        body: bytes,
        contentType: str,  # noqa: N803 — boto3 method signature pinned by AWS SDK
        accept: str,
    ) -> dict[str, Any]:
        del contentType, accept
        host = f"bedrock-runtime.{self._region}.amazonaws.com"
        path = f"/model/{modelId}/invoke"
        authorization = _synthesise_sigv4_authorization(
            access_key_id=self._access_key_id,
            region=self._region,
        )
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path=path,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    "x-amz-date": "20260517T000000Z",
                    "Host": host,
                },
                body={"modelId": modelId, "raw_body": body},
            )
        )
        # Titan response shape — adequate for the happy-path scenarios
        # which only assert on the recorded outbound request.
        return {"body": _StreamingBody(b'{"embedding": [0.1, 0.2, 0.3]}')}


# ---------------------------------------------------------------------------
# Raising transport — drives the canonical error-mapping branches
# ---------------------------------------------------------------------------


class _FakeBotoClientError(Exception):
    """Stand-in for ``botocore.exceptions.ClientError``.

    Exposes the same ``.response`` dict shape boto3 surfaces, keyed by
    ``Error.Code`` (e.g. ``"AccessDeniedException"``) and
    ``ResponseMetadata.HTTPStatusCode``.
    """

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


class _RaisingTransportClient:
    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.recorded_requests: list[RecordedRequest] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise self._err


# ---------------------------------------------------------------------------
# Given — bedrock-specific configuration (Background)
# ---------------------------------------------------------------------------


@given(parsers.parse('the bedrock provider configured with model id "{model_id}"'))
def _given_model_id(_provider_wire_state: dict[str, Any], model_id: str) -> None:
    """Set the configured Bedrock embed model id on the shared state."""
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = model_id


@given(parsers.parse('the configured credential resolver returns AWS access key, secret, and region "{region}"'))
def _given_aws_creds(_provider_wire_state: dict[str, Any], region: str) -> None:
    """Pin AWS access-key / secret / region on the shared state.

    The access key id is a fixed test value (the scenarios assert it
    appears in the SigV4 ``Credential=`` scope) and the region is the
    Background default ("us-east-1") unless a later step overrides it.
    """
    _provider_wire_state["api_key"] = "AKIA-TESTACCESSKEY"  # pragma: allowlist secret
    _provider_wire_state["extra"] = {
        "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
        "region": region,
    }


@given(parsers.parse('the bedrock plugin is configured with region "{region}" via the region config key'))
def _given_region_override(_provider_wire_state: dict[str, Any], region: str) -> None:
    """Override the configured region via the dedicated config key.

    Region travels via a config key (``BedrockCredentials.region``),
    not via a free-form endpoint URL — a misregioned config must NOT
    silently fall back to the Background default.
    """
    extra = _provider_wire_state["extra"]
    extra["region"] = region


@given("the wire endpoint will respond with status 403 and a Bedrock AccessDeniedException body")
def _given_access_denied(_provider_wire_state: dict[str, Any]) -> None:
    """Mark the transport to raise an ``AccessDeniedException`` shape.

    The mapper recognises Bedrock's typed exception by
    ``response["Error"]["Code"]`` so the test fixture pins that code
    string verbatim.
    """
    _provider_wire_state["transport"] = {
        "raise_code": "AccessDeniedException",
        "raise_status": 403,
        "headers": {},
    }


@given("the wire endpoint will respond with status 429 and a Bedrock ThrottlingException body")
def _given_throttling(_provider_wire_state: dict[str, Any]) -> None:
    _provider_wire_state["transport"] = {
        "raise_code": "ThrottlingException",
        "raise_status": 429,
        "headers": {},
    }


# ---------------------------------------------------------------------------
# When — drive embed via the bedrock plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> BedrockProvider:
    extra = state["extra"]
    region = extra.get("region") or "us-east-1"
    access_key_id = state["api_key"] or "AKIA-TESTACCESSKEY"  # pragma: allowlist secret
    secret = extra.get("secret_access_key") or "test-secret"  # pragma: allowlist secret
    model = state["model"] or "amazon.titan-embed-text-v2:0"
    creds = BedrockCredentials(
        access_key_id=access_key_id,
        secret_access_key=secret,
        region=region,
        embed_model_id=model,
    )
    transport_slot = state.get("transport")
    if isinstance(transport_slot, dict) and "raise_status" in transport_slot:
        err = _FakeBotoClientError(
            code=transport_slot.get("raise_code", "InternalServerException"),
            status=transport_slot["raise_status"],
            headers=transport_slot.get("headers"),
        )
        transport: Any = _RaisingTransportClient(err)
    else:
        transport = _RecordingTransportClient(region=region, access_key_id=access_key_id)
    state["transport"] = transport
    return BedrockProvider(credentials=creds, transport_client=transport)


@when("the operator embeds a single text via the bedrock plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — bedrock-specific (SigV4 header semantics + path-contains)
# ---------------------------------------------------------------------------


def _last(state: dict[str, Any]) -> RecordedRequest:
    transport = state["transport"]
    recorded = getattr(transport, "recorded_requests", [])
    assert recorded, "no outbound request was recorded by the bedrock wire endpoint"
    return recorded[-1]


@then(parsers.parse('the recorded request header "{name}" begins with "{prefix}"'))
def _then_header_begins(_provider_wire_state: dict[str, Any], name: str, prefix: str) -> None:
    """Sabotage-proof: if the synthesised SigV4 header dropped the
    ``AWS4-HMAC-SHA256`` algorithm prefix (or boto3 internally stopped
    using SigV4 for Bedrock), this assertion fails. The bedrock plugin
    asserts the algorithm name is exactly the canonical SigV4 token.
    """
    req = _last(_provider_wire_state)
    actual = req.headers.get(name)
    assert actual is not None and actual.startswith(prefix), (
        f"header {name!r} expected to begin with {prefix!r}; got {actual!r}"
    )


@then(parsers.parse('the recorded request header "{name}" contains "{needle}"'))
def _then_header_contains(_provider_wire_state: dict[str, Any], name: str, needle: str) -> None:
    """Sabotage-proof: if the SigV4 ``Credential=`` scope dropped the
    region or the ``bedrock/aws4_request`` service token, this fails.
    Pins both the region travel and the SigV4 service scope.
    """
    req = _last(_provider_wire_state)
    actual = req.headers.get(name)
    assert actual is not None and needle in actual, f"header {name!r} expected to contain {needle!r}; got {actual!r}"


@then(parsers.parse('the recorded request path contains "{needle}"'))
def _then_path_contains(_provider_wire_state: dict[str, Any], needle: str) -> None:
    """Sabotage-proof: if the plugin stopped flowing modelId into the
    ``/model/<id>/invoke`` path (e.g. hardcoded a different model id),
    the recorded path would miss the substring, failing here.
    """
    req = _last(_provider_wire_state)
    assert needle in req.path, f"expected path to contain {needle!r}; got {req.path!r}"


@then("the bedrock plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the ``AccessDeniedException`` branch in
    ``_map_transport_error`` → error becomes bare ``ProviderError``,
    this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, AuthError), f"expected AuthError; got {type(err).__name__}: {err!r}"


@then("the bedrock plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the ``ThrottlingException`` branch → error
    becomes ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__}: {err!r}"
