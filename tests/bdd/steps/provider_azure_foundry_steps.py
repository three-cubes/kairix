"""Step definitions for provider_azure_foundry.feature (#provider-plugin-arch IM-7).

Drives :class:`kairix.providers.azure_foundry.AzureFoundryProvider` with
a recording fake ``transport_client`` so the scenarios assert the wire
shape Foundry expects (host, ``/openai/v1`` path prefix, ``api-key``
header, deployment name in the body) and the canonical typed-error
mapping for 4xx responses.

Shared step phrases ("a wire-endpoint fixture that records every
outbound request", "the configured endpoint is <url>", "the wire
endpoint will respond with status <n>", every "the recorded request
<attr>" assertion, "the error message names the configured provider as
<name>") live in :mod:`tests.bdd.steps.provider_wire_common_steps` to
avoid duplicate-step ambiguity across the seven provider step modules.

This module owns only the provider-name-bearing surface:

- Background ``Given the azure_foundry provider configured with deployment <x>``
  — sets ``provider_name`` and ``model`` on the shared state.
- Background ``Given the configured credential resolver returns the api key <k>``
  — sets ``api_key`` on the shared state.
- ``When the operator embeds a single text via the foundry plugin`` —
  builds the recording (or raising) transport and drives
  :meth:`AzureFoundryProvider.embed_batch`.
- ``Then the foundry plugin raises a canonical RateLimited error`` etc
  — provider-name-bearing typed-error assertions.

DI-clean: ``transport_client=`` is the documented test seam on
:class:`AzureFoundryProvider` (see ADR § Provider Protocol contract).
No monkeypatch, no @patch, no env mutation.

F1-clean, F2-clean, F5-clean.

Sabotage-proofs are noted per step inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.credentials import Credentials
from kairix.providers import AuthError, ProviderError, RateLimited
from kairix.providers.azure_foundry import (
    AzureFoundryProvider,
    normalize_foundry_endpoint,
)
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

#: Stable plugin name; matches the Background step phrasing and the
#: ``PROVIDER_NAME`` exported by the plugin.
_PROVIDER = "azure_foundry"


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
    """Recording fake mirroring the openai-SDK ``AzureOpenAI`` surface.

    Each ``embed_batch`` / ``chat`` call lands on
    ``embeddings.create`` / ``chat.completions.create`` here; the
    captured kwargs plus a synthetic :class:`RecordedRequest` carrying
    the normalised Foundry URL and the ``api-key`` header populate the
    ``recorded_requests`` list read by the shared assertion steps.
    """

    def __init__(self, endpoint: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self.embeddings = _RecordingEmbeddings(self)
        self.chat = _RecordingChat(self)
        self.recorded_requests: list[RecordedRequest] = []

    def record(self, kwargs: dict[str, Any], *, kind: str) -> None:
        normalised = normalize_foundry_endpoint(self._endpoint)
        host = _host_of(normalised)
        base_path = _path_prefix_of(normalised)
        suffix = "/embeddings" if kind == "embed" else "/chat/completions"
        path = base_path + suffix
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path=path,
                headers={"api-key": self._api_key},
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
    """Stand-in for an openai-SDK ``APIStatusError``-shaped exception."""

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
    """No outbound recording — the call raises before record() is reached."""

    def __init__(self, err: BaseException) -> None:
        self.embeddings = _RaisingEmbeddings(err)
        self.chat = _RaisingChat(err)
        self.recorded_requests: list[RecordedRequest] = []


# ---------------------------------------------------------------------------
# Given — provider configuration (Background)
# ---------------------------------------------------------------------------


@given(parsers.parse('the azure_foundry provider configured with deployment "{deployment}"'))
def _given_deployment(_provider_wire_state: dict[str, Any], deployment: str) -> None:
    """Set the configured deployment (Foundry's name for ``model=``).

    Multiple occurrences are allowed across Background + Scenario —
    the last one wins so the "deployment name flows through" scenario
    can override the Background default.
    """
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = deployment


@given(parsers.parse('the configured credential resolver returns the api key "{api_key}"'))
def _given_api_key(_provider_wire_state: dict[str, Any], api_key: str) -> None:
    _provider_wire_state["api_key"] = api_key


# ---------------------------------------------------------------------------
# When — drive embed via the foundry plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> AzureFoundryProvider:
    creds = Credentials(
        api_key=state["api_key"] or "foundry-test-key",  # pragma: allowlist secret
        endpoint=state["endpoint"] or "https://example-resource.services.ai.azure.com",
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
    return AzureFoundryProvider(credentials=creds, transport_client=transport)


@when("the operator embeds a single text via the foundry plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — provider-name-bearing typed-error assertions
# ---------------------------------------------------------------------------


@then("the foundry plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → error becomes bare ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__}: {err!r}"


@then("the foundry plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 401/403 branch in ``_map_transport_error``
    → error becomes ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, AuthError), f"expected AuthError; got {type(err).__name__}: {err!r}"
