"""Unit tests for ConfigDrivenAgentRegistry + parse_agent_registry."""

from __future__ import annotations

import pytest

from kairix.core.search.registry import (
    AgentDef,
    ConfigDrivenAgentRegistry,
    parse_agent_registry,
)


@pytest.mark.unit
def test_empty_registry_lists_no_agents() -> None:
    registry = ConfigDrivenAgentRegistry()
    assert registry.list_agents() == []


@pytest.mark.unit
def test_collection_for_returns_declared_collection() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="alpha", collection="alpha-memory", write_path="agents/alpha")]
    )
    assert registry.collection_for("alpha") == "alpha-memory"


@pytest.mark.unit
def test_collection_for_raises_on_unknown_agent() -> None:
    registry = ConfigDrivenAgentRegistry()
    with pytest.raises(KeyError):
        registry.collection_for("nobody")


@pytest.mark.unit
def test_validate_write_accepts_path_under_write_path() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="alpha", collection="alpha-memory", write_path="agents/alpha")]
    )
    assert registry.validate_write("alpha", "agents/alpha/memory/2026-05-04.md") is True
    assert registry.validate_write("alpha", "agents/alpha") is True


@pytest.mark.unit
def test_validate_write_rejects_path_outside_write_path() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="alpha", collection="alpha-memory", write_path="agents/alpha")]
    )
    assert registry.validate_write("alpha", "agents/beta/notes.md") is False


@pytest.mark.unit
def test_validate_write_rejects_read_only_agent() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="alpha", collection="alpha-memory", write_path="agents/alpha", read_only=True)]
    )
    assert registry.validate_write("alpha", "agents/alpha/anything.md") is False


@pytest.mark.unit
def test_validate_write_rejects_unknown_agent() -> None:
    registry = ConfigDrivenAgentRegistry()
    assert registry.validate_write("nobody", "anywhere") is False


@pytest.mark.unit
def test_parse_agent_registry_with_explicit_collection() -> None:
    data = {
        "agents": [
            {"name": "alpha", "collection": "alpha-zone", "write_path": "agents/alpha"},
            {"name": "beta"},  # no collection or write_path
        ]
    }
    registry = parse_agent_registry(data)
    listed = registry.list_agents()
    assert len(listed) == 2
    assert {a.name for a in listed} == {"alpha", "beta"}
    assert registry.collection_for("alpha") == "alpha-zone"
    # Beta gets default {agent}-memory pattern
    assert registry.collection_for("beta") == "beta-memory"


@pytest.mark.unit
def test_parse_agent_registry_with_custom_pattern() -> None:
    data = {"agents": [{"name": "alpha"}]}
    registry = parse_agent_registry(data, default_pattern="{agent}-store")
    assert registry.collection_for("alpha") == "alpha-store"


@pytest.mark.unit
def test_parse_agent_registry_returns_empty_when_section_missing() -> None:
    registry = parse_agent_registry({})
    assert registry.list_agents() == []


@pytest.mark.unit
def test_parse_agent_registry_skips_malformed_entries() -> None:
    data = {
        "agents": [
            {"name": "alpha"},
            "not-a-dict",  # malformed
            {"no_name": "field"},  # malformed
            {"name": "beta"},
        ]
    }
    registry = parse_agent_registry(data)
    assert {a.name for a in registry.list_agents()} == {"alpha", "beta"}
