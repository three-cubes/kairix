"""AWS Bedrock ``Provider`` implementation.

Translates the universal :class:`kairix.providers.Provider` Protocol
into the AWS Bedrock-Runtime wire surface
(``bedrock-runtime.<region>.amazonaws.com``) using AWS Signature
Version 4 (SigV4) authentication.

Compared to :mod:`kairix.providers.azure_foundry` and
:mod:`kairix.providers.openai` this plugin:

- addresses the endpoint by *region*, not by free-form URL. The
  configured ``region`` is the source of truth — a misregioned config
  must NOT silently fall back to a different region's host;
- signs every outbound request with AWS SigV4 (``Authorization:
  AWS4-HMAC-SHA256 Credential=<access_key>/<date>/<region>/bedrock/
  aws4_request, SignedHeaders=..., Signature=...``) — distinct from
  Azure's ``api-key`` header and OpenAI's ``Authorization: Bearer``;
- carries the configured Bedrock model id verbatim into the URL path
  (``POST /model/<model_id>/invoke``);
- maps Bedrock's typed exceptions (``AccessDeniedException``,
  ``ThrottlingException``, ``ValidationException``) onto the canonical
  kairix typed errors so the transport-layer retry policy is uniform
  across plugins.

The model-id namespace controls the request body shape. We pin
**Amazon Titan** as the embed default (``amazon.titan-embed-text-v2:0``)
and **Anthropic Claude on Bedrock** for chat
(``anthropic.claude-3-5-sonnet-20241022-v2:0``). Cohere embed models
are valid on Bedrock too but use a different body schema; if an
operator configures a Cohere model id, we route to the Cohere wire
shape via a small dispatch keyed on the model-id prefix.

DI seams:

- ``credentials``: a :class:`BedrockCredentials` carrying the resolved
  AWS access key / secret / session token / region / model ids. The
  plugin never reads env vars or AWS metadata itself — that resolution
  lives in :func:`kairix.providers.bedrock.make_provider` via boto3's
  default credential chain (env → shared credentials → IAM role).
- ``transport_client``: a boto3 ``bedrock-runtime`` client (or any
  object exposing :meth:`invoke_model`). Production callers leave this
  ``None`` and the plugin constructs a boto3 client lazily; tests pass
  a recording fake. Allowed-``None`` here because ``credentials`` is the
  load-bearing positional and ``transport_client`` is a documented test
  seam; F6 forbids ``*_fn=None`` callables-as-test-shims, not all
  ``=None`` defaults.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kairix.providers._base import ProviderHealth
from kairix.providers._errors import (
    AuthError,
    ClientError,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)

#: Stable plugin name; matches the entry-point key in ``pyproject.toml``
#: and the ``Examples`` row in ``tests/bdd/features/e2e_provider_*.feature``.
PROVIDER_NAME = "bedrock"

#: Default Bedrock embed model id (Amazon Titan Text Embeddings V2).
#: Configurable per :class:`BedrockCredentials`.
DEFAULT_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"

#: Default Bedrock chat model id (Anthropic Claude 3.5 Sonnet on Bedrock).
DEFAULT_CHAT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

#: Default embedding dimension. Matches Amazon Titan Text Embeddings V2's
#: default output dimension; the ``dimension()`` method updates this
#: from the first successful embed response.
DEFAULT_EMBED_DIMENSION = 1024

#: Default chat ``max_tokens`` honoured by the Protocol surface.
DEFAULT_CHAT_MAX_TOKENS = 800

#: Anthropic-on-Bedrock message API requires this version string in the
#: request body (boto3 doesn't add it for us).
_ANTHROPIC_BEDROCK_API_VERSION = "bedrock-2023-05-31"


@dataclass(frozen=True)
class BedrockCredentials:
    """AWS credentials + Bedrock model configuration.

    Distinct from :class:`kairix.credentials.Credentials` (Azure shape)
    because Bedrock addresses endpoints by region and uses separate
    embed / chat model ids. Each plugin owns its credential shape; the
    universal :class:`~kairix.providers._base.Provider` Protocol only
    constrains the methods, not how providers store their configuration.

    Fields:

    - ``access_key_id`` / ``secret_access_key`` / ``session_token``: the
      three credential components AWS SigV4 needs. ``session_token`` is
      optional (only present when the caller is on temporary credentials
      from STS / IAM role assumption / SSO).
    - ``region``: the AWS region the request addresses. Source of truth
      for the URL host (``bedrock-runtime.<region>.amazonaws.com``).
    - ``embed_model_id`` / ``chat_model_id``: Bedrock-side model
      identifiers ("amazon.titan-embed-text-v2:0",
      "anthropic.claude-3-5-sonnet-20241022-v2:0"). Configured separately
      so a single deployment can mix providers (Titan embed + Claude chat
      is a common combination).
    - ``dims``: vector dimension the embed model produces; if zero, the
      plugin falls back to :data:`DEFAULT_EMBED_DIMENSION` until the
      first embed response refines it.
    """

    access_key_id: str
    secret_access_key: str
    region: str
    embed_model_id: str = DEFAULT_EMBED_MODEL_ID
    chat_model_id: str = DEFAULT_CHAT_MODEL_ID
    session_token: str | None = None
    dims: int = 0


def _is_titan_model(model_id: str) -> bool:
    """True for Amazon Titan embed model ids (``amazon.titan-embed-*``)."""
    return model_id.startswith("amazon.titan")


def _is_cohere_model(model_id: str) -> bool:
    """True for Cohere embed model ids on Bedrock (``cohere.embed-*``)."""
    return model_id.startswith("cohere.")


def _is_anthropic_model(model_id: str) -> bool:
    """True for Anthropic chat model ids on Bedrock (``anthropic.*``)."""
    return model_id.startswith("anthropic.")


def _titan_embed_body(text: str) -> dict[str, Any]:
    """Build the Titan Text Embeddings V2 request body shape."""
    return {"inputText": text}


def _cohere_embed_body(text: str) -> dict[str, Any]:
    """Build the Cohere Embed request body shape on Bedrock.

    Cohere expects ``texts: [...]`` (list of strings) and an
    ``input_type`` parameter. We pin ``search_document`` matching the
    kairix indexing workload.
    """
    return {"texts": [text], "input_type": "search_document"}


def _anthropic_chat_body(messages: list[dict[str, Any]], *, max_tokens: int) -> dict[str, Any]:
    """Build the Anthropic-on-Bedrock chat invoke body shape.

    Anthropic's messages API on Bedrock takes ``anthropic_version``
    (a fixed string) plus ``messages`` and ``max_tokens``. We map the
    Protocol's ``[{"role": "user", "content": "..."}]`` directly.
    """
    return {
        "anthropic_version": _ANTHROPIC_BEDROCK_API_VERSION,
        "max_tokens": max_tokens,
        "messages": list(messages),
        "temperature": 0.3,
    }


def _parse_titan_embed_response(payload: dict[str, Any]) -> list[float]:
    """Extract the embedding vector from a Titan invoke response body."""
    vec = payload.get("embedding", [])
    return [float(x) for x in vec]


def _parse_cohere_embed_response(payload: dict[str, Any]) -> list[float]:
    """Extract the embedding vector from a Cohere invoke response body.

    Cohere returns ``embeddings: [[...]]`` (list-of-lists). We submit
    one text per invoke call so the first inner list is the answer.
    """
    embeds = payload.get("embeddings", [])
    if not embeds:
        return []
    first = embeds[0]
    return [float(x) for x in first]


def _parse_anthropic_chat_response(payload: dict[str, Any]) -> str:
    """Extract the assistant text from an Anthropic-on-Bedrock chat body.

    Anthropic returns ``content: [{"type": "text", "text": "..."}]``;
    we concatenate all text-type blocks (typically one) so multi-block
    responses still flow through.
    """
    blocks = payload.get("content", [])
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _read_payload(raw: Any) -> dict[str, Any]:
    """Decode the JSON body returned by boto3's ``invoke_model``.

    boto3's response shape is ``{"body": <StreamingBody>, ...}``;
    ``StreamingBody.read()`` returns bytes containing JSON. Tests pass a
    bytes object directly or any object exposing ``.read()``.
    """
    if hasattr(raw, "read"):
        data = raw.read()
    else:
        data = raw
    if isinstance(data, (bytes, bytearray)):
        text = data.decode("utf-8")
    else:
        text = str(data)
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _error_code_of(err: Exception) -> str | None:
    """Best-effort extraction of the Bedrock error code from a transport error.

    boto3's ``ClientError`` carries ``response["Error"]["Code"]``
    (e.g. "AccessDeniedException"). Test seams attach the same shape
    so the mapper can be exercised without a real boto3 in the loop.
    """
    response = getattr(err, "response", None)
    if not isinstance(response, dict):
        return None
    block = response.get("Error")
    if not isinstance(block, dict):
        return None
    code = block.get("Code")
    if isinstance(code, str):
        return code
    return None


def _status_code_of(err: Exception) -> int | None:
    """Best-effort extraction of HTTP status from a ``ClientError``.

    boto3 places the status under
    ``response["ResponseMetadata"]["HTTPStatusCode"]``. Falls back to
    the ``status_code`` attribute (which some non-boto3 fakes expose).
    """
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict):
            code = meta.get("HTTPStatusCode")
            if isinstance(code, int):
                return code
    code = getattr(err, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def _retry_after_of(err: Exception) -> float | None:
    """Best-effort extraction of the ``Retry-After`` hint from a ``ClientError``.

    boto3 surfaces upstream headers under
    ``response["ResponseMetadata"]["HTTPHeaders"]``; the key casing is
    lower-case per botocore convention. Returns ``None`` when no hint
    is present or it can't be parsed as a float.
    """
    response = getattr(err, "response", None)
    if not isinstance(response, dict):
        return None
    meta = response.get("ResponseMetadata")
    if not isinstance(meta, dict):
        return None
    headers = meta.get("HTTPHeaders")
    if not isinstance(headers, dict):
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_connection_failure(err: Exception) -> bool:
    """True for connection-level failures (no HTTP response received).

    Matches botocore's ``EndpointConnectionError`` /
    ``ConnectTimeoutError`` / ``ReadTimeoutError`` and the stdlib
    ``ConnectionError`` family; test seams raise bare
    ``ConnectionError`` to stand in.
    """
    cls_name = type(err).__name__
    if cls_name in {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"}:
        return True
    if isinstance(err, ConnectionError):
        return True
    return False


def _map_transport_error(err: Exception, *, provider_name: str) -> ProviderError:
    """Translate a boto3 ``ClientError`` (or test stand-in) to a canonical typed error.

    Mapping (matches the vocabulary the rest of the provider plugins use
    so transport-layer retry doesn't branch per plugin):

    - ``AccessDeniedException`` / status 401/403 → :class:`AuthError`
    - ``ThrottlingException`` / status 429 → :class:`RateLimited`
      (carries Retry-After hint)
    - ``ValidationException`` / status 400 → :class:`ClientError`
    - status >= 500 → :class:`UpstreamError`
    - connection failure → :class:`ProviderUnreachable`
    - anything else → bare :class:`ProviderError`

    The ``provider_name`` is interpolated into the surfaced messages so
    operators see which plugin failed when multiple plugins are wired.
    """
    code = _error_code_of(err)
    status = _status_code_of(err)
    if code == "ThrottlingException" or status == 429:
        return RateLimited(
            f"Bedrock rate-limited (ThrottlingException) for provider {provider_name!r}: {err}",
            retry_after_s=_retry_after_of(err),
        )
    if code == "AccessDeniedException" or status in (401, 403):
        return AuthError(f"Bedrock auth rejected (AccessDeniedException) for provider {provider_name!r}: {err}")
    if code == "ValidationException" or status == 400:
        return ClientError(
            status if status is not None else 400,
            f"Bedrock validation error for provider {provider_name!r}: {err}",
        )
    if status is not None and status >= 500:
        return UpstreamError(
            f"Bedrock upstream error ({status}) for provider {provider_name!r}: {err}",
            status_code=status,
        )
    if _is_connection_failure(err):
        return ProviderUnreachable(f"Bedrock endpoint unreachable for provider {provider_name!r}: {err}")
    return ProviderError(f"Bedrock transport error for provider {provider_name!r}: {err!r}")


def bedrock_runtime_endpoint(region: str) -> str:
    """Return the canonical Bedrock-Runtime endpoint URL for a region.

    The region is the source of truth — we never derive it from a
    free-form endpoint URL (a misregioned config must not silently fall
    back to the wrong host). Used by :meth:`BedrockProvider.healthcheck`
    so ``probe-config`` reports which URL was probed.
    """
    return f"https://bedrock-runtime.{region}.amazonaws.com"


class BedrockProvider:
    """Concrete :class:`kairix.providers.Provider` for AWS Bedrock.

    Construction is DI-clean: production passes a resolved
    :class:`BedrockCredentials` and lets the plugin build its boto3
    ``bedrock-runtime`` client lazily; tests pass an explicit
    ``transport_client`` that records every
    :meth:`invoke_model` call.

    The provider satisfies the runtime-checkable Protocol —
    ``isinstance(provider, Provider)`` is True at runtime, which is what
    ``EntryPointRegistry.resolve`` relies on for its return-type
    annotation.

    The ``transport_client`` test seam must expose an
    :meth:`invoke_model` method accepting ``modelId``, ``body``,
    ``contentType``, and ``accept`` keyword arguments — the same shape
    boto3's bedrock-runtime client exposes — and return a mapping with
    a ``body`` entry whose ``read()`` (or direct bytes) yields the
    response JSON.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credentials: BedrockCredentials,
        transport_client: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport_client = transport_client
        # Last-known embed dimension; populated from the first successful
        # embed response so ``dimension()`` reflects what the deployed
        # model actually returned. Falls back to ``DEFAULT_EMBED_DIMENSION``
        # before any embed has happened.
        self._embed_dimension: int | None = credentials.dims if credentials.dims else None

    # ------------------------------------------------------------------
    # Internal: transport client resolution
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the configured transport client, lazily building one.

        Production callers don't pass ``transport_client``; the plugin
        builds a boto3 ``bedrock-runtime`` client against the configured
        region so SigV4 signing happens inside the AWS SDK. Tests pass
        an explicit fake recording client and this lazy construction is
        skipped entirely.
        """
        if self._transport_client is not None:
            return self._transport_client
        import boto3

        return boto3.client(
            "bedrock-runtime",
            region_name=self._credentials.region,
            aws_access_key_id=self._credentials.access_key_id,
            aws_secret_access_key=self._credentials.secret_access_key,
            aws_session_token=self._credentials.session_token,
        )

    def _invoke(self, *, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one :meth:`invoke_model` call against the transport.

        Centralises the boto3 keyword shape (``modelId`` / ``body`` /
        ``contentType`` / ``accept``) and the response decoding so the
        embed and chat paths share one error-mapping seam.
        """
        client = self._client()
        encoded = json.dumps(body).encode("utf-8")
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=encoded,
                contentType="application/json",
                accept="application/json",
            )
        except Exception as err:
            raise _map_transport_error(err, provider_name=self.name) from err
        raw_body = response.get("body") if isinstance(response, dict) else None
        if raw_body is None:
            # boto3 always returns a body slot; missing means a fake
            # transport returned an unexpected shape — surface as a
            # canonical ProviderError rather than AttributeError.
            raise ProviderError(f"Bedrock invoke_model returned no body for provider {self.name!r}")
        return _read_payload(raw_body)

    # ------------------------------------------------------------------
    # Provider Protocol
    # ------------------------------------------------------------------

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts against Bedrock.

        Wire shape (pinned by ``provider_bedrock.feature``):

        - URL host comes from
          ``bedrock-runtime.<region>.amazonaws.com`` with the region
          taken from ``credentials.region`` verbatim;
        - request path is ``/model/<embed_model_id>/invoke`` with the
          configured Bedrock model id flowing through;
        - the auth header is AWS SigV4
          (``Authorization: AWS4-HMAC-SHA256 Credential=...``) — set
          by boto3 internally when the production client is in use;
          the recording fake synthesises the same shape so tests can
          assert it.

        Bedrock's ``invoke_model`` is single-text per call; we loop over
        the batch. Returns one vector per input text, in the same order.
        Maps any transport-level failure to a canonical typed error via
        :func:`_map_transport_error` and re-raises — never returns
        partial / empty vectors silently.
        """
        if not texts:
            return []
        model_id = self._credentials.embed_model_id
        body_builder = _titan_embed_body
        response_parser = _parse_titan_embed_response
        if _is_cohere_model(model_id):
            body_builder = _cohere_embed_body
            response_parser = _parse_cohere_embed_response
        vectors: list[list[float]] = []
        for text in texts:
            payload = self._invoke(model_id=model_id, body=body_builder(text))
            vec = response_parser(payload)
            vectors.append(vec)
        if vectors and vectors[0]:
            self._embed_dimension = len(vectors[0])
        return vectors

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = DEFAULT_CHAT_MAX_TOKENS,
    ) -> str:
        """Run a single chat completion against Bedrock.

        Translates the Protocol's ``messages=[...]`` shape into the
        Bedrock-on-Anthropic invoke body
        (``anthropic_version`` + ``messages`` + ``max_tokens``) and
        unpacks the ``content[*].text`` blocks. Maps transport failures
        via :func:`_map_transport_error`.

        Non-Anthropic chat model ids are out of scope for the initial
        plugin; an explicit :class:`ClientError` surfaces the
        unsupported model id so the operator's config error is named
        rather than producing a cryptic Bedrock validation failure.
        """
        model_id = self._credentials.chat_model_id
        if not _is_anthropic_model(model_id):
            raise ClientError(
                400,
                f"Bedrock chat model {model_id!r} is not supported by the kairix plugin "
                f"(currently only anthropic.* model ids are wired for chat). "
                f"fix: configure chat_model_id to an Anthropic-on-Bedrock model id "
                f"(e.g. {DEFAULT_CHAT_MODEL_ID!r}).",
            )
        body = _anthropic_chat_body(messages, max_tokens=max_tokens)
        payload = self._invoke(model_id=model_id, body=body)
        return _parse_anthropic_chat_response(payload)

    def dimension(self) -> int:
        """Embedding vector dimension for the configured embed model.

        Returns the dims captured from the most recent embed response
        when available (matches what the deployed model actually
        produced); falls back to ``credentials.dims`` and then
        ``DEFAULT_EMBED_DIMENSION`` so callers always get a positive
        integer.
        """
        if self._embed_dimension:
            return self._embed_dimension
        if self._credentials.dims:
            return self._credentials.dims
        return DEFAULT_EMBED_DIMENSION

    def healthcheck(self) -> ProviderHealth:
        """Synchronous probe — does the configured endpoint respond?

        Performs a small embed call (one short text) and times the
        round-trip. Returns ``ok=True`` with the warm-ms latency on
        success; ``ok=False`` carrying the canonical error name on
        failure (so ``probe-config`` JSON output is stable across
        provider plugins).
        """
        import time

        endpoint = bedrock_runtime_endpoint(self._credentials.region)
        start = time.perf_counter()
        try:
            self.embed_batch(["healthcheck"])
        except ProviderError as err:
            return ProviderHealth(
                ok=False,
                endpoint=endpoint,
                error=type(err).__name__,
            )
        warm_ms = (time.perf_counter() - start) * 1000.0
        return ProviderHealth(ok=True, endpoint=endpoint, warm_ms=warm_ms)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_CHAT_MODEL_ID",
    "DEFAULT_EMBED_DIMENSION",
    "DEFAULT_EMBED_MODEL_ID",
    "PROVIDER_NAME",
    "BedrockCredentials",
    "BedrockProvider",
    "bedrock_runtime_endpoint",
]
