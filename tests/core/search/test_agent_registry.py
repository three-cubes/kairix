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
        agents=[AgentDef(name="alpha", legacy_collection_name="alpha-memory", write_path="agents/alpha")]
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
        agents=[AgentDef(name="alpha", legacy_collection_name="alpha-memory", write_path="agents/alpha")]
    )
    assert registry.validate_write("alpha", "agents/alpha/memory/2026-05-04.md") is True
    assert registry.validate_write("alpha", "agents/alpha") is True


@pytest.mark.unit
def test_validate_write_rejects_path_outside_write_path() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="alpha", legacy_collection_name="alpha-memory", write_path="agents/alpha")]
    )
    assert registry.validate_write("alpha", "agents/beta/notes.md") is False


@pytest.mark.unit
def test_validate_write_rejects_read_only_agent() -> None:
    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="alpha", legacy_collection_name="alpha-memory", write_path="agents/alpha", read_only=True)
        ]
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
            AgentDef(name="shape", legacy_collection_name="shape-memory", write_path="04-Agent-Knowledge/shape/memory"),
            AgentDef(
                name="builder", legacy_collection_name="builder-memory", write_path="04-Agent-Knowledge/builder/memory"
            ),
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
        agents=[
            AgentDef(name="shape", legacy_collection_name="shape-memory", write_path="04-Agent-Knowledge/shape/memory")
        ]
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
            AgentDef(name="general", legacy_collection_name="g", write_path="shared"),
            AgentDef(name="specific", legacy_collection_name="s", write_path="shared/team-a"),
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
            AgentDef(name="ghost", legacy_collection_name="g", write_path=""),
            AgentDef(name="real", legacy_collection_name="r", write_path="memory/real"),
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

    registry = ConfigDrivenAgentRegistry(
        agents=[AgentDef(name="shape", legacy_collection_name="s", write_path="shape-area")]
    )
    resolver = build_agent_owner_resolver(registry)
    # Normally a directory, but support the edge case of write_path-as-file
    assert resolver("c", "shape-area") == "shape"
    # And the prefix case
    assert resolver("c", "shape-area/sub/doc.md") == "shape"
    # But not a sibling directory with the same prefix string
    assert resolver("c", "shape-area-other/doc.md") is None


# ---------------------------------------------------------------------------
# Branch-coverage tests — read-only agents and degenerate path entries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_owns_path_returns_false_for_read_only_agent() -> None:
    """A read-only agent owns nothing, even paths inside its declared zone."""
    agent = AgentDef(
        name="ro-archive",
        legacy_collection_name="archive",
        write_path="06-Archive/curator",
        read_only=True,
    )
    # The path is exactly inside the declared write zone, but read_only=True
    # short-circuits ownership to False.
    assert agent.owns_path("06-Archive/curator/note.md") is False
    # Other paths obviously also return False.
    assert agent.owns_path("06-Archive/curator") is False


@pytest.mark.unit
def test_owns_path_skips_empty_path_entries_in_effective_paths() -> None:
    """An empty/whitespace ``paths`` entry is skipped, not treated as 'owns everything'.

    Without the ``if not wp: continue`` guard, an empty string after rstrip
    would match every rel_path via ``rel_path.startswith("" + "/")`` semantics,
    falsely claiming ownership.
    """
    agent = AgentDef(
        name="weird",
        legacy_collection_name="weird",
        write_path="real-zone",
        # Mix a real path with an empty entry to ensure the guard fires.
        paths=("", "real-zone"),
    )
    # The real path still owns its zone.
    assert agent.owns_path("real-zone/x.md") is True
    # An unrelated path is NOT claimed via the empty entry.
    assert agent.owns_path("totally/unrelated.md") is False


@pytest.mark.unit
def test_build_agent_owner_resolver_excludes_read_only_agents() -> None:
    """``build_agent_owner_resolver`` skips read-only agents — they own no docs."""
    from kairix.core.search.registry import build_agent_owner_resolver

    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name="active", legacy_collection_name="a", write_path="active-zone"),
            AgentDef(
                name="archive",
                legacy_collection_name="arch",
                write_path="archive-zone",
                read_only=True,
            ),
        ]
    )
    resolver = build_agent_owner_resolver(registry)

    assert resolver("c", "active-zone/x.md") == "active"
    # The read-only agent's path resolves to None — the resolver doesn't list
    # it as an owner candidate.
    assert resolver("c", "archive-zone/x.md") is None


