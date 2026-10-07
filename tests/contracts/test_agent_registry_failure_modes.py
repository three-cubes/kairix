"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`AgentRegistry`.

Three Protocol methods (``list_agents`` / ``collection_for`` /
``validate_write``). Every body runs over BOTH the production
:class:`ConfigDrivenAgentRegistry` and the canonical
:class:`tests.fakes.FakeAgentRegistry` (F43 behavioural parity), each
built from the same declarative agent spec. The "unknown agent" failure
path: ``collection_for`` raises ``KeyError``, ``validate_write`` returns
``False`` (the "no" outcome — observable + sabotage-provable), and
``list_agents`` returns the empty list when none configured.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import AgentRegistry
from kairix.core.search.registry import AgentDef, ConfigDrivenAgentRegistry
from tests.fakes import FakeAgentRegistry

pytestmark = pytest.mark.contract

RegistryFactory = Callable[[list[dict[str, Any]]], AgentRegistry]


def _real_registry(specs: list[dict[str, Any]]) -> AgentRegistry:
    """Production registry built from the same spec rows the fake takes."""
    return ConfigDrivenAgentRegistry(
        [
            AgentDef(
                name=s["name"],
                write_path=s.get("write_path", ""),
                read_only=s.get("read_only", False),
                legacy_collection_name=s.get("collection", ""),
            )
            for s in specs
        ]
    )


def _fake_registry(specs: list[dict[str, Any]]) -> AgentRegistry:
    return FakeAgentRegistry(agents=specs)


_IMPLEMENTATIONS: list[tuple[str, RegistryFactory]] = [
    ("real", _real_registry),
    ("fake", _fake_registry),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_list_agents_returns_empty_when_no_agents_configured(name: str, factory: RegistryFactory) -> None:
    """A registry constructed with no agents returns an empty list —
    callers MUST tolerate the empty case (e.g. ALL_AGENTS scope resolves
    to "everything", not a crash).

    Sabotage proof: in ``ConfigDrivenAgentRegistry.list_agents`` return
    ``[AgentDef(name="ghost")]``. Re-run: the ``real`` case fails because
    the result has one entry instead of zero. Restored.
    """
    registry = factory([])
    result = registry.list_agents()
    assert result == [], f"{name}: empty registry must return empty list; got {result!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_collection_for_raises_key_error_when_agent_unknown(name: str, factory: RegistryFactory) -> None:
    """``collection_for("unknown")`` must raise — silent fallback to a
    default collection would let typos route writes to the wrong agent.

    Sabotage proof: in ``ConfigDrivenAgentRegistry.get`` replace the
    ``raise KeyError(...)`` with ``return AgentDef(name=name)``. Re-run:
    the ``real`` case fails because no exception is raised. Restored.
    """
    registry = factory([{"name": "agent-alpha", "collection": "alpha-mem"}])
    with pytest.raises(KeyError, match="unknown"):
        registry.collection_for("does-not-exist")
    # Positive control on the same impl: the known agent resolves to the
    # same collection on both impls.
    assert registry.collection_for("agent-alpha") == "alpha-mem", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_validate_write_returns_empty_negative_when_path_outside_scope(name: str, factory: RegistryFactory) -> None:
    """``validate_write`` returns ``False`` when the agent exists but the
    path is outside its ``write_path`` — the "no" answer IS the failure
    outcome (the caller refuses the write).

    Sabotage proof (executed): in ``ConfigDrivenAgentRegistry.validate_write``
    change ``return agent.claims_write(path)`` to ``return True``. Re-run:
    the ``real`` case fails because every path is allowed. Restored.
    """
    registry = factory([{"name": "agent-alpha", "collection": "alpha-mem", "write_path": "agents/alpha"}])
    assert registry.validate_write("agent-alpha", "agents/beta/notes.md") is False, name
    # And the unknown-agent branch returns False, not raises — silent
    # negative is the documented Protocol shape for "no such agent".
    assert registry.validate_write("agent-ghost", "any/path") is False, name
    # Positive control: inside the write zone is allowed on both impls.
    assert registry.validate_write("agent-alpha", "agents/alpha/notes.md") is True, name
