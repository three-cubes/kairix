"""Step definitions for provider_openai.feature (#provider-plugin-arch IM-7).

Drives :class:`kairix.providers.openai.OpenAIProvider` with a recording
fake ``transport_client`` so the scenarios assert that the configured
``base_url`` flows through verbatim (no Foundry suffix), the auth header
is ``Authorization: Bearer <api_key>`` (distinct from the ``api-key``
header used by AzureOpenAI), and the canonical 4xx/5xx error mapping
matches the rest of the provider layer.

Shared step phrases live in :mod:`tests.bdd.steps.provider_wire_common_steps`;
this module owns only the OpenAI-specific Background Given, the When
step, and the provider-name-bearing typed-error Then assertions.

DI-clean: ``transport_client=`` is the documented test seam on
:class:`OpenAIProvider`. No monkeypatch, no @patch, no env mutation.

F1-clean, F2-clean, F5-clean.

Sabotage-proofs are noted per step inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.credentials import Credentials
from kairix.providers import AuthError, ProviderError, RateLimited, UpstreamError
from kairix.providers.openai import OpenAIProvider
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

_PROVIDER = "openai"


# ---------------------------------------------------------------------------
# Recording transport — exposes ``recorded_requests`` for the shared
# assertion steps to read.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


@dataclass
class _FakeChatMessage:
    content: str | None


@dataclass
class _FakeChatChoice:
    message: _FakeChatMessage


@dataclass
class _FakeChatResponse:
    choices: list[_FakeChatChoice]


class _RecordingEmbeddings:
    def __init__(self, parent: _RecordingTransportClient) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self._parent.record(kwargs, kind="embed")
        return _FakeEmbeddingResponse(
            data=[_FakeEmbeddingItem(embedding=[0.1, 0.2, 0.3]) for _ in kwargs.get("input", [])]
        )


class _RecordingChatCompletions:
    def __init__(self, parent: _RecordingTransportClient) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        self._parent.record(kwargs, kind="chat")
        return _FakeChatResponse(choices=[_FakeChatChoice(message=_FakeChatMessage(content="hi"))])


class _RecordingChat:
    def __init__(self, parent: _RecordingTransportClient) -> None:
        self.completions = _RecordingChatCompletions(parent)


class _RecordingTransportClient:
    """Recording fake mirroring the openai-SDK ``OpenAI`` surface.

    Unlike the azure_foundry fake, this one does NOT append
    ``/openai/v1`` to the endpoint — OpenAI-direct operators configure
    the full ``base_url`` themselves (typically
    ``https://api.openai.com/v1``). The recorded header is
    ``Authorization: Bearer <api_key>``.
    """

    def __init__(self, endpoint: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self.embeddings = _RecordingEmbeddings(self)
        self.chat = _RecordingChat(self)
        self.recorded_requests: list[RecordedRequest] = []

    def record(self, kwargs: dict[str, Any], *, kind: str) -> None:
        host = _host_of(self._endpoint)
        base_path = _path_prefix_of(self._endpoint).rstrip("/")
        suffix = "/embeddings" if kind == "embed" else "/chat/completions"
        path = (base_path + suffix) if base_path else suffix
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path=path,
                headers={"Authorization": f"Bearer {self._api_key}"},
                body=dict(kwargs),
            )
        )


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0]


def _path_prefix_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    if "/" not in rest:
        return ""
    return "/" + rest.split("/", 1)[1]


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


class _RaisingEmbeddings:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        del kwargs
        raise self._err


class _RaisingChatCompletions:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        del kwargs
        raise self._err


class _RaisingChat:
    def __init__(self, err: BaseException) -> None:
        self.completions = _RaisingChatCompletions(err)


class _RaisingTransportClient:
    def __init__(self, err: BaseException) -> None:
        self.embeddings = _RaisingEmbeddings(err)
        self.chat = _RaisingChat(err)
        self.recorded_requests: list[RecordedRequest] = []


# ---------------------------------------------------------------------------
# Given — provider configuration (Background)
# ---------------------------------------------------------------------------


@given(parsers.parse('the openai provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = model


# Note: the credential-resolver Given step ('returns the api key "..."')
# is owned by the azure_foundry module to avoid a duplicate registration;
# the phrasing is identical between features so a single binding suffices.
# That step sets ``api_key`` on the shared state dict, which this
# module's _build_provider reads.


# ---------------------------------------------------------------------------
# When — drive embed via the openai plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> OpenAIProvider:
    creds = Credentials(
        api_key=state["api_key"] or "sk-test-openai",  # pragma: allowlist secret
        endpoint=state["endpoint"] or "https://api.openai.com/v1",
        model=state["model"] or "text-embedding-3-large",
        dims=1536,
    )
    transport_slot = state.get("transport")
    if isinstance(transport_slot, dict) and "raise_status" in transport_slot:
        transport: Any = _RaisingTransportClient(
            _UpstreamApiError(transport_slot["raise_status"], headers=transport_slot["headers"])
        )
    else:
        transport = _RecordingTransportClient(creds.endpoint, creds.api_key)
    state["transport"] = transport
    return OpenAIProvider(credentials=creds, transport_client=transport)


@when("the operator embeds a single text via the openai plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — provider-name-bearing typed-error assertions
# ---------------------------------------------------------------------------


@then("the openai plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → error becomes bare ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__}: {err!r}"


@then("the openai plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 401 branch → error becomes
    ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, AuthError), f"expected AuthError; got {type(err).__name__}: {err!r}"


@then("the openai plugin raises a canonical UpstreamError")
def _then_upstream_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 5xx branch → error becomes
    ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, UpstreamError), f"expected UpstreamError; got {type(err).__name__}: {err!r}"
