"""Unit + contract tests for the ReadinessGate Protocol and EventReadinessGate Adapter.

Tested through public surface: construct, query, mark, query. No private
symbol imports. No @patch.
"""

from __future__ import annotations

import threading

import pytest

from kairix.agents.mcp.readiness import EventReadinessGate, ReadinessGate


@pytest.mark.unit
def test_default_is_not_ready() -> None:
    gate = EventReadinessGate()
    assert gate.is_ready() is False


@pytest.mark.unit
def test_initial_ready_state_respected() -> None:
    """Pre-marked-ready gates exist for stdio transport where there is no warm-up."""
    gate = EventReadinessGate(ready=True)
    assert gate.is_ready() is True


@pytest.mark.unit
def test_mark_ready_flips_state() -> None:
    gate = EventReadinessGate()
    assert gate.is_ready() is False
    gate.mark_ready()
    assert gate.is_ready() is True


@pytest.mark.unit
def test_mark_ready_is_idempotent() -> None:
    gate = EventReadinessGate()
    gate.mark_ready()
    gate.mark_ready()  # no exception, no toggle back
    assert gate.is_ready() is True


@pytest.mark.unit
def test_concurrent_mark_ready_is_safe() -> None:
    """If multiple warm-up paths race to mark ready, the gate stays consistent."""
    gate = EventReadinessGate()
    threads = [threading.Thread(target=gate.mark_ready) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert gate.is_ready() is True


@pytest.mark.contract
def test_event_readiness_gate_satisfies_protocol() -> None:
    gate = EventReadinessGate()
    assert isinstance(gate, ReadinessGate)


@pytest.mark.contract
def test_in_test_fake_satisfies_protocol() -> None:
    """Any small fake with the right surface should structurally satisfy the Protocol."""

    class _FakeGate:
        def is_ready(self) -> bool:
            return True

        def mark_ready(self) -> None:
            """Test stub — satisfies ReadinessGate Protocol; structural check only."""

    assert isinstance(_FakeGate(), ReadinessGate)
