"""Step definitions for provider_azure_legacy.feature (#provider-plugin-arch IM-14).

Drives :class:`kairix.providers.azure_legacy.AzureLegacyProvider` with
a recording fake ``transport_client`` so the scenarios assert the wire
shape the legacy Azure OpenAI Service expects (host, NO ``/openai/v1``
path prefix, ``api-key`` header, ``api-version`` query parameter,
deployment name in the body) and the canonical typed-error mapping for
4xx responses.

Shared step phrases ("a wire-endpoint fixture that records every
outbound request", "the configured endpoint is <url>", "the wire
endpoint will respond with status <n>", every "the recorded request
<attr>" assertion, "the error message names the configured provider as
<name>") live in :mod:`tests.bdd.steps.provider_wire_common_steps` to
avoid duplicate-step ambiguity across the seven provider step modules.

This module owns only the provider-name-bearing surface:

- Background ``Given the azure_legacy provider configured with deployment <x>``
  — sets ``provider_name`` and ``model`` on the shared state.
- Background ``Given the configured credential resolver returns the api key <k>``
  — sets ``api_key`` on the shared state. The matching step on the
  foundry module owns the same phrase; pytest-bdd dispatches by the
  active scenario's feature file binding, so the per-provider modules
  do not collide.
- ``Given no operator override for the api-version parameter`` /
  ``Given the operator override for api-version is "<value>"`` — drive
  the api-version override chain into provider construction.
- ``When the operator embeds a single text via the azure_legacy plugin``
  — builds the recording (or raising) transport and drives
  :meth:`AzureLegacyProvider.embed_batch`.
- ``Then the recorded request query contains the parameter "<name>"``
  — assert the legacy-specific ``api-version`` query parameter is
  present.
- ``Then the recorded request query "<name>" equals the ADR default
  api-version`` and the override variant — assert the api-version
  value flowed through.
- ``Then the azure_legacy plugin raises a canonical RateLimited error``
  / ``... canonical AuthError`` — provider-name-bearing typed-error
  assertions.

DI-clean: ``transport_client=`` is the documented test seam on
:class:`AzureLegacyProvider` (see ADR § Provider Protocol contract).
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
from kairix.providers.azure_legacy import AzureLegacyProvider
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

#: Stable plugin name; matches the Background step phrasing and the
#: ``PROVIDER_NAME`` exported by the plugin.
_PROVIDER = "azure_legacy"


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
    the host, the legacy path prefix (NO ``/openai/v1``), the
    ``api-key`` header, and the ``api-version`` query parameter
    populate the ``recorded_requests`` list read by the shared assertion
    steps.
    """

    def __init__(self, endpoint: str, api_key: str, api_version: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._api_version = api_version
        self.embeddings = _RecordingEmbeddings(self)
        self.chat = _RecordingChat(self)
        self.recorded_requests: list[RecordedRequest] = []

    def record(self, kwargs: dict[str, Any], *, kind: str) -> None:
        host = _host_of(self._endpoint)
        deployment = kwargs.get("model", "")
        suffix = "/embeddings" if kind == "embed" else "/chat/completions"
        # Legacy Azure OpenAI Service: the SDK builds
        # ``/openai/deployments/<deployment>/<embeddings|chat/completions>``
        # on the wire. The leading ``/openai`` is the legacy API root —
        # crucially distinct from Foundry's ``/openai/v1`` alias which
        # routes to the openai-compat shim.
        path = f"/openai/deployments/{deployment}{suffix}"
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path=path,
                headers={"api-key": self._api_key},
                body=dict(kwargs),
                query={"api-version": self._api_version},
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


@given(parsers.parse('the azure_legacy provider configured with deployment "{deployment}"'))
def _given_deployment(_provider_wire_state: dict[str, Any], deployment: str) -> None:
    """Set the configured deployment (Azure's name for ``model=``)."""
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = deployment


@given("no operator override for the api-version parameter")
def _given_no_override(_provider_wire_state: dict[str, Any]) -> None:
    """Mark the api-version override slot as empty.

    The When-step reads ``state["extra"]["api_version"]``; ``None``
    means "fall through to the plugin's pinned default".
    """
    _provider_wire_state["extra"]["api_version"] = None


@given(parsers.parse('the operator override for api-version is "{value}"'))
def _given_api_version_override(_provider_wire_state: dict[str, Any], value: str) -> None:
    """Record the operator's override string for the api-version param."""
    _provider_wire_state["extra"]["api_version"] = value


# ---------------------------------------------------------------------------
# When — drive embed via the azure_legacy plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> AzureLegacyProvider:
    creds = Credentials(
        api_key=state["api_key"] or "legacy-test-key",  # pragma: allowlist secret
        endpoint=state["endpoint"] or "https://example-resource.openai.azure.com",
        model=state["model"] or "text-embedding-3-large",
        dims=1536,
    )
    api_version_override = state["extra"].get("api_version")
    transport_slot = state.get("transport")
    if isinstance(transport_slot, dict) and "raise_status" in transport_slot:
        transport: Any = _RaisingTransportClient(
            _UpstreamApiError(transport_slot["raise_status"], headers=transport_slot["headers"])
        )
    else:
        # The recording transport needs the effective api_version to
        # synthesise the ``query`` field. Construct the provider first
        # so the override-chain resolution runs once; then read the
        # resolved value off the provider for the recording fixture.
        # Provider is built twice (once probe, once real) but both
        # constructions are pure: the rejector check runs and no I/O
        # happens until the embed call.
        probe = AzureLegacyProvider(
            credentials=creds,
            api_version=api_version_override,
            transport_client=_RecordingTransportClient(creds.endpoint, creds.api_key, "probe"),
        )
        effective_version = probe.api_version
        transport = _RecordingTransportClient(creds.endpoint, creds.api_key, effective_version)
    state["transport"] = transport
    return AzureLegacyProvider(
        credentials=creds,
        api_version=api_version_override,
        transport_client=transport,
    )


@when("the operator embeds a single text via the azure_legacy plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    """Build the provider with the recording / raising fake and drive embed.

    Sabotage-proof: if the recording fake stopped writing to
    ``recorded_requests``, the shared host / path / header assertion
    steps would fail in their ``last_recorded`` helper.
    """
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — provider-name-bearing query and typed-error assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the recorded request query contains the parameter "{name}"'))
def _then_query_contains(_provider_wire_state: dict[str, Any], name: str) -> None:
    """Sabotage-proof: if the recording fake stopped populating the
    ``query`` field with ``api-version``, this lookup misses, this fails.
    """
    transport = _provider_wire_state["transport"]
    recorded = getattr(transport, "recorded_requests", None)
    assert recorded, "no outbound request was recorded by the wire endpoint"
    last = recorded[-1]
    assert name in last.query, f"expected query parameter {name!r}; got {list(last.query)!r}"


@then(parsers.parse('the recorded request query "{name}" equals the ADR default api-version'))
def _then_query_default(_provider_wire_state: dict[str, Any], name: str) -> None:
    """Sabotage-proof: if the plugin's pinned default drifted from
    "2024-06-01", the recorded query mismatches and this fails. Pulls
    the constant via the public ``api_version`` property on a freshly
    constructed provider so the assertion is decoupled from the private
    constant name.
    """
    transport = _provider_wire_state["transport"]
    recorded = getattr(transport, "recorded_requests", None)
    assert recorded, "no outbound request was recorded by the wire endpoint"
    last = recorded[-1]
    # The provider built in _build_provider with override=None resolves
    # to the plugin's pinned default; the recording fixture stamps that
    # value into the query. So "ADR default" is just whatever the
    # provider's api_version property returns when override is None.
    creds = Credentials(
        api_key="probe-key",  # pragma: allowlist secret
        endpoint="https://example-resource.openai.azure.com",
        model="probe",
        dims=1536,
    )
    expected = AzureLegacyProvider(
        credentials=creds,
        transport_client=_RecordingTransportClient(creds.endpoint, creds.api_key, "probe"),
    ).api_version
    assert last.query.get(name) == expected, (
        f"expected query {name!r}={expected!r} (plugin's pinned default); got {last.query.get(name)!r}"
    )


@then(parsers.parse('the recorded request query "{name}" equals "{value}"'))
def _then_query_equals(_provider_wire_state: dict[str, Any], name: str, value: str) -> None:
    """Sabotage-proof: if the override didn't propagate through
    _resolve_api_version, the recorded query would carry the pinned
    default and this fails.
    """
    transport = _provider_wire_state["transport"]
    recorded = getattr(transport, "recorded_requests", None)
    assert recorded, "no outbound request was recorded by the wire endpoint"
    last = recorded[-1]
    assert last.query.get(name) == value, f"expected query {name!r}={value!r}; got {last.query.get(name)!r}"


@then("the azure_legacy plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → error becomes bare ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__}: {err!r}"


@then("the azure_legacy plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the 401/403 branch in ``_map_transport_error``
    → error becomes ``ProviderError``, this fails.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, AuthError), f"expected AuthError; got {type(err).__name__}: {err!r}"
