"""Contract probes for ``kairix.core.search.registry``.

One probe per documented public-method claim plus boundary cases that
existing BDD/integration coverage does not pin. These tests treat the
module docstring + each method docstring as the spec; if a claim cannot
be sabotage-proven, it is not a real contract.
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import AgentRegistry
from kairix.core.search.registry import (
    AgentDef,
    ConfigDrivenAgentRegistry,
    parse_agent_registry,
)
from tests.fakes import FakeAgentRegistry

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# AgentDef — dataclass field defaults
# ---------------------------------------------------------------------------


def test_agent_def_defaults_write_path_empty_and_not_read_only() -> None:
    """AgentDef: write_path defaults to '' and read_only defaults to False.

    Default-constructed AgentDef is the strict-but-listable shape — listed
    by the registry, but rejected by validate_write because write_path is ''.
    """
    agent = AgentDef(name="alpha", collection="alpha-memory")
    assert agent.write_path == ""
    assert agent.read_only is False


# ---------------------------------------------------------------------------
# ConfigDrivenAgentRegistry — structural protocol conformance
# ---------------------------------------------------------------------------


def test_config_driven_registry_satisfies_agent_registry_protocol() -> None:
    """ConfigDrivenAgentRegistry must structurally satisfy AgentRegistry."""
    assert isinstance(ConfigDrivenAgentRegistry(), AgentRegistry)


def test_fake_registry_satisfies_agent_registry_protocol() -> None:
    """FakeAgentRegistry from tests/fakes.py must satisfy the same Protocol."""
    assert isinstance(FakeAgentRegistry(), AgentRegistry)


# ---------------------------------------------------------------------------
# list_agents — boundary: empty registry & arbitrary order, no aliasing
# ---------------------------------------------------------------------------


def test_list_agents_returns_empty_list_for_empty_registry() -> None:
    """Empty registry → list_agents returns [] (not None, not raising)."""
    assert ConfigDrivenAgentRegistry().list_agents() == []
    assert ConfigDrivenAgentRegistry(agents=None).list_agents() == []
    assert ConfigDrivenAgentRegistry(agents=[]).list_agents() == []


def test_list_agents_returns_all_registered_agent_defs() -> None:
    """Every constructed AgentDef appears in list_agents output."""
    a = AgentDef(name="alpha", collection="alpha-memory")
    b = AgentDef(name="beta", collection="beta-memory", write_path="p/b", read_only=True)
    listed = ConfigDrivenAgentRegistry(agents=[a, b]).list_agents()
    assert {x.name for x in listed} == {"alpha", "beta"}
    by_name = {x.name: x for x in listed}
    assert by_name["beta"].read_only is True
    assert by_name["beta"].write_path == "p/b"


def test_list_agents_returns_independent_list_not_internal_state() -> None:
    """Mutating the returned list must not corrupt registry internals."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="alpha-memory")])
    listed = registry.list_agents()
    listed.clear()
    # Internal state intact: a fresh call still sees alpha.
    assert [a.name for a in registry.list_agents()] == ["alpha"]


def test_duplicate_name_in_constructor_keeps_last_definition() -> None:
    """Duplicate agent names: registry collapses by name; last entry wins.

    Internal storage is a dict keyed by name, so a later AgentDef overrides
    an earlier one with the same name. This is a real contract — operators
    relying on duplicate-name precedence depend on this resolution.
    """
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="alpha", collection="first"),
            AgentDef(name="alpha", collection="second"),
        ]
    )
    listed = registry.list_agents()
    assert len(listed) == 1
    assert listed[0].collection == "second"
    assert registry.collection_for("alpha") == "second"


# ---------------------------------------------------------------------------
# collection_for — happy path, KeyError shape, sorted registered list
# ---------------------------------------------------------------------------


def test_collection_for_returns_declared_collection_string() -> None:
    """collection_for returns the AgentDef.collection string verbatim."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="alpha-zone")])
    assert registry.collection_for("alpha") == "alpha-zone"


def test_collection_for_raises_key_error_when_agent_unknown() -> None:
    """Unknown agent → KeyError (not None, not '', not silent)."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="alpha-memory")])
    with pytest.raises(KeyError):
        registry.collection_for("ghost")


def test_collection_for_key_error_message_lists_registered_agents_sorted() -> None:
    """KeyError message names the unknown agent and lists registered names sorted.

    Documented in the source (``f\"unknown agent {name!r}; registered: {sorted(...)}\"``).
    Operators rely on this for diagnosing typos in scope=agent calls.
    """
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="zulu", collection="z"),
            AgentDef(name="alpha", collection="a"),
            AgentDef(name="mike", collection="m"),
        ]
    )
    with pytest.raises(KeyError) as exc_info:
        registry.collection_for("ghost")
    message = str(exc_info.value)
    assert "ghost" in message
    # Sorted, not insertion order.
    assert message.index("alpha") < message.index("mike") < message.index("zulu")


def test_collection_for_empty_registry_includes_empty_registered_list() -> None:
    """Even on an empty registry, KeyError still names the unknown agent."""
    with pytest.raises(KeyError) as exc_info:
        ConfigDrivenAgentRegistry().collection_for("anything")
    assert "anything" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_write — every clause of the docstring
# ---------------------------------------------------------------------------


def test_validate_write_true_when_path_equals_write_path() -> None:
    """Path equal to write_path is treated as 'under' it."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c", write_path="agents/alpha")])
    assert registry.validate_write("alpha", "agents/alpha") is True


def test_validate_write_true_when_path_strictly_below_write_path() -> None:
    """Path below write_path (with a separator) is allowed."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c", write_path="agents/alpha")])
    assert registry.validate_write("alpha", "agents/alpha/sub/file.md") is True


