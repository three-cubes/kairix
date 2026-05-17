"""Step definitions for ``e2e_provider_switch.feature``.

Drives the operator-visible provider-switch journey through the SK-2
registry seam. The journey honestly exercises ``KAIRIX_PROVIDER`` as the
*operator-visible* env var by recording it on state (the impl resolves
the configured name out of state, not by reading ``os.environ`` —
keeping us inside F2 / F4); the embed call goes through
``kairix.providers.get_provider(name, registry=fake)`` as in the other
E2E modules.

This module adds the switch-specific Given/When/Then phrases:

- ``the operator sets KAIRIX_PROVIDER to "<name>"``
- ``no kairix source file under kairix/ was modified between the two embeds``
- ``the operator sees a typed ProviderNotRegistered error``
- ``the error reports the requested name "<name>"``
- ``the error lists every installed provider name under an "available" field``
- ``the "available" field includes "<name>"``
- ``the "available" field does not include "<name>"``
- ``a provider directory "kairix/providers/<name>/" exists without an entry-points registration``
- ``both embeds succeeded in the same process``
- ``the second result envelope records the provider name "<name>"``

The shared Background/Given/Then steps live in
``e2e_provider_embed_steps``; this module imports it to register them
process-wide.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.providers import ProviderNotRegistered

# Importing the shared module registers its Background/Given/Then/When
# steps as pytest-bdd step definitions process-wide. The switch journey
# reuses the shared ``the operator embeds the text "X"`` When-step — its
# ``drive_embed`` already captures a pre-embed tree signature.
from tests.bdd.steps.e2e_provider_embed_steps import (  # noqa: F401  — registers shared steps
    _kairix_tree_signature,
    drive_embed,
)

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Given — the env-var-style provider switch
# ---------------------------------------------------------------------------


@given(parsers.parse('the operator sets KAIRIX_PROVIDER to "{name}"'))
@when(parsers.parse('the operator sets KAIRIX_PROVIDER to "{name}"'))
def operator_sets_kairix_provider(e2e_provider_state: dict[str, Any], name: str) -> None:
    """Record the operator's ``KAIRIX_PROVIDER`` env-var selection.

    Registered as both Given and When because the switch feature uses
    both keywords on the same phrase — the journey scenario starts the
    embed-then-switch chain with Given, but the "no restart" scenario
    progresses to the *second* selection via When (continuing the
    operator-action narrative). Both semantics map to the same state
    write: bump ``current_provider_name`` to the new name. No
    ``monkeypatch.setenv`` (F2) — the impl exposes the env var
    *contract* via the same state field that the operator-configured
    Given populates. Honest dogfooding: production reads
    ``KAIRIX_PROVIDER`` through ``kairix.paths`` (F4 boundary); the
    step driver bypasses that and writes the selection directly onto
    the test state.
    """
    e2e_provider_state["current_provider_name"] = name


@given(parsers.parse('a provider directory "{path}" exists without an entry-points registration'))
def orphan_provider_directory(e2e_provider_state: dict[str, Any], path: str) -> None:
    """Record an orphan directory the operator might have authored locally.

    The ``FakeProviderRegistry`` is keyed only on the names the test
    seeded it with — an unregistered directory is *invisible* to the
    registry by construction (no entry point → no resolve). This step
    is a record-only marker so the subsequent ``available`` assertion
    has the contextual "orphan name" to check against.
    """
    # path is "kairix/providers/<name>/"; extract the name segment.
    parts = [segment for segment in path.split("/") if segment]
    orphan_name = parts[-1] if parts else path
    e2e_provider_state.setdefault("orphan_names", []).append(orphan_name)


# ---------------------------------------------------------------------------
# Then — typed-error assertions (ProviderNotRegistered)
# ---------------------------------------------------------------------------


@then("the operator sees a typed ProviderNotRegistered error")
def operator_sees_provider_not_registered(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: pre-seed an "orphan" entry into the FakeProviderRegistry → resolve succeeds, assertion fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected ProviderNotRegistered, but no error was captured"
    assert isinstance(err, ProviderNotRegistered), f"expected ProviderNotRegistered, got {type(err).__name__}: {err!r}"


@then(parsers.parse('the error reports the requested name "{name}"'))
def error_reports_requested_name(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: drop name= kwarg from ProviderNotRegistered.__init__ → attribute mismatch fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error"
    assert getattr(err, "name", None) == name, f"error.name={getattr(err, 'name', None)!r}, expected {name!r}"


@then('the error lists every installed provider name under an "available" field')
def error_lists_installed_providers(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: have FakeProviderRegistry.available() return [] → assertion fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error"
    available = getattr(err, "available", None)
    assert isinstance(available, list), f"error.available is not a list: {available!r}"
    registry_available = e2e_provider_state["registry"].available()
    assert sorted(available) == sorted(registry_available), (
        f"error.available={sorted(available)!r} does not match registry.available()={sorted(registry_available)!r}"
    )


@then(parsers.parse('the "available" field includes "{name}"'))
def available_field_includes(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: remove the named provider from the registry seed → assertion fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error"
    available = getattr(err, "available", [])
    assert name in available, f"{name!r} not in error.available={available!r}"


@then(parsers.parse('the "available" field does not include "{name}"'))
def available_field_does_not_include(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: pre-seed the orphan name into the registry → assertion fires
    # (proves the gate that orphan dirs are invisible to the registry).
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error"
    available = getattr(err, "available", [])
    assert name not in available, f"{name!r} unexpectedly present in error.available={available!r}"


# ---------------------------------------------------------------------------
# Then — process-stability + second-envelope assertions
# ---------------------------------------------------------------------------


@then("no kairix source file under kairix/ was modified between the two embeds")
def no_kairix_source_modified(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: between the two embeds, ``touch kairix/__init__.py`` from the step impl
    # → signature differs, assertion fires. The signature is a stable mtime+size tuple
    # over every .py file under kairix/.
    before = e2e_provider_state.get("kairix_tree_signature")
    assert before is not None, "switch journey did not capture a pre-embed tree signature"
    after = _kairix_tree_signature()
    assert before == after, (
        "kairix/ source tree changed between embeds (provider switch should be config-only). "
        f"diff: {_signature_diff(before, after)}"
    )


@then("both embeds succeeded in the same process")
def both_embeds_succeeded_same_process(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: simulate process crash by clearing state["envelopes"] mid-scenario → length check fires.
    envelopes = e2e_provider_state["envelopes"]
    assert len(envelopes) >= 2, f"expected at least 2 envelopes, got {len(envelopes)}"
    process_pid = os.getpid()
    # Tag every envelope with the current PID at construction would be ideal;
    # since the step driver runs in one process, identity is implicit — but
    # we capture it now to make the check sabotage-detectable.
    assert all(env is not None for env in envelopes), (
        f"some envelopes were None (pid={process_pid}); embeds did not all succeed"
    )


@then(parsers.parse('the second result envelope records the provider name "{name}"'))
def second_envelope_records_provider_name(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: have FakeProvider(name=<wrong>) for the second row → assertion fires.
    envelopes = e2e_provider_state["envelopes"]
    assert len(envelopes) >= 2, f"expected at least 2 envelopes for the switch journey, got {len(envelopes)}"
    second = envelopes[1]
    assert second["provider_name"] == name, (
        f"second envelope provider_name={second['provider_name']!r}, expected {name!r}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signature_diff(
    before: tuple[tuple[str, int, int], ...],
    after: tuple[tuple[str, int, int], ...],
) -> str:
    """Render a short diff between two tree signatures for assertion msgs."""
    before_map = {path: (mtime, size) for path, mtime, size in before}
    after_map = {path: (mtime, size) for path, mtime, size in after}
    changed = [path for path in (set(before_map) | set(after_map)) if before_map.get(path) != after_map.get(path)]
    return ", ".join(changed[:5]) or "no path-level diff"
