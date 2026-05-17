"""Step definitions for provider_ollama.feature (#provider-plugin-arch IM-11).

Drives :class:`kairix.providers.ollama.OllamaProvider` with a recording
fake ``transport_client`` so the scenarios assert that the configured
endpoint flows through verbatim (no ``/openai/v1`` suffix, no
``/v1/embeddings`` openai-style path), the request body carries the
configured model + prompt, and connection-refused maps to
``ProviderUnreachable``.

Shared step phrases live in :mod:`tests.bdd.steps.provider_wire_common_steps`;
this module owns only the Ollama-specific Background Given, the When
step, and the ``ProviderUnreachable``-with-endpoint Then assertions.

DI-clean: ``transport_client=`` is the documented test seam on
:class:`OllamaProvider`. No monkeypatch, no @patch, no env mutation.

F1-clean, F2-clean, F5-clean.

Sabotage-proofs are noted per step inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.credentials import Credentials
from kairix.providers import ProviderError, ProviderUnreachable
from kairix.providers.ollama import OllamaProvider
from tests.bdd.steps.provider_wire_common_steps import RecordedRequest

pytestmark = pytest.mark.bdd

_PROVIDER = "ollama"


# ---------------------------------------------------------------------------
# Recording transport — exposes ``recorded_requests`` for the shared
# assertion steps to read.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingOllamaTransport:
    """Recording fake mirroring the Ollama-native HTTP surface.

    Unlike the OpenAI / Azure fakes (which mirror the openai-SDK
    ``embeddings.create`` shape), this fake exposes a single
    ``post(path, json)`` method matching the Ollama wire — that's the
    DI seam the plugin actually consumes for Ollama-native API calls.

    No auth header is recorded because Ollama has none. The
    ``recorded_requests`` list is populated with an empty headers dict
    so the shared "no header named X" assertion finds no auth keys.
    """

    endpoint: str

    def __post_init__(self) -> None:
        self.recorded_requests: list[RecordedRequest] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        host = _host_of(self.endpoint)
        self.recorded_requests.append(
            RecordedRequest(
                host=host,
                path=path,
                headers={},  # Ollama: no auth header at all.
                body=dict(json),
            )
        )
        if path.endswith("/embeddings"):
            # Return a stub embedding so embed_batch completes normally.
            return {"embedding": [0.1, 0.2, 0.3]}
        if path.endswith("/chat"):
            return {"message": {"role": "assistant", "content": "hi"}}
        return {}


class _ConnectionRefusedTransport:
    """Transport that always raises ConnectionRefusedError.

    Models the load-bearing Ollama failure mode: the operator's sidecar
    isn't running. Exposes ``recorded_requests`` (empty) so the shared
    assertion helpers don't AttributeError when they ask for it.
    """

    def __init__(self) -> None:
        self.recorded_requests: list[RecordedRequest] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        del path, json
        raise ConnectionRefusedError("Connection refused (sidecar not running)")


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0]


# ---------------------------------------------------------------------------
# Given — provider configuration (Background)
# ---------------------------------------------------------------------------


@given(parsers.parse('the ollama provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    _provider_wire_state["provider_name"] = _PROVIDER
    _provider_wire_state["model"] = model


@given("the wire endpoint refuses the connection")
def _given_refused(_provider_wire_state: dict[str, Any]) -> None:
    """Mark the transport as 'will refuse the connection'.

    The per-provider When step reads this slot and constructs a
    raising transport rather than the recording one. Distinct from
    the shared 429/401/500 status-code slots (which assume an HTTP
    response was received) — connection-refused never gets that far.
    """
    _provider_wire_state["transport"] = {"refuse_connection": True}


# ---------------------------------------------------------------------------
# When — drive embed via the ollama plugin
# ---------------------------------------------------------------------------


def _build_provider(state: dict[str, Any]) -> OllamaProvider:
    creds = Credentials(
        # Ollama is unauthenticated — api_key is intentionally empty.
        api_key="",
        endpoint=state["endpoint"] or "http://localhost:11434",
        model=state["model"] or "nomic-embed-text",
        dims=0,
    )
    transport_slot = state.get("transport")
    transport: Any
    if isinstance(transport_slot, dict) and transport_slot.get("refuse_connection"):
        transport = _ConnectionRefusedTransport()
    else:
        transport = _RecordingOllamaTransport(endpoint=creds.endpoint)
    state["transport"] = transport
    return OllamaProvider(credentials=creds, transport_client=transport)


@when("the operator embeds a single text via the ollama plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    """Build the provider, embed once, capture any typed error.

    The recording transport records the path / host / body so the
    shared assertion steps can read it; the refused transport raises
    so the error-path scenarios can match on ``ProviderUnreachable``.
    """
    provider = _build_provider(_provider_wire_state)
    try:
        provider.embed_batch(["hello"])
    except ProviderError as err:
        _provider_wire_state["raised"] = err


# ---------------------------------------------------------------------------
# Then — provider-name-bearing typed-error assertions
# ---------------------------------------------------------------------------


@then("the ollama plugin raises a canonical ProviderUnreachable error")
def _then_unreachable(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the connection-failure branch in
    ``_map_transport_error`` → bare ``ProviderError`` raised, this
    isinstance check fails. Verified by mutating the early-return.
    """
    err = _provider_wire_state["raised"]
    assert isinstance(err, ProviderUnreachable), (
        f"expected ProviderUnreachable; got {type(err).__name__ if err else 'None'}: {err!r}"
    )


@then("the error message names the configured endpoint")
def _then_message_names_endpoint(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: drop the endpoint interpolation in the
    ProviderUnreachable message → the assertion fails. Verified by
    removing the endpoint= argument from _map_transport_error.
    """
    err = _provider_wire_state["raised"]
    assert err is not None, "expected an error to have been raised"
    endpoint = _provider_wire_state["endpoint"]
    assert endpoint is not None, "scenario did not configure an endpoint"
    assert endpoint in str(err), f"expected configured endpoint {endpoint!r} in error message; got {err!s}"
