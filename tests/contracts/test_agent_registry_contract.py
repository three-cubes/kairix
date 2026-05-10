"""Contract tests for the AgentRegistry Protocol and AgentDef value object.

Each test pins a behaviour documented in
``kairix/core/search/registry.py``'s docstrings. Read the docstring,
write what it claims, and sabotage-check the assertion.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kairix.core.protocols import AgentRegistry
from kairix.core.search.registry import (
    DEFAULT_AGENT_WORKSPACE_TEMPLATE,
    RESERVED_AGENT_COLLECTION_NAMES,
    AgentDef,
    ConfigDrivenAgentRegistry,
    build_agent_owner_resolver,
    parse_agent_registry,
)
from tests.fakes import FakeAgentRegistry

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# AgentDef.effective_paths — documented resolution order:
#   1. ``paths`` if explicitly declared.
#   2. ``[write_path]`` for legacy single-path agents.
#   3. ``[DEFAULT_AGENT_WORKSPACE_TEMPLATE.format(name=...)]`` out-of-the-box.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_effective_paths_uses_explicit_paths_when_declared() -> None:
    agent = AgentDef(name="alpha", paths=["/zone/a", "/zone/b"], write_path="/zone/a")
    assert agent.effective_paths == ["/zone/a", "/zone/b"]


@pytest.mark.contract
def test_effective_paths_falls_back_to_write_path_when_paths_empty() -> None:
    agent = AgentDef(name="alpha", paths=[], write_path="/zone/a")
    assert agent.effective_paths == ["/zone/a"]


@pytest.mark.contract
def test_effective_paths_falls_back_to_default_workspace_when_both_empty() -> None:
    agent = AgentDef(name="alpha")
    assert agent.effective_paths == [DEFAULT_AGENT_WORKSPACE_TEMPLATE.format(name="alpha")]
    # Sanity-check: the template embeds the agent name, not a hardcoded literal.
    assert "alpha" in agent.effective_paths[0]


# ---------------------------------------------------------------------------
# AgentDef.collection_names — documented:
#   "Subsequent collections use the ``{name}-{i}`` convention"
#   "First name honours legacy_collection_name … if the YAML supplied
#    the old ``collection:`` field"
# Sabotage-check: legacy_collection_name overrides ONLY the first slot.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_collection_names_synthetic_naming_for_each_effective_path() -> None:
    agent = AgentDef(name="alpha", paths=["/zone/a", "/zone/b", "/zone/c"])
    # Three paths → three synthetic collection names.
    assert agent.collection_names() == ["alpha-0", "alpha-1", "alpha-2"]


@pytest.mark.contract
def test_collection_names_legacy_override_replaces_only_first_entry() -> None:
    agent = AgentDef(
        name="alpha",
        paths=["/zone/a", "/zone/b"],
        legacy_collection_name="alpha-memory",
    )
    names = agent.collection_names()
    # First slot = legacy override; remainder follows the {name}-{i} convention.
    assert names == ["alpha-memory", "alpha-1"]


@pytest.mark.contract
def test_agentdef_collection_returns_first_synthetic_name() -> None:
    """The legacy single-collection accessor returns the first name only."""
    agent = AgentDef(name="alpha", paths=["/zone/a", "/zone/b"])
    assert agent.collection == "alpha-0"

    agent_with_override = AgentDef(
        name="alpha",
        paths=["/zone/a", "/zone/b"],
        legacy_collection_name="alpha-memory",
    )
    assert agent_with_override.collection == "alpha-memory"


# ---------------------------------------------------------------------------
# AgentDef.resolved_paths — documented contract for embed scanner.
#   "Absolute paths used as-is. Relative paths joined with document_root."
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_resolved_paths_keeps_absolute_paths_unchanged() -> None:
    agent = AgentDef(name="alpha", paths=["/abs/zone/a"])
    [resolved] = agent.resolved_paths(Path("/some/root"))
    assert resolved == Path("/abs/zone/a")


@pytest.mark.contract
def test_resolved_paths_joins_relative_paths_with_document_root() -> None:
    agent = AgentDef(name="alpha", paths=["rel/zone/a"])
    [resolved] = agent.resolved_paths(Path("/some/root"))
    assert resolved == Path("/some/root/rel/zone/a")


@pytest.mark.contract
def test_resolved_paths_handles_mixed_absolute_and_relative() -> None:
    agent = AgentDef(name="alpha", paths=["/abs/one", "rel/two"])
    resolved = agent.resolved_paths(Path("/root"))
    assert resolved == [Path("/abs/one"), Path("/root/rel/two")]


# ---------------------------------------------------------------------------
# ConfigDrivenAgentRegistry.collections_for / all_collections
# Documented: "Every agent's collection names, deduped, in registration order."
# Sabotage-check: dedupe matters when two agents share a path with the same
# legacy override; iteration is stable in YAML/registration order.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_collections_for_returns_every_synthetic_name() -> None:
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", paths=["/zone/a", "/zone/b", "/zone/c"])])
    assert registry.collections_for("alpha") == ["alpha-0", "alpha-1", "alpha-2"]


@pytest.mark.contract
def test_all_collections_dedupes_when_two_agents_share_a_collection_name() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="alpha", paths=["/shared"], legacy_collection_name="shared-zone"),
            AgentDef(name="beta", paths=["/shared"], legacy_collection_name="shared-zone"),
        ]
    )
    # Same legacy collection name for both → present once, not twice.
    assert registry.all_collections() == ["shared-zone"]


@pytest.mark.contract
def test_all_collections_preserves_registration_order() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="zulu", legacy_collection_name="zulu-c"),
            AgentDef(name="alpha", legacy_collection_name="alpha-c"),
            AgentDef(name="mike", legacy_collection_name="mike-c"),
        ]
    )
    # YAML/declaration order is preserved — NOT alphabetical.
    assert registry.all_collections() == ["zulu-c", "alpha-c", "mike-c"]


# ---------------------------------------------------------------------------
# ConfigDrivenAgentRegistry.get — informative KeyError for bad lookup.
# Documented: "raising KeyError if unknown".
# The message lists registered agents so the operator can see what was
# loaded vs what was requested.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_get_raises_keyerror_naming_unknown_and_registered_agents() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="alpha", legacy_collection_name="a"),
            AgentDef(name="beta", legacy_collection_name="b"),
        ]
    )
    with pytest.raises(KeyError) as excinfo:
        registry.get("ghost")
    msg = str(excinfo.value)
    assert "ghost" in msg
    # Registered agents listed for the operator
    assert "alpha" in msg and "beta" in msg


# ---------------------------------------------------------------------------
# parse_agent_registry — RESERVED_AGENT_COLLECTION_NAMES clash guard.
# Documented: "We therefore refuse to honour the override at parse time and
# substitute the agent's synthetic ``{name}-{i}`` naming, with a logged
# warning."
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_parse_logs_warning_and_drops_override_when_collection_clashes_with_reserved_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sanity-check: a reserved name actually exists; otherwise this test asserts nothing.
    assert RESERVED_AGENT_COLLECTION_NAMES, "no reserved names defined; reserved-clash test is vacuous"
    reserved = next(iter(RESERVED_AGENT_COLLECTION_NAMES))

    raw = {
        "agents": [
            {
                "name": "alpha",
                "collection": reserved,
                "write_path": "alpha-zone",
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        registry = parse_agent_registry(raw)

    # 1. A warning was logged naming the agent and the offending collection.
    assert any(
        "alpha" in r.message and reserved in r.message and "auto-injected" in r.message for r in caplog.records
    ), f"expected warning naming reserved collection clash; got: {[r.message for r in caplog.records]}"

    # 2. The override was dropped — agent uses synthetic naming, NOT the reserved name.
    agent = registry.get("alpha")
    assert agent.legacy_collection_name == "", (
        f"reserved override leaked into legacy_collection_name: {agent.legacy_collection_name!r}"
    )
    assert reserved not in agent.collection_names(), (
        f"reserved name still appears in collection_names: {agent.collection_names()}"
    )
    # And the agent's first synthetic collection follows the {name}-{i} convention.
    assert agent.collection_names()[0] == "alpha-0"


# ---------------------------------------------------------------------------
# parse_agent_registry — read_only flag round-trips into AgentDef.
# Documented schema field "read_only: false # optional; true skips write
# validation".
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_parse_round_trips_read_only_flag() -> None:
    raw = {
        "agents": [
            {"name": "active", "collection": "a", "write_path": "active-zone", "read_only": False},
            {"name": "archive", "collection": "arch", "write_path": "archive-zone", "read_only": True},
        ]
    }
    registry = parse_agent_registry(raw)
    assert registry.get("active").read_only is False
    assert registry.get("archive").read_only is True
    # And read_only=True is honoured downstream by validate_write.
    assert registry.validate_write("archive", "archive-zone/anything.md") is False
    assert registry.validate_write("active", "active-zone/anything.md") is True


# ---------------------------------------------------------------------------
# parse_agent_registry — legacy ``collection:`` emits a deprecation warning.
# Documented (#115): "Schema (legacy, still parses with a deprecation warning)".
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_parse_warns_when_legacy_collection_field_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A YAML using the deprecated ``collection:`` form parses correctly but
    emits a deprecation warning naming the agent and the suggested
    multi-path replacement.
    """
    raw = {
        "agents": [
            {
                "name": "alpha",
                "collection": "alpha-memory",
                "write_path": "agents/alpha",
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        registry = parse_agent_registry(raw)

    # Behaviour preserved: legacy override still wins on the first synthetic name.
    assert registry.collection_for("alpha") == "alpha-memory"

    # Deprecation warning emitted naming the agent + the deprecated field.
    deprecation_warnings = [r for r in caplog.records if "alpha" in r.message and "deprecated" in r.message]
    assert deprecation_warnings, (
        f"expected a deprecation warning naming agent 'alpha'; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# parse_agent_registry — multi-path schema round-trip.
# Documented schema:
#   agents:
#     - name: alpha
#       paths: [/data/workspaces/alpha, 04-Agent-Knowledge/alpha]
#       write_path: /data/workspaces/alpha
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_parse_round_trips_multi_path_schema() -> None:
    raw = {
        "agents": [
            {
                "name": "alpha",
                "paths": ["/data/workspaces/alpha", "04-Agent-Knowledge/alpha"],
                "write_path": "/data/workspaces/alpha",
            }
        ]
    }
    registry = parse_agent_registry(raw)
    agent = registry.get("alpha")
    assert agent.paths == ["/data/workspaces/alpha", "04-Agent-Knowledge/alpha"]
    assert agent.write_path == "/data/workspaces/alpha"
    # Multi-path → two synthetic collections.
    assert agent.collection_names() == ["alpha-0", "alpha-1"]


# ---------------------------------------------------------------------------
# build_agent_owner_resolver — stable-sort tie-break for equal-length paths.
# Documented: "ties keep YAML-declaration order so operators have a
# deterministic knob — operators can flip this by re-ordering the
# ``agents:`` list."
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_resolver_equal_length_paths_break_ties_by_declaration_order() -> None:
    # Both agents declare the same path string (same length). The first agent
    # in the list wins. Sabotage-proof: flip the order and re-check.
    registry_first_alpha = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="alpha", legacy_collection_name="a", write_path="shared/zone"),
            AgentDef(name="beta", legacy_collection_name="b", write_path="shared/zone"),
        ]
    )
    resolve_first_alpha = build_agent_owner_resolver(registry_first_alpha)
    assert resolve_first_alpha("c", "shared/zone/note.md") == "alpha"

    registry_first_beta = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="beta", legacy_collection_name="b", write_path="shared/zone"),
            AgentDef(name="alpha", legacy_collection_name="a", write_path="shared/zone"),
        ]
    )
    resolve_first_beta = build_agent_owner_resolver(registry_first_beta)
    # Flipping registration order flips the tie-break.
    assert resolve_first_beta("c", "shared/zone/note.md") == "beta"