@pytest.mark.unit
def test_parse_agent_registry_logs_warning_when_paths_field_is_not_a_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-list ``paths`` value in the YAML is rejected with a warning, paths default to empty.

    Without this guard, a misconfigured ``paths: "single string"`` would be
    iterated character-by-character with surprising results.
    """
    import logging

    raw = {
        "agents": [
            {
                "name": "shape",
                "collection": "shape-memory",
                "paths": "not-a-list",  # invalid — expected list
                "write_path": "shape-area",
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        registry = parse_agent_registry(raw)

    # Warning emitted naming the offending key + the offending value.
    assert any(
        "paths" in r.message and "must be a list" in r.message and "not-a-list" in r.message for r in caplog.records
    ), f"expected warning naming the bad paths value; got: {[r.message for r in caplog.records]}"
    # The registry still loaded the agent. The bad ``paths: "not-a-list"`` was
    # discarded — the resulting paths come only from the write_path fallback,
    # never from iterating the bad value's characters.
    agent = registry.get("shape")
    assert agent.write_path == "shape-area"
    # Specifically: no path entry derives from the rejected string. If the
    # guard hadn't fired, ``paths_raw = "not-a-list"`` would have been
    # iterated as ['n','o','t',...], producing single-character entries.
    assert all(len(p) > 1 for p in agent.paths), f"single-char paths leaked from rejected string: {agent.paths}"


@pytest.mark.unit
def test_legacy_collection_deprecation_warning_dedupes_across_invocations(caplog: pytest.LogCaptureFixture) -> None:
    """#275: parse_agent_registry runs once per benchmark case in the eval path.

    Without dedup, an N-agent suite producing the legacy ``collection:`` field
    spews N x M warning lines (M cases). That drowns out real benchmark stderr
    in container logs and (in the alpha-deploy webhook's CombinedOutput) made
    the JSON-parse step impossible. The warning should fire exactly once per
    (agent, candidate) per process.

    Sabotage-proof inline: remove the dedup guard in
    ``_resolve_legacy_collection_name`` and this test will see the warning N
    times instead of once.

    Uses unique agent names (``dedup-test-*``) so the per-process dedup state
    is naturally isolated from other tests — no private-name imports needed.
    """
    import logging
    import uuid

    # Unique-per-run names so the per-process dedup set never carries state
    # from prior tests into this one. (F5: no internal-name imports.)
    suffix = uuid.uuid4().hex[:8]
    name_a = f"dedup-test-shape-{suffix}"
    name_b = f"dedup-test-builder-{suffix}"
    raw = {
        "agents": [
            {"name": name_a, "collection": f"{name_a}-memory", "write_path": f"04-Agent-Knowledge/{name_a}"},
            {"name": name_b, "collection": f"{name_b}-memory", "write_path": f"04-Agent-Knowledge/{name_b}"},
        ]
    }

    with caplog.at_level(logging.WARNING):
        parse_agent_registry(raw)
        parse_agent_registry(raw)  # second pass — should NOT re-warn.
        parse_agent_registry(raw)  # third pass — still no new warnings.

    # Scope to *our* uniquely-named agents only — other tests may emit
    # deprecation warnings for their own agent names into the same caplog.
    deprecation_records = [
        r for r in caplog.records if "is deprecated" in r.message and r.args and r.args[0] in (name_a, name_b)
    ]
    assert len(deprecation_records) == 2, (
        f"expected exactly 2 deprecation warnings (one per agent); got {len(deprecation_records)}: "
        f"{[r.getMessage() for r in deprecation_records]}"
    )
    names_warned = {r.args[0] for r in deprecation_records}
    assert names_warned == {name_a, name_b}, f"expected warnings for both agents; got {names_warned}"
