"""Step definitions for ``e2e_provider_health.feature``.

Drives the operator-visible ``kairix probe-config`` CLI journey. The
CLI itself is owned by IM-9-RETRY and was *not* in ``origin/develop`` at
this worktree's rebase point — every When-step here calls
``pytest.skip(...)`` with a substantive reason that grep-trails back to
IM-9. Once the CLI lands, the skips can be deleted and the impls
populated by reading the JSON probe schema at
``docs/architecture/probe-config-schema.md``.

The shared Background steps (registry loaded, operator-configured
provider, credential variable) are reused from
``e2e_provider_embed_steps`` so the feature's Background can resolve
before the When-step fires its skip. That keeps the skip located at
the *call site* of the missing CLI surface, not buried in a Background
hook.

Why a runtime ``pytest.skip(...)`` (not ``@pytest.mark.skip``)?
``@pytest.mark.skip`` would skip every binding in this module
unconditionally — even after IM-9 lands. Runtime ``pytest.skip`` keeps
the skip *local to the missing surface*: when IM-9-RETRY ships
``kairix probe-config``, deleting the skip line inside the When-step is
the only edit needed. F11 polices decorator-level skips for rationale;
runtime ``pytest.skip(reason)`` is not in scope of that gate (the
rationale is the string argument).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

# Importing the shared module registers its Background/Given step
# phrases as pytest-bdd definitions process-wide.
from tests.bdd.steps.e2e_provider_embed_steps import drive_embed  # noqa: F401  — registers shared steps

pytestmark = pytest.mark.bdd


_PROBE_CONFIG_SKIP_REASON = (
    "blocked on IM-9-RETRY probe-config CLI; once `kairix probe-config` lands in "
    "develop, replace this skip with the JSON-schema field assertions per "
    "docs/architecture/probe-config-schema.md (SK-7). fix: track IM-9-RETRY "
    "merge; next: rebase this worktree onto develop and remove the skip."
)


# ---------------------------------------------------------------------------
# Given — health-specific scenario setup
# ---------------------------------------------------------------------------


@given(parsers.parse("the configured provider answers warm calls in {ms:d} milliseconds"))
def configured_provider_warm_call_ms(e2e_provider_state: dict[str, Any], ms: int) -> None:
    """Record an expected warm-call latency for the slow-call assertion.

    The probe-config CLI under IM-9-RETRY honours this by issuing a
    warm-call against the provider and surfacing a tuning recommendation
    when the latency exceeds its configured threshold. Until the CLI
    lands, this step is record-only — the When-step skips before the
    recommendation is checked.
    """
    e2e_provider_state["expected_warm_ms"] = ms


@given("the configured provider fails every healthcheck call")
def configured_provider_unhealthy(e2e_provider_state: dict[str, Any]) -> None:
    """Mark the configured provider as degraded for the unhealthy scenario.

    Same record-only semantics as the warm-latency Given above — the
    probe-config CLI is the consumer of this state; until IM-9-RETRY
    ships, the When-step skips before this flag is honoured.
    """
    e2e_provider_state["force_unhealthy"] = True


# ---------------------------------------------------------------------------
# When — the missing CLI surface
# ---------------------------------------------------------------------------


@when(parsers.parse('the operator runs "{command}"'))
def operator_runs_command(e2e_provider_state: dict[str, Any], command: str) -> None:
    """Run the operator-visible CLI command and capture its exit + stdout.

    Today this is a skip — ``kairix probe-config`` is owned by IM-9-RETRY
    and was not in ``origin/develop`` at this worktree's rebase point.
    The skip points at the missing surface so IM-9-RETRY's merge is the
    single trigger for re-enabling these scenarios. Once landed:

        import subprocess
        proc = subprocess.run(command.split(), capture_output=True, text=True)
        e2e_provider_state["exit_code"] = proc.returncode
        e2e_provider_state["stdout"] = proc.stdout

    populates state for the Then-step assertions to read.
    """
    # Record the requested command so a future implementation can route on it.
    e2e_provider_state["requested_command"] = command
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


# ---------------------------------------------------------------------------
# Then — assertions on the probe-config report
#
# These are unreachable until IM-9-RETRY lands because the When-step
# above always raises ``pytest.skip``. They're defined so pytest-bdd
# can match every step at collection time (pytest-bdd 7+ raises
# StepDefinitionNotFoundError at collection if a Then has no impl).
# Each assertion documents the field it would check against the JSON
# probe schema; once the CLI lands, the bodies populate.
# ---------------------------------------------------------------------------


@then(parsers.parse("the command exits with code {code:d}"))
def command_exits_with_code(e2e_provider_state: dict[str, Any], code: int) -> None:
    # sabotage (post-IM-9): mutate probe-config to return exit code 0 on degraded path → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("stdout is valid JSON")
def stdout_is_valid_json(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): make probe-config emit markdown instead of JSON → json.loads fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the JSON report names the configured provider")
def report_names_provider(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the "provider" field from the report → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the JSON report includes a cold_latency_ms entry for the configured provider")
def report_includes_cold_latency(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): omit cold_latency_ms from the report → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the JSON report includes a warm_latency_ms entry for the configured provider")
def report_includes_warm_latency(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): omit warm_latency_ms from the report → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then(parsers.parse('the JSON report includes a "status" field with the value "{value}"'))
def report_status_field(e2e_provider_state: dict[str, Any], value: str) -> None:
    # sabotage (post-IM-9): hardcode status="degraded" → assertion fires when value="healthy".
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the warm_latency_ms entry is less than or equal to the cold_latency_ms entry")
def warm_less_than_cold(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): swap cold/warm in the report → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then(parsers.parse('the JSON report includes a "warnings" array with at least one entry'))
def report_warnings_non_empty(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the recommendation emit → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("one warning names the configured provider as a slow_warm_call source")
def warning_names_slow_warm_provider(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the provider name from the warning → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then(parsers.parse('one warning includes a "recommendation" field with non-empty text'))
def warning_recommendation_non_empty(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): blank the recommendation string → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the JSON report records the failure mode for the configured provider")
def report_records_failure_mode(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the failure-mode field → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then(parsers.parse('the JSON report includes a "kairix_version" field'))
def report_includes_kairix_version(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the version field → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then(parsers.parse('the JSON report includes a "schema_version" field'))
def report_includes_schema_version(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): drop the schema_version field → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)


@then("the schema_version field matches the schema documented at probe-config-schema.md")
def schema_version_matches_doc(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage (post-IM-9): bump schema_version without touching the doc → assertion fires.
    pytest.skip(_PROBE_CONFIG_SKIP_REASON)