def test_validate_write_false_for_sibling_path_with_shared_prefix() -> None:
    """Naive prefix match would let 'agents/alpha-other' through; the
    rstrip('/') + '/' separator step prevents that. Without it, an agent
    declared at 'agents/a' could write to 'agents/alpha/...'."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="a", collection="c", write_path="agents/a")])
    assert registry.validate_write("a", "agents/alpha/notes.md") is False


def test_validate_write_normalises_trailing_slash_on_write_path_for_prefix_check() -> None:
    """A write_path declared with trailing slash still admits sub-paths."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c", write_path="agents/alpha/")])
    assert registry.validate_write("alpha", "agents/alpha/file.md") is True


def test_validate_write_false_for_unrelated_path() -> None:
    """Path entirely outside write_path is rejected."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c", write_path="agents/alpha")])
    assert registry.validate_write("alpha", "agents/beta/notes.md") is False


def test_validate_write_false_when_agent_unknown() -> None:
    """Unknown agent → False (NOT a KeyError; the contract is a bool gate)."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c", write_path="agents/alpha")])
    assert registry.validate_write("ghost", "agents/alpha/x.md") is False


def test_validate_write_false_when_agent_is_read_only() -> None:
    """read_only=True overrides any path match."""
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(
                name="alpha",
                collection="c",
                write_path="agents/alpha",
                read_only=True,
            )
        ]
    )
    assert registry.validate_write("alpha", "agents/alpha/anything.md") is False


def test_validate_write_false_when_write_path_unset() -> None:
    """An agent with no declared write_path cannot write anywhere — strict default."""
    registry = ConfigDrivenAgentRegistry(agents=[AgentDef(name="alpha", collection="c")])
    assert registry.validate_write("alpha", "agents/alpha/file.md") is False
    # Even the literal empty string is rejected — empty write_path is a sentinel.
    assert registry.validate_write("alpha", "") is False


# ---------------------------------------------------------------------------
# parse_agent_registry — YAML-shape contract claims
# ---------------------------------------------------------------------------


def test_parse_returns_empty_registry_when_agents_section_missing() -> None:
    """Missing 'agents' key → empty registry (callers raise NotImplementedError loudly)."""
    assert parse_agent_registry({}).list_agents() == []


def test_parse_returns_empty_registry_when_agents_section_is_falsy() -> None:
    """None or [] for 'agents' is treated the same as missing."""
    assert parse_agent_registry({"agents": None}).list_agents() == []
    assert parse_agent_registry({"agents": []}).list_agents() == []


def test_parse_uses_explicit_collection_when_declared() -> None:
    """When YAML provides 'collection:', it is used verbatim, no pattern."""
    registry = parse_agent_registry({"agents": [{"name": "alpha", "collection": "alpha-zone"}]})
    assert registry.collection_for("alpha") == "alpha-zone"


def test_parse_derives_collection_from_default_pattern_when_omitted() -> None:
    """Omitted collection → default_pattern.format(agent=name) — '{agent}-memory'."""
    registry = parse_agent_registry({"agents": [{"name": "alpha"}]})
    assert registry.collection_for("alpha") == "alpha-memory"


def test_parse_honours_caller_supplied_default_pattern() -> None:
    """Caller can override the default pattern (e.g. '{agent}-store')."""
    registry = parse_agent_registry({"agents": [{"name": "alpha"}]}, default_pattern="{agent}-store")
    assert registry.collection_for("alpha") == "alpha-store"


def test_parse_treats_empty_collection_string_as_omitted() -> None:
    """An explicit but empty 'collection' value falls back to the pattern.

    The implementation uses ``item.get('collection') or default_pattern.format(...)``.
    Truthy-or means '' is treated as missing — operators who type
    ``collection: ''`` get the pattern rather than an empty collection name
    that would silently break downstream resolvers.
    """
    registry = parse_agent_registry({"agents": [{"name": "alpha", "collection": ""}]})
    assert registry.collection_for("alpha") == "alpha-memory"


def test_parse_propagates_write_path_and_read_only() -> None:
    """write_path and read_only flow through from YAML to validate_write."""
    registry = parse_agent_registry(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/alpha"},
                {"name": "beta", "write_path": "agents/beta", "read_only": True},
            ]
        }
    )
    assert registry.validate_write("alpha", "agents/alpha/x.md") is True
    assert registry.validate_write("beta", "agents/beta/x.md") is False


def test_parse_skips_non_dict_entries() -> None:
    """Non-dict items in the agents list are silently skipped (defensive)."""
    registry = parse_agent_registry({"agents": [{"name": "alpha"}, "not-a-dict", 42, None, {"name": "beta"}]})
    assert {a.name for a in registry.list_agents()} == {"alpha", "beta"}


def test_parse_skips_entries_without_a_name() -> None:
    """Dict entries without a truthy 'name' are skipped (cannot be addressed)."""
    registry = parse_agent_registry(
        {
            "agents": [
                {"collection": "orphan-memory"},  # no name
                {"name": ""},  # empty name = falsy
                {"name": "alpha"},
            ]
        }
    )
    assert [a.name for a in registry.list_agents()] == ["alpha"]


def test_parse_coerces_numeric_name_and_collection_to_string() -> None:
    """Non-string scalar values are coerced via str() — list_agents must
    return AgentDef instances whose name/collection are strings."""
    registry = parse_agent_registry({"agents": [{"name": 42, "collection": 99}]})
    listed = registry.list_agents()
    assert len(listed) == 1
    assert listed[0].name == "42"
    assert listed[0].collection == "99"
    assert registry.collection_for("42") == "99"
