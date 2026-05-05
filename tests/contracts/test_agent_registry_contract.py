"""Contract tests for the AgentRegistry Protocol."""

from __future__ import annotations

import pytest

from kairix.core.protocols import AgentRegistry
from kairix.core.search.registry import AgentDef, ConfigDrivenAgentRegistry
from tests.fakes import FakeAgentRegistry


@pytest.mark.contract
def test_config_driven_registry_satisfies_protocol() -> None:
    assert isinstance(ConfigDrivenAgentRegistry(), AgentRegistry)


@pytest.mark.contract
def test_fake_registry_satisfies_protocol() -> None:
    assert isinstance(FakeAgentRegistry(), AgentRegistry)


@pytest.mark.contract
def test_registry_returns_iterable_of_agent_defs() -> None:
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", legacy_collection_name="alpha-mem")])
    listed = registry.list_agents()
    assert len(listed) == 1
    assert listed[0].name == "alpha"
    assert listed[0].collection == "alpha-mem"
