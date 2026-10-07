"""F68 failure-mode contract for the ``ReadinessGate`` Protocol.

The gate's failure surface is the cold-start window: until warm-up
calls ``mark_ready()``, ``is_ready()`` is False and every tool call must
get the structured ``KAIRIX_COLD_START`` envelope (the "request before
initialization complete" incident) instead of a half-warm answer.

F43 parity: ONE body per method runs over the real
``EventReadinessGate`` and the canonical ``FakeReadinessGate`` — the
observable is the envelope :func:`require_ready` hands the tool layer.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.agents.mcp.cold_start import is_cold_start_envelope, require_ready
from kairix.agents.mcp.readiness import EventReadinessGate, ReadinessGate
from tests.fakes import FakeReadinessGate

pytestmark = pytest.mark.contract

_GATES: list[tuple[str, Callable[[], ReadinessGate]]] = [
    ("real", EventReadinessGate),
    ("fake", FakeReadinessGate),
]


def _envelope_for(gate: ReadinessGate) -> object:
    """What a tool handler sees: the cold-start envelope, or ``None`` to proceed.

    ``warm_progress_source`` pins "warm not started" so the envelope is
    the deterministic static shape (no live warm-state global read).
    """
    return require_ready("search", gate.is_ready, warm_progress_source=lambda: None)


@pytest.mark.parametrize("name,factory", _GATES)
def test_is_ready_unavailable_before_warm_up_returns_cold_start_envelope(
    name: str, factory: Callable[[], ReadinessGate]
) -> None:
    """``unavailable``: before warm-up the gate reports not-ready and the
    tool layer answers with the cold-start envelope — never a real
    (half-initialised) tool call.

    Sabotage proof (executed): make ``EventReadinessGate.__init__``
    default ``ready=True`` → the ``real`` case fails (``is_ready()`` is
    True and ``require_ready`` returns ``None``). Restored.
    """
    gate = factory()
    assert gate.is_ready() is False, name
    envelope = _envelope_for(gate)
    assert is_cold_start_envelope(envelope), f"{name}: expected KAIRIX_COLD_START, got {envelope!r}"


@pytest.mark.parametrize("name,factory", _GATES)
def test_mark_ready_unavailable_until_called_then_clears_cold_start_idempotently(
    name: str, factory: Callable[[], ReadinessGate]
) -> None:
    """``unavailable``: the gate stays cold-start until ``mark_ready()``;
    after it (called twice — the hook may fire again on a re-warm) the
    tool layer proceeds and the gate stays ready.

    Sabotage proof (executed): make ``EventReadinessGate.mark_ready`` a
    no-op → the ``real`` case fails (the envelope persists after
    warm-up, so every tool call would be refused forever). Restored.
    """
    gate = factory()
    assert is_cold_start_envelope(_envelope_for(gate)), name
    gate.mark_ready()
    gate.mark_ready()
    assert gate.is_ready() is True
    assert _envelope_for(gate) is None
