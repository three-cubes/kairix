"""Skeleton step definitions for provider_ollama.feature (#provider-plugin-arch IM-7).

The ``ollama`` plugin is a Wave-4 NotImplementedError stub in
:mod:`kairix.providers.ollama`; the implementation lands once the
contract proved by ``azure_foundry`` and ``openai`` is stable.

Every step parses here and dispatches ``pytest.skip("plugin not
implemented yet — Wave 4")`` so pytest-bdd's collection is green and
F28 (every plugin has a matching feature) sees behavioural coverage
scaffolding.

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
only the ollama-specific surface (no-auth local-host wiring,
connection-refused error path, ``ollama`` plugin-name assertions).

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


@given(parsers.parse('the ollama provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    del model
    _skip(_provider_wire_state)


@given("the wire endpoint refuses the connection")
def _given_refused(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@when("the operator embeds a single text via the ollama plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the ollama plugin raises a canonical ProviderUnreachable error")
def _then_unreachable(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the error message names the configured endpoint")
def _then_message_names_endpoint(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)
