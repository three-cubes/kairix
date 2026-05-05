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


# ---------------------------------------------------------------------------
# build_agent_owner_resolver — used by embed scanner for #114 tagging
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolver_returns_agent_for_path_under_write_path() -> None:
    """A doc under an agent's write_path resolves to that agent's name."""
    from kairix.core.search.registry import (
        AgentDef,
        ConfigDrivenAgentRegistry,
        build_agent_owner_resolver,
    )

    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="shape", collection="shape-memory", write_path="04-Agent-Knowledge/shape/memory"),
            AgentDef(name="builder", collection="builder-memory", write_path="04-Agent-Knowledge/builder/memory"),
        ]
    )
    resolver = build_agent_owner_resolver(registry)

    assert resolver("agent-knowledge", "04-Agent-Knowledge/shape/memory/note.md") == "shape"
    assert resolver("agent-knowledge", "04-Agent-Knowledge/builder/memory/log.md") == "builder"


@pytest.mark.unit
def test_resolver_returns_none_for_path_outside_any_write_path() -> None:
    """Docs not under any agent's write_path resolve to None (shared)."""
    from kairix.core.search.registry import (
        AgentDef,
        ConfigDrivenAgentRegistry,
        build_agent_owner_resolver,
    )

    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="shape", collection="shape-memory", write_path="04-Agent-Knowledge/shape/memory")]
    )
    resolver = build_agent_owner_resolver(registry)
    assert resolver("areas", "02-Areas/doc.md") is None
    assert resolver("knowledge", "05-Knowledge/methods/method.md") is None


@pytest.mark.unit
def test_resolver_longest_prefix_wins() -> None:
    """If two agents have nested write_paths, the longest prefix wins.

    Prevents ``shared/foo`` accidentally matching agent whose write_path
    is ``shared`` when a more specific agent owns ``shared/foo``.
    """
    from kairix.core.search.registry import (
        AgentDef,
        ConfigDrivenAgentRegistry,
        build_agent_owner_resolver,
    )

    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="general", collection="g", write_path="shared"),
            AgentDef(name="specific", collection="s", write_path="shared/team-a"),
        ]
    )
    resolver = build_agent_owner_resolver(registry)
    assert resolver("c", "shared/team-a/note.md") == "specific"
    assert resolver("c", "shared/team-b/note.md") == "general"


@pytest.mark.unit
def test_resolver_skips_agents_without_write_path() -> None:
    """Agents with empty write_path are skipped — they own no specific docs."""
    from kairix.core.search.registry import (
        AgentDef,
        ConfigDrivenAgentRegistry,
        build_agent_owner_resolver,
    )

    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="ghost", collection="g", write_path=""),
            AgentDef(name="real", collection="r", write_path="memory/real"),
        ]
    )
    resolver = build_agent_owner_resolver(registry)
    assert resolver("c", "memory/real/log.md") == "real"
    # The ghost agent doesn't claim anything; this matches no agent
    assert resolver("c", "anything/else.md") is None


@pytest.mark.unit
def test_resolver_exact_path_match() -> None:
    """A document at exactly the write_path itself (no trailing component) matches."""
    from kairix.core.search.registry import (
        AgentDef,
        ConfigDrivenAgentRegistry,
        build_agent_owner_resolver,
    )

    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="shape", collection="s", write_path="shape-area")])
    resolver = build_agent_owner_resolver(registry)
    # Normally a directory, but support the edge case of write_path-as-file
    assert resolver("c", "shape-area") == "shape"
    # And the prefix case
    assert resolver("c", "shape-area/sub/doc.md") == "shape"
    # But not a sibling directory with the same prefix string
    assert resolver("c", "shape-area-other/doc.md") is None
