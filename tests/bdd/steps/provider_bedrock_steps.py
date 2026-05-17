"""Skeleton step definitions for provider_bedrock.feature (#provider-plugin-arch IM-7).

The ``bedrock`` plugin is a Wave-4 NotImplementedError stub in
:mod:`kairix.providers.bedrock`; the implementation lands once the
contract proved by ``azure_foundry`` and ``openai`` is stable.

Every step parses here and dispatches ``pytest.skip("plugin not
implemented yet — Wave 4")`` so pytest-bdd's collection is green and
F28 (every plugin has a matching feature) sees behavioural coverage
scaffolding.

Shared step phrases live in
:mod:`tests.bdd.steps.provider_wire_common_steps`; this module owns
only the bedrock-specific surface (AWS credential resolver, region
config key, AccessDenied / Throttling body shape, ``bedrock`` plugin-
name assertions).

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


# ---------------------------------------------------------------------------
# Background / Given — bedrock-specific
# ---------------------------------------------------------------------------


@given(parsers.parse('the bedrock provider configured with model id "{model_id}"'))
def _given_model_id(_provider_wire_state: dict[str, Any], model_id: str) -> None:
    del model_id
    _skip(_provider_wire_state)


@given(parsers.parse('the configured credential resolver returns AWS access key, secret, and region "{region}"'))
def _given_aws_creds(_provider_wire_state: dict[str, Any], region: str) -> None:
    del region
    _skip(_provider_wire_state)


@given(parsers.parse('the bedrock plugin is configured with region "{region}" via the region config key'))
def _given_region_override(_provider_wire_state: dict[str, Any], region: str) -> None:
    del region
    _skip(_provider_wire_state)


@given("the wire endpoint will respond with status 403 and a Bedrock AccessDeniedException body")
def _given_access_denied(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@given("the wire endpoint will respond with status 429 and a Bedrock ThrottlingException body")
def _given_throttling(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the operator embeds a single text via the bedrock plugin")
def _when_embed(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


# ---------------------------------------------------------------------------
# Then — bedrock-specific (SigV4 header semantics)
# ---------------------------------------------------------------------------


@then(parsers.parse('the recorded request header "{name}" begins with "{prefix}"'))
def _then_header_begins(_provider_wire_state: dict[str, Any], name: str, prefix: str) -> None:
    del name, prefix
    _skip(_provider_wire_state)


@then(parsers.parse('the recorded request header "{name}" contains "{needle}"'))
def _then_header_contains(_provider_wire_state: dict[str, Any], name: str, needle: str) -> None:
    del name, needle
    _skip(_provider_wire_state)


@then(parsers.parse('the recorded request path contains "{needle}"'))
def _then_path_contains(_provider_wire_state: dict[str, Any], needle: str) -> None:
    del needle
    _skip(_provider_wire_state)


@then("the bedrock plugin raises a canonical AuthError")
def _then_auth_error(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)


@then("the bedrock plugin raises a canonical RateLimited error")
def _then_rate_limited(_provider_wire_state: dict[str, Any]) -> None:
    _skip(_provider_wire_state)
