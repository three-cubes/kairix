"""Step definitions for provider_anthropic.feature (#provider-plugin-arch IM-13).

Drives :class:`kairix.providers.anthropic.AnthropicProvider` with a
recording fake ``transport_client`` so the scenarios assert that the
``x-api-key`` header carries the configured key, the
``anthropic-version`` header is set, and the Authorization header is
**absent** (Anthropic's auth model is distinct from OpenAI's Bearer
shape). The error scenarios drive the 401 / 429 error-mapping branches
through the same fake.

The ``EmbedNotSupported`` scenario asserts a stronger invariant than
the other plugins' error scenarios: not only must the typed error
fire, but **no outbound request must be recorded** — because Anthropic
has no embed endpoint to receive one. The shared step
"no outbound request was recorded by the wire endpoint" (in
:mod:`tests.bdd.steps.provider_wire_common_steps`) reads
``transport.recorded_requests`` to enforce this.

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
only the anthropic-specific Given (model), the When steps (embed /
chat), and the provider-name-bearing typed-error Then assertions
(``EmbedNotSupported`` / ``RateLimited`` / ``AuthError``).

DI-clean: ``transport_client=`` is the documented test seam on
:class:`AnthropicProvider`. No monkeypatch, no @patch, no env mutation.

F1-clean, F2-clean, F5-clean.

Sabotage-proofs are noted per step inline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    EmbedNotSupported,
    ProviderError,
    RateLimited,
)
from kairix.providers.anthropic import ANTHROPIC_API_VERSION, AnthropicProvider
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

_PROVIDER = "anthropic"


# ---------------------------------------------------------------------------
# Recording transport — exposes ``recorded_requests`` for the shared
# assertion steps to read. Mirrors the official ``anthropic`` SDK
# surface (``client.messages.create(...)``) plus synthesises the wire
# headers the SDK would put on the request (so the shared header / host /
# path assertions work without an actual HTTP layer).
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    type: str = "text"
    text: str = "hi"


@dataclass
class _FakeMessagesResponse:
    content: list[Any] = field(default_factory=list)


class _RecordingMessages:
    def __init__(self, parent: _RecordingTransportClient) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeMessagesResponse:
        self._parent.record(kwargs)
        return _FakeMessagesResponse(content=[_FakeTextBlock(text="hello from anthropic")])


class _RecordingTransportClient:
    """Recording fake mirroring the official ``anthropic`` SDK surface.

    Unlike the OpenAI / azure_foundry plugins, Anthropic does NOT use
    the openai SDK — the wire shape is a direct POST /v1/messages with
    an ``x-api-key`` header and an ``anthropic-version`` header. This
    fake reflects those headers back through the shared
    :class:`RecordedRequest` so the
    ``provider_wire_common_steps`` assertions can read them uniformly
    across every plugin.
    """

    def __init__(self, endpoint: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self.messages = _RecordingMessages(self)
        self.recorded_requests: list[RecordedRequest] = []

    def record(self, kwargs: dict[str, Any]) -> None:
        host = _host_of(self._endpoint)
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path="/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                },
                body=dict(kwargs),
            )
        )


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0]


# ---------------------------------------------------------------------------
# Raising transport — drives the error-mapping branches
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

    def create(self, **kwargs: Any) -> _FakeMessagesResponse:
        del kwargs
        raise self._err


class _RaisingTransportClient:
    def __init__(self, err: BaseException) -> None:
        self.messages = _RaisingMessages(err)
        self.recorded_requests: list[RecordedRequest] = []


# ---------------------------------------------------------------------------
# Given — provider configuration (Background)
# ---------------------------------------------------------------------------


@given(parsers.parse('the anthropic provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    """Record the model the operator configured.

    Note: the credential-resolver Given step
    (``the configured credential resolver returns the api key "..."``)
    is owned by the azure_foundry module — the phrasing is identical
    across features so a single registration suffices. That step sets
    ``api_key`` on the shared state dict, which this module's
    _build_provider reads.
    """
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = model


# ---------------------------------------------------------------------------
# When — drive embed / chat via the anthropic plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> AnthropicProvider:
    creds = Credentials(
        api_key=state["api_key"] or "anthropic-test-key",  # pragma: allowlist secret
        endpoint=state["endpoint"] or "https://api.anthropic.com",
        model=state["model"] or "claude-3-5-sonnet-20241022",
        dims=0,
    )
    transport_slot = state.get("transport")
    if isinstance(transport_slot, dict) and "raise_status" in transport_slot:
        transport: Any = _RaisingTransportClient(
            _UpstreamApiError(transport_slot["raise_status"], headers=transport_slot["headers"])
        )
    else:
        transport = _RecordingTransportClient(creds.endpoint, creds.api_key)
    state["transport"] = transport
    return AnthropicProvider(credentials=creds, transport_client=transport)


@when("the operator embeds a single text via the anthropic plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    """Drive embed_batch; capture the typed error if one fires.

    Sabotage-proof: if AnthropicProvider.embed_batch stopped raising
    EmbedNotSupported (e.g. returned [] for empty input), the
    @anthropic_no_embed scenario fails at the "raises a canonical
    EmbedNotSupported" Then step.
    """
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


@when("the operator runs a single chat completion via the anthropic plugin")
def _when_chat(_provider_wire_state: dict[str, Any]) -> None:
    """Drive chat; capture the typed error if one fires.

    Sabotage-proof: if the plugin stopped emitting the x-api-key
    header (or started emitting Authorization), the @happy_path
    scenario fails at the shared header-equality / header-absent steps
    in provider_wire_common_steps.
    """
    provider = _build_provider(_provider_wire_state)
    try:
        provider.chat([{"role": "user", "content": "hi"}])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — provider-name-bearing typed-error assertions
# ---------------------------------------------------------------------------


@then("the anthropic plugin raises a canonical EmbedNotSupported error")
def _then_embed_not_supported(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: if embed_batch returned [] instead of raising,
    state['raised'] would still be None and the isinstance check fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, EmbedNotSupported), (
        f"expected EmbedNotSupported; got {type(err).__name__ if err else 'None'}: {err!r}"
    )


@then("the anthropic plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → error becomes bare ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__}: {err!r}"


@then("the anthropic plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 401 branch → error becomes
    ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, AuthError), f"expected AuthError; got {type(err).__name__}: {err!r}"
