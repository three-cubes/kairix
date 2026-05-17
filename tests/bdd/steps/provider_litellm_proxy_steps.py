"""Skeleton step definitions for provider_litellm_proxy.feature (#provider-plugin-arch IM-7).

The ``litellm_proxy`` plugin is a Wave-4 NotImplementedError stub in
:mod:`kairix.providers.litellm_proxy`; the implementation lands once
the contract proved by ``azure_foundry`` and ``openai`` is stable.

Every step parses here and dispatches ``pytest.skip("plugin not
implemented yet — Wave 4")`` so pytest-bdd's collection is green and
F28 (every plugin has a matching feature) sees behavioural coverage
scaffolding.

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
the litellm_proxy-specific virtual-key Background and the
``litellm_proxy`` plugin-name typed-error assertions.

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


@given(parsers.parse('the litellm_proxy provider configured with model "{model}"'))
def _given_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    del model
    _skip(_provider_wire_state)


@given(parsers.parse('the configured credential resolver returns the virtual key "{virtual_key}"'))
def _given_virtual_key(_provider_wire_state: dict[str, Any], virtual_key: str) -> None:
    del virtual_key
    _skip(_provider_wire_state)


@when("the operator embeds a single text via the litellm_proxy plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the litellm_proxy plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the litellm_proxy plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)
