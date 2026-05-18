"""Step definitions for provider_chat_max_completion_tokens.feature.

Drives :class:`kairix.providers.azure_foundry.AzureFoundryProvider` with a
recording fake ``transport_client`` and asserts the kwarg-name routing
when the configured deployment is a reasoning-class model (gpt-5.x,
o1-x, o3-x) versus a chat-class model (gpt-4o-mini, etc).

Encapsulation: each step produces a scenario-scoped value via pytest-bdd
``target_fixture`` and consumes prior values by parameter name. No
module-level shared ``_state`` dict; no leakage across step modules; no
monkey-patching of kairix internals. ``transport_client=`` is the
documented test seam on :class:`AzureFoundryProvider`.

Behaviour pinned:

- Reasoning-class deployments → wire receives ``max_completion_tokens``;
  ``max_tokens`` absent.
- Chat-class deployments → wire receives ``max_tokens``;
  ``max_completion_tokens`` absent.
- Public kairix surface keeps ``max_tokens=N`` as the kwarg callers pass.

Sabotage-proofs noted per step inline.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.credentials import Credentials
from kairix.providers.azure_foundry import AzureFoundryProvider

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Recording transport — minimal SDK-shaped surface that captures the kwargs
# every ``chat.completions.create(...)`` call carries. No HTTP, no
# monkey-patching, injected through the documented ``transport_client=``
# DI seam.
# ---------------------------------------------------------------------------


@dataclass
class _FakeChatMessage:
    content: str | None


@dataclass
class _FakeChatChoice:
    message: _FakeChatMessage


@dataclass
class _FakeChatResponse:
    choices: list[_FakeChatChoice]


class _RecordingChatCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append(dict(kwargs))
        return _FakeChatResponse(choices=[_FakeChatChoice(message=_FakeChatMessage(content="ok"))])


class _RecordingChat:
    def __init__(self) -> None:
        self.completions = _RecordingChatCompletions()


class _RecordingTransportClient:
    """SDK-shaped fake recording only ``chat.completions.create`` calls."""

    def __init__(self) -> None:
        self.chat = _RecordingChat()


# ---------------------------------------------------------------------------
# Background — recording transport
# ---------------------------------------------------------------------------


@given(
    "a recording transport client that captures every chat.completions.create call",
    target_fixture="recording_transport",
)
def _given_recording_transport() -> _RecordingTransportClient:
    """Yield a recording transport client scoped to this scenario.

    Sabotage-proof: drop this step from the feature and the When-step's
    ``recording_transport`` parameter is unresolved — pytest-bdd
    reports the missing fixture by name, immediately localising the
    missing Background line.
    """
    return _RecordingTransportClient()


@given(
    parsers.parse('the foundry chat provider is configured against deployment "{deployment}"'),
    target_fixture="deployment_name",
)
def _given_deployment_name(deployment: str) -> str:
    """Yield the deployment name scoped to this scenario.

    Unique phrase distinct from
    ``provider_azure_foundry_steps.py``'s ``the azure_foundry provider
    configured with deployment "..."`` to avoid cross-feature step
    shadowing — pytest-bdd resolves step definitions globally by phrase,
    not per feature file.

    Sabotage-proof: change the deployment-name capture group and the
    parametrised cases stop matching — pytest-bdd reports unmatched
    step.
    """
    return deployment


@given(parsers.parse('the configured credential resolver returns the api key "{api_key}"'))
def _given_api_key_noop(api_key: str) -> None:
    """Background-phrase compatibility no-op — the recording transport
    doesn't validate the api key (no HTTP); this step exists so the
    Background reads the same way as the other provider features.
    """
    del api_key  # captured for Gherkin readability; not consumed


# ---------------------------------------------------------------------------
# When — invoke the chat method, capture the recorded call
# ---------------------------------------------------------------------------


@when(
    parsers.parse("the operator invokes the foundry chat method with max_tokens {max_tokens:d}"),
    target_fixture="recorded_chat_call",
)
def _when_invoke_chat(
    recording_transport: _RecordingTransportClient,
    deployment_name: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Build the provider with the recording transport, invoke ``chat``,
    return the kwargs the transport saw.

    Sabotage-proof: change the provider impl to bypass
    ``transport_client`` (e.g. always build a real openai client) and
    the recorded_calls list stays empty — the next Then-step's index
    access raises IndexError.
    """
    creds = Credentials(
        api_key="foundry-test-key",  # pragma: allowlist secret — test-only literal, never reaches a real Azure endpoint
        endpoint="https://example-resource.services.ai.azure.com",
        model=deployment_name,
        dims=1536,
    )
    provider = AzureFoundryProvider(credentials=creds, transport_client=recording_transport)
    provider.chat([{"role": "user", "content": "hi"}], max_tokens=max_tokens)

    calls = recording_transport.chat.completions.calls
    assert len(calls) == 1, f"expected exactly one chat.completions.create call; got {len(calls)}"
    return calls[0]


