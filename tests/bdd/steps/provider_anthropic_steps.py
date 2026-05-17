"""Skeleton step definitions for provider_anthropic.feature (#provider-plugin-arch IM-7).

The ``anthropic`` plugin is a Wave-4 NotImplementedError stub in
:mod:`kairix.providers.anthropic`; the implementation lands once the
contract proved by ``azure_foundry`` and ``openai`` is stable.

Every step parses here and dispatches ``pytest.skip("plugin not
implemented yet — Wave 4")`` so pytest-bdd's collection is green and
F28 (every plugin has a matching feature) sees behavioural coverage
scaffolding.

Anthropic is the special case in the provider matrix: chat-only, no
embed surface. The feature file pins this via the
``EmbedNotSupported`` typed error; that assertion lands here as a
skipped step until Wave 4 ships the implementation.

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
the anthropic-specific Background and the ``anthropic`` plugin-name
assertions.

F1-clean, F2-clean, F5-clean, F11-clean (skips dispatched from the
step body carry their own rationale).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

pytestmark = pytest.mark.bdd

_SKIP_REASON = "plugin not implemented yet — Wave 4"


def _skip(_provider_wire_state: dict[str, Any]) -> None:
    del _provider_wire_state
    pytest.skip(_SKIP_REASON)


@given(parsers.parse('the anthropic provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    del model
    _skip(_provider_wire_state)


@when("the operator embeds a single text via the anthropic plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@when("the operator runs a single chat completion via the anthropic plugin")
def _when_chat(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the anthropic plugin raises a canonical EmbedNotSupported error")
def _then_embed_not_supported(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the anthropic plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the anthropic plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)
