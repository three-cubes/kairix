"""Shape contract for :mod:`kairix.core.agents.scope` (PR 1.1 / #420).

Moved from ``tests/contracts/test_agent_scope_protocol.py`` (PLA-472):
``AgentSurface`` / ``AgentScope`` are concrete frozen value objects with a
single implementation — there is no Protocol and no fake to prove parity
against — so these shape pins are unit tests of that one implementation.

Pins the structural promises of :class:`AgentSurface` + :class:`AgentScope`
that callers will depend on once PR 1.2 swaps the hardcoded
``{document_root}/04-Agent-Knowledge/<agent>/memory`` resolution onto the
config-driven scope.

Three shape promises:
  * ``AgentSurface`` is a frozen dataclass with ``path``/``glob``/``label`` fields
    and sensible defaults.
  * ``AgentScope`` is a frozen dataclass with ``name``/``surfaces``/``harness``.
  * ``AgentScope.memory_paths()`` projects all surface paths; ``writable_path()``
    prefers a surface labeled ``"memory"`` and falls back to the first surface;
    empty surfaces is a typed ``ValueError``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kairix.core.agents.scope import AgentScope, AgentSurface

pytestmark = pytest.mark.unit


# Sabotage-proof: changed @dataclass(frozen=True) to @dataclass on AgentSurface
# in scope.py → FrozenInstanceError no longer raised; test failed as expected;
# restored.
def test_agent_surface_is_frozen_with_expected_fields() -> None:
    """AgentSurface is frozen + carries (path, glob, label) with defaults."""
    surface = AgentSurface(path=Path("/tmp/x"))
    assert dataclasses.is_dataclass(surface)
    fields = {f.name: f for f in dataclasses.fields(surface)}
    assert set(fields) == {"path", "glob", "label"}
    assert surface.glob == "**/*.md"
    assert surface.label == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        surface.path = Path("/tmp/y")  # type: ignore[misc] — proving the frozen=True contract; mypy correctly flags the assignment as forbidden


# Sabotage-proof: removed `frozen=True` from AgentScope decorator → assignment
# succeeded and FrozenInstanceError was not raised; test failed; restored.
def test_agent_scope_is_frozen_with_expected_fields() -> None:
    """AgentScope is frozen + carries (name, surfaces, harness) with default
    ``harness=""``."""
    scope = AgentScope(name="agent-alpha", surfaces=(AgentSurface(path=Path("/tmp/m")),))
    assert dataclasses.is_dataclass(scope)
    fields = {f.name: f for f in dataclasses.fields(scope)}
    assert set(fields) == {"name", "surfaces", "harness"}
    assert scope.harness == ""
    assert isinstance(scope.surfaces, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.name = "agent-beta"  # type: ignore[misc] — proving the frozen=True contract; mypy correctly flags the assignment as forbidden


# Sabotage-proof: changed memory_paths to return a list (not tuple) → isinstance
# check failed; test failed; restored.
def test_memory_paths_returns_tuple_of_all_surface_paths() -> None:
    """memory_paths() projects every surface's path, in declared order."""
    p1 = Path("/tmp/mem")
    p2 = Path("/tmp/work")
    scope = AgentScope(
        name="agent-alpha",
        surfaces=(
            AgentSurface(path=p1, label="memory"),
            AgentSurface(path=p2, label="workspace"),
        ),
    )
    out = scope.memory_paths()
    assert isinstance(out, tuple)
    assert out == (p1, p2)
    assert all(isinstance(p, Path) for p in out)


# Sabotage-proof: removed the `if s.label == "memory"` branch from
# writable_path() → returned the workspace path first; test failed; restored.
def test_writable_path_prefers_memory_label_over_first_surface() -> None:
    """writable_path() prefers a surface labeled "memory" even when it
    appears after another surface in the declared order."""
    mem = Path("/tmp/mem")
    work = Path("/tmp/work")
    scope = AgentScope(
        name="agent-alpha",
        surfaces=(
            AgentSurface(path=work, label="workspace"),
            AgentSurface(path=mem, label="memory"),
        ),
    )
    assert scope.writable_path() == mem


# Sabotage-proof: changed the unlabeled-fallback branch to `return None` →
# returned None instead of Path; equality assertion failed; restored.
def test_writable_path_falls_back_to_first_surface_when_no_memory_label() -> None:
    """When no surface carries label="memory", writable_path() returns the
    first surface — the documented fallback shape."""
    first = Path("/tmp/first")
    scope = AgentScope(
        name="agent-alpha",
        surfaces=(
            AgentSurface(path=first, label="workspace"),
            AgentSurface(path=Path("/tmp/other"), label="archive"),
        ),
    )
    assert scope.writable_path() == first


# Sabotage-proof: changed the empty-surfaces branch to `return Path("/")` →
# ValueError no longer raised; pytest.raises failed; restored.
def test_writable_path_raises_value_error_when_surfaces_empty() -> None:
    """An AgentScope with no surfaces cannot satisfy a write request — raise
    ValueError with the agent name in the message so the operator sees which
    scope is misconfigured."""
    scope = AgentScope(name="agent-empty", surfaces=())
    with pytest.raises(ValueError, match="agent-empty"):
        scope.writable_path()
