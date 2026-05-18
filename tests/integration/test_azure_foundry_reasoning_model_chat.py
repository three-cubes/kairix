"""Integration: AzureFoundryProvider routes max_tokens correctly for reasoning-class models.

End-to-end through the provider's public chat method with a recording
fake injected via the documented ``transport_client=`` DI seam. No
monkey-patching, no @patch, no attribute reassignment on kairix
internals (F1-clean).

Sibling layer to the BDD scenarios in
``tests/bdd/features/provider_chat_max_completion_tokens.feature`` —
the BDD layer asserts operator-language behaviour; this integration
test asserts the same behaviour through the Python API the rest of
kairix consumes, with parametrisation over the full reasoning-model
prefix matrix so a model that newly joins the catalogue is one table
entry away from coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers.azure_foundry import AzureFoundryProvider

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording transport — minimal SDK-shaped surface
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
    def __init__(self) -> None:
        self.chat = _RecordingChat()


def _build_provider(model: str) -> tuple[AzureFoundryProvider, _RecordingTransportClient]:
    transport = _RecordingTransportClient()
    creds = Credentials(
        # Test-only literal — never reaches a real Azure endpoint
        # (transport is the in-memory fake above).
        api_key="foundry-test-key",  # pragma: allowlist secret
        endpoint="https://example-resource.services.ai.azure.com",
        model=model,
        dims=1536,
    )
    provider = AzureFoundryProvider(credentials=creds, transport_client=transport)
    return provider, transport


# ---------------------------------------------------------------------------
# Reasoning-class models — max_completion_tokens on the wire
# ---------------------------------------------------------------------------

# Inventory of deployment-name prefixes that require ``max_completion_tokens``
# on the wire. Adding a new reasoning model means adding one entry here +
# one to the implementation's prefix list.
REASONING_MODELS = [
    "gpt-5",
    "gpt-5.4-mini",
    "gpt-5-turbo",
    "o1-mini",
    "o1-preview",
    "o3-mini",
]

# Chat-class models that still use the legacy ``max_tokens`` parameter.
CHAT_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]


@pytest.mark.parametrize("model", REASONING_MODELS)
def test_reasoning_model_receives_max_completion_tokens(model: str) -> None:
    """Reasoning-class deployment → ``max_completion_tokens`` on wire.

    Sabotage-proof: force the impl's prefix list to be empty and every
    parametrised case fails because the wire receives ``max_tokens``.
    """
    provider, transport = _build_provider(model)

    provider.chat([{"role": "user", "content": "hi"}], max_tokens=321)

    call = transport.chat.completions.calls[0]
    assert "max_completion_tokens" in call, (
        f"model={model!r}: expected max_completion_tokens on wire; got kwargs: {sorted(call.keys())}"
    )
    assert call["max_completion_tokens"] == 321
    assert "max_tokens" not in call, (
        f"model={model!r}: reasoning models must not also send max_tokens; "
        f"Azure rejects requests with both. Got kwargs: {sorted(call.keys())}"
    )


@pytest.mark.parametrize("model", CHAT_MODELS)
def test_chat_model_receives_max_tokens(model: str) -> None:
    """Chat-class deployment → ``max_tokens`` on wire (legacy parameter).

    Sabotage-proof: force the impl to always send
    ``max_completion_tokens`` and every chat-class case fails.
    """
    provider, transport = _build_provider(model)

    provider.chat([{"role": "user", "content": "hi"}], max_tokens=128)

    call = transport.chat.completions.calls[0]
    assert "max_tokens" in call, f"model={model!r}: expected max_tokens on wire; got kwargs: {sorted(call.keys())}"
    assert call["max_tokens"] == 128
    assert "max_completion_tokens" not in call, (
        f"model={model!r}: chat-class deployments must not receive "
        f"max_completion_tokens. Got kwargs: {sorted(call.keys())}"
    )


# ---------------------------------------------------------------------------
# Model + value matrix — kwarg value flows through both routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_kwarg"),
    [
        ("gpt-5.4-mini", "max_completion_tokens"),
        ("o1-mini", "max_completion_tokens"),
        ("gpt-4o-mini", "max_tokens"),
        ("gpt-4o", "max_tokens"),
    ],
)
def test_max_tokens_value_flows_to_correct_kwarg(model: str, expected_kwarg: str) -> None:
    """The numeric value of the caller's ``max_tokens=`` lands under the
    right kwarg on the wire, never silently dropped.

    Sabotage-proof: change the impl to hardcode the value (e.g. always
    256) and the equality assertion fails.
    """
    provider, transport = _build_provider(model)

    provider.chat([{"role": "user", "content": "hi"}], max_tokens=9999)

    call = transport.chat.completions.calls[0]
    assert call[expected_kwarg] == 9999, f"model={model!r}: expected {expected_kwarg}=9999 on wire; got call={call!r}"
