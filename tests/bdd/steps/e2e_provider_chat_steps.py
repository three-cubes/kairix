"""Step definitions for ``e2e_provider_chat.feature``.

Drives the operator-visible chat journey through the SK-2 registry seam.
The shared Given/Then steps (``the kairix provider registry is loaded…``,
``the operator has configured provider…``, ``the credential variable…``,
``the result envelope records the provider name…``) live in
``e2e_provider_embed_steps`` so the step phrase is defined exactly once.

This module adds the chat-specific When/Then phrases:

- ``the operator sends the chat message "X"`` (incl. ``with max_tokens N``)
- ``the response text is a non-empty string``
- ``the result envelope records a stage_latency_ms entry for "stage"``
- ``the stage_latency_ms entry for "stage" is a non-negative number``

The same envelope plumbing model is used (one envelope per
``state["envelopes"]`` append; latest aliased to ``state["envelope"]``)
so the shared ``the result envelope records the provider name…``
assertion from the embed module works without surgery.

Sabotage proof per scenario is documented inline.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pytest_bdd import parsers, then, when

from kairix.providers import get_provider

# Importing the shared module registers its Background/Given/Then
# steps as pytest-bdd step definitions process-wide. We also reuse the
# ``skip_if_unimplemented`` helper for the chat journey's skip path.
from tests.bdd.steps.e2e_provider_embed_steps import skip_if_unimplemented

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# When — chat actions
# ---------------------------------------------------------------------------


def _drive_chat(state: dict[str, Any], message: str, *, max_tokens: int = 800) -> None:
    """Resolve the configured provider and call ``chat([{role,content}])``.

    Appends a chat envelope to ``state["envelopes"]`` with provider_name,
    response_text, and ``stage_latency_ms.http_roundtrip``. Splitting
    this out keeps cognitive complexity below the F16 ceiling.
    """
    registry = state["registry"]
    name = state["current_provider_name"]
    provider = get_provider(name, registry=registry)
    skip_if_unimplemented(provider, "chat")
    messages = [{"role": "user", "content": message}]
    start = time.perf_counter()
    response = provider.chat(messages, max_tokens=max_tokens)
    latency_ms = (time.perf_counter() - start) * 1000.0
    envelope = {
        "provider_name": provider.name,
        "response_text": response,
        "stage_latency_ms": {"http_roundtrip": latency_ms},
    }
    state["response_text"] = response
    state["envelope"] = envelope
    state["envelopes"].append(envelope)


@when(parsers.parse('the operator sends the chat message "{message}"'))
def operator_sends_chat(e2e_provider_state: dict[str, Any], message: str) -> None:
    _drive_chat(e2e_provider_state, message)


@when(parsers.parse('the operator sends the chat message "{message}" with max_tokens {max_tokens:d}'))
def operator_sends_chat_with_max_tokens(
    e2e_provider_state: dict[str, Any],
    message: str,
    max_tokens: int,
) -> None:
    _drive_chat(e2e_provider_state, message, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Then — chat-specific assertions
# ---------------------------------------------------------------------------


@then("the response text is a non-empty string")
def response_text_non_empty(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: configure FakeProvider(chat_reply="") for the row → assertion fires.
    envelope = e2e_provider_state["envelope"]
    assert envelope is not None, "chat did not populate envelope"
    response = envelope.get("response_text")
    assert isinstance(response, str), f"response_text is not str: {type(response).__name__}"
    assert len(response) > 0, f"response_text is empty (envelope={envelope!r})"


@then(parsers.parse('the result envelope records a stage_latency_ms entry for "{stage}"'))
def envelope_records_stage_latency(e2e_provider_state: dict[str, Any], stage: str) -> None:
    # sabotage: drop the stage_latency_ms key from the envelope build → assertion fires.
    envelope = e2e_provider_state["envelope"]
    assert envelope is not None, "chat did not populate envelope"
    stage_map = envelope.get("stage_latency_ms")
    assert isinstance(stage_map, dict), f"stage_latency_ms is not a dict: {stage_map!r}"
    assert stage in stage_map, f"stage_latency_ms missing entry for {stage!r}; present keys: {sorted(stage_map)}"


@then(parsers.parse('the stage_latency_ms entry for "{stage}" is a non-negative number'))
def stage_latency_non_negative(e2e_provider_state: dict[str, Any], stage: str) -> None:
    # sabotage: set the http_roundtrip latency to -1.0 in the chat envelope build → assertion fires.
    envelope = e2e_provider_state["envelope"]
    assert envelope is not None, "chat did not populate envelope"
    stage_map = envelope.get("stage_latency_ms", {})
    value = stage_map.get(stage)
    assert isinstance(value, (int, float)), f"stage_latency_ms[{stage!r}] is not a number: {value!r}"
    assert value >= 0, f"stage_latency_ms[{stage!r}]={value} is negative"