# ---------------------------------------------------------------------------
# Then — wire-shape assertions on the recorded call kwargs
# ---------------------------------------------------------------------------


@then(parsers.parse("the recorded chat.completions.create call carries max_completion_tokens {value:d}"))
def _then_call_carries_max_completion_tokens(recorded_chat_call: dict[str, Any], value: int) -> None:
    """Sabotage-proof: revert the provider to always send ``max_tokens``
    regardless of model, and this assertion fails for every gpt-5/o1/o3
    scenario.
    """
    assert "max_completion_tokens" in recorded_chat_call, (
        f"expected max_completion_tokens; kwargs = {sorted(recorded_chat_call.keys())}"
    )
    assert recorded_chat_call["max_completion_tokens"] == value


@then(parsers.parse("the recorded chat.completions.create call carries max_tokens {value:d}"))
def _then_call_carries_max_tokens(recorded_chat_call: dict[str, Any], value: int) -> None:
    """Sabotage-proof: change impl to always send ``max_completion_tokens``
    and this scenario (gpt-4o-mini) fails.
    """
    assert "max_tokens" in recorded_chat_call, f"expected max_tokens; kwargs = {sorted(recorded_chat_call.keys())}"
    assert recorded_chat_call["max_tokens"] == value


@then("the recorded chat.completions.create call does not carry max_tokens")
def _then_call_omits_max_tokens(recorded_chat_call: dict[str, Any]) -> None:
    """Reasoning-class path must NOT also send ``max_tokens``.

    Sabotage-proof: send both kwargs from impl, this fails.
    """
    assert "max_tokens" not in recorded_chat_call, (
        f"reasoning-class deployments must not receive max_tokens; kwargs = {sorted(recorded_chat_call.keys())}"
    )


@then("the recorded chat.completions.create call does not carry max_completion_tokens")
def _then_call_omits_max_completion_tokens(recorded_chat_call: dict[str, Any]) -> None:
    """Sabotage-proof mirror: chat-class deployments must not receive
    the reasoning kwarg.
    """
    assert "max_completion_tokens" not in recorded_chat_call, (
        f"chat-class deployments must not receive max_completion_tokens; kwargs = {sorted(recorded_chat_call.keys())}"
    )


# ---------------------------------------------------------------------------
# Then — public-signature stability
# ---------------------------------------------------------------------------


@then(parsers.parse('the foundry chat method\'s public signature still accepts the kwarg "{name}"'))
def _then_public_signature_has_kwarg(name: str) -> None:
    """Pin the public kairix surface: ``provider.chat(messages, *,
    max_tokens=N)``. The reasoning-model translation is INTERNAL —
    callers always pass ``max_tokens=`` regardless of underlying model.

    Sabotage-proof: rename the kwarg and this fails.
    """
    sig = inspect.signature(AzureFoundryProvider.chat)
    assert name in sig.parameters, (
        f"AzureFoundryProvider.chat signature missing public kwarg {name!r}; params = {sorted(sig.parameters.keys())}"
    )


@then(parsers.parse('the foundry chat method\'s public signature does not accept the kwarg "{name}"'))
def _then_public_signature_omits_kwarg(name: str) -> None:
    """Sabotage-proof: add ``max_completion_tokens`` as a public kwarg
    by accident and this fails.
    """
    sig = inspect.signature(AzureFoundryProvider.chat)
    assert name not in sig.parameters, (
        f"AzureFoundryProvider.chat unexpectedly exposes public kwarg {name!r}; "
        f"params = {sorted(sig.parameters.keys())}"
    )
