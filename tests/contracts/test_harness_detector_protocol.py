"""Protocol-shape contract for :mod:`kairix.core.agents.detectors` (PR 1.3 / #420).

Pins the structural promises of :class:`HarnessDetector` and the registry
helper :func:`get_registered_detectors`. PR 1.4's ``kairix onboard scan``
will iterate every registered detector to bootstrap an agent's
:class:`~kairix.core.agents.scope.AgentScope` from disk; this contract
freezes the shape those callers depend on.

Three shape promises:
  * :class:`HarnessDetector` is a runtime-checkable :class:`~typing.Protocol`
    with a ``name: str`` attribute and a ``propose_surfaces`` method that
    returns ``tuple[AgentSurface, ...]``.
  * Each of :class:`ClaudeCodeDetector`, :class:`CodexDetector`, and
    :class:`GenericDetector` satisfies the protocol at runtime.
  * :func:`get_registered_detectors` returns exactly the three detectors
    in deterministic order with the ``generic`` detector last so callers
    can treat it as a fallback.

F43 parity: every body runs over every real detector AND the canonical
:class:`tests.fakes.FakeHarnessDetector`. The registry promises (a single
concrete function, not a Protocol contract) live in
``tests/unit/test_detectors/test_detector_registry.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.agents.detectors import (
    ClaudeCodeDetector,
    CodexDetector,
    GenericDetector,
    HarnessDetector,
)
from kairix.core.agents.scope import AgentSurface
from tests.fakes import FakeHarnessDetector

pytestmark = pytest.mark.contract

#: Every HarnessDetector impl under contract — the three shipped detectors
#: plus the canonical fake. Each marker file below triggers its detector.
_IMPLS: list[HarnessDetector] = [ClaudeCodeDetector(), CodexDetector(), GenericDetector(), FakeHarnessDetector()]
_IDS = ["claude-code", "codex", "generic", "fake"]
#: The marker file that triggers each impl, in ``_IMPLS`` order.
_MARKERS: list[str] = ["CLAUDE.md", ".codex", "MEMORY.md", "FAKE.md"]


# Sabotage-proof (executed): renamed `propose_surfaces` on GenericDetector
# to `_propose_surfaces` → the generic leg's isinstance check returned
# False; test failed; restored.
@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_detector_satisfies_protocol(impl: HarnessDetector) -> None:
    """Every detector is structurally a HarnessDetector — has the ``name``
    attribute and the ``propose_surfaces`` method that callers in PR 1.4
    rely on. (Replaces the three per-detector copies of this body.)"""
    assert isinstance(impl, HarnessDetector)


# Sabotage-proof (executed): changed ClaudeCodeDetector.name from
# "claude-code" to "" → the claude-code leg's truthy-name assertion
# failed; restored.
@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_each_detector_has_non_empty_name(impl: HarnessDetector) -> None:
    """Every detector reports a non-empty ``name`` — callers use it for
    log lines and operator-facing proposal grouping."""
    assert isinstance(impl.name, str)
    assert impl.name, f"detector {type(impl).__name__} reported empty name"


# Sabotage-proof (executed): made ClaudeCodeDetector.propose_surfaces return
# ``list(surfaces)`` instead of ``tuple(surfaces)`` → the claude-code leg's
# isinstance(result, tuple) failed; restored.
@pytest.mark.parametrize(("impl", "marker"), list(zip(_IMPLS, _MARKERS, strict=True)), ids=_IDS)
def test_propose_surfaces_returns_tuple_of_agent_surface(impl: HarnessDetector, marker: str, tmp_path: Path) -> None:
    """``propose_surfaces`` always returns ``tuple[AgentSurface, ...]`` —
    never a list, dict, or generator. With the detector's marker present
    the tuple carries a ``memory`` surface anchored at the candidate."""
    (tmp_path / marker).write_text("# project")
    out = impl.propose_surfaces("agent-alpha", tmp_path)
    assert isinstance(out, tuple)
    assert out, f"{impl.name!r} must propose a surface when {marker} is present"
    for item in out:
        assert isinstance(item, AgentSurface)
    assert out[0].path == tmp_path
    assert out[0].label == "memory"


# Sabotage-proof (executed): made GenericDetector.propose_surfaces return
# ``None`` for a directory with no recognised markdown → the generic leg's
# ``== ()`` assertion failed; restored.
@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_empty_directory_yields_empty_tuple_not_none(impl: HarnessDetector, tmp_path: Path) -> None:
    """An empty candidate directory — and a missing one — yields an empty
    tuple from every detector: never None, never a raise. PR 1.4 relies
    on this so the aggregation loop can concatenate without None-guarding."""
    out = impl.propose_surfaces("agent-alpha", tmp_path)
    assert out == ()
    assert isinstance(out, tuple)
    assert impl.propose_surfaces("agent-alpha", tmp_path / "missing") == ()
