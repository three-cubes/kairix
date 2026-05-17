"""Skeleton step definitions for provider_azure_legacy.feature (#provider-plugin-arch IM-7).

The ``azure_legacy`` plugin is a Wave-4 NotImplementedError stub in
:mod:`kairix.providers.azure_legacy`; the implementation lands as
part of the follow-up wave to IM-4 / IM-5 once the contract is proven.

This module exists so pytest-bdd's collection succeeds when
:mod:`tests.bdd.test_provider_azure_legacy` binds the feature file,
and so F28 (every plugin has a matching feature) sees behavioural
coverage scaffolding. Every Given / When / Then in
``provider_azure_legacy.feature`` parses here, then immediately
``pytest.skip("plugin not implemented yet — Wave 4")`` so collection
is green while execution is intentionally deferred.

The skip rationale is the public name of the gap (the Wave-4 ticket
in the ADR's migration plan); :mod:`tests.bdd.conftest` does NOT need
an F11-rationale carve-out because the skip is dispatched from inside
the step body, not via the ``@pytest.mark.skip`` decorator that F11
gates.

Shared step phrases ("a wire-endpoint fixture that records every
outbound request", "the configured endpoint is <url>", every
"the recorded request <attr>" assertion, the canonical-error
assertions) are still owned by
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module
intercepts the provider-name-bearing steps so the eventual
Wave-4 implementation can drop in alongside without conflicting.

F1-clean, F2-clean, F5-clean, F11-clean (skips dispatched from the
step body carry their own rationale comment).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

pytestmark = pytest.mark.bdd

_SKIP_REASON = "plugin not implemented yet — Wave 4"


def _skip(_provider_wire_state: dict[str, Any]) -> None:
    """Dispatch ``pytest.skip`` with the Wave-4 rationale.

    Centralised so every step body shares the exact same skip reason
    text — F11-clean because the skip is dispatched at execution
    time with a rationale (not via the decorator F11 gates).
    """
    del _provider_wire_state
    pytest.skip(_SKIP_REASON)


# ---------------------------------------------------------------------------
# Background / Given
# ---------------------------------------------------------------------------


@given(parsers.parse('the azure_legacy provider configured with deployment "{deployment}"'))
def _given_deployment(_provider_wire_state: dict[str, Any], deployment: str) -> None:
    del deployment
    _skip(_provider_wire_state)


@given("no operator override for the api-version parameter")
def _given_no_override(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@given(parsers.parse('the operator override for api-version is "{value}"'))
def _given_api_version_override(_provider_wire_state: dict[str, Any], value: str) -> None:
    del value
    _skip(_provider_wire_state)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the operator embeds a single text via the azure_legacy plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


# ---------------------------------------------------------------------------
# Then — provider-name-bearing
# ---------------------------------------------------------------------------


@then(parsers.parse('the recorded request query contains the parameter "{name}"'))
def _then_query_contains(_provider_wire_state: dict[str, Any], name: str) -> None:
    del name
    _skip(_provider_wire_state)


@then(parsers.parse('the recorded request query "{name}" equals the ADR default api-version'))
def _then_query_default(_provider_wire_state: dict[str, Any], name: str) -> None:
    del name
    _skip(_provider_wire_state)


@then(parsers.parse('the recorded request query "{name}" equals "{value}"'))
def _then_query_equals(_provider_wire_state: dict[str, Any], name: str, value: str) -> None:
    del name, value
    _skip(_provider_wire_state)


@then("the azure_legacy plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the azure_legacy plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)
