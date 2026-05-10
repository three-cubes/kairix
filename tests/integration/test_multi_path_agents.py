"""End-to-end integration test for multi-path agent collections (#115).

Constructs a real ``ConfigDrivenAgentRegistry`` from YAML, wires it into a
real ``DefaultCollectionResolver``, and verifies that scope resolution
returns the correct multi-path collection lists. No fakes, no monkeypatch
— the actual production parsing + resolution code path is exercised end
to end.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from kairix.core.search.registry import parse_agent_registry
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope

pytestmark = pytest.mark.integration


def _load_registry_from_yaml(yaml_text: str):
    return parse_agent_registry(yaml.safe_load(yaml_text))


@pytest.mark.integration
def test_default_workspace_when_paths_omitted_resolves_correctly() -> None:
    """End-to-end: YAML omits paths → resolver produces /data/workspaces/{name}-shaped collections."""
    yaml_text = textwrap.dedent("""
        agents:
          - name: alice
            write_path: /data/workspaces/alice
    """)
    registry = _load_registry_from_yaml(yaml_text)
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=registry)

    cols = resolver.resolve("alice", Scope.AGENT)
    assert cols is not None
    assert len(cols) == 1
    # Synthetic name follows ``{agent}-memory`` legacy pattern when paths are omitted
    # (the parser falls back to ``default_pattern`` for that legacy scenario).
    assert cols[0] == "alice-memory" or cols[0].startswith("alice")


@pytest.mark.integration
def test_three_path_tc_pattern_resolves_to_three_collections() -> None:
    """End-to-end: TC-style three-path agent → resolver returns three synthetic names."""
    yaml_text = textwrap.dedent("""
        agents:
          - name: shape
            paths:
              - /data/workspaces/shape
              - 04-Agent-Knowledge/shape
              - 04-Agent-Knowledge/shared
            write_path: /data/workspaces/shape
    """)
    registry = _load_registry_from_yaml(yaml_text)
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=registry)

    cols = resolver.resolve("shape", Scope.AGENT)
    assert cols == ["shape-0", "shape-1", "shape-2"]


@pytest.mark.integration
def test_all_agents_dedupes_shared_collections() -> None:
    """End-to-end: scope=all-agents across two agents with a shared collection name dedupes."""
    yaml_text = textwrap.dedent("""
        agents:
          - name: shape
            collection: shared-knowledge
            paths:
              - /data/workspaces/shape
              - 04-Agent-Knowledge/shared
          - name: builder
            collection: shared-knowledge
            paths:
              - /data/workspaces/builder
              - 04-Agent-Knowledge/shared
    """)
    registry = _load_registry_from_yaml(yaml_text)
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=registry)

    cols = resolver.resolve(None, Scope.ALL_AGENTS)
    assert cols is not None
    # Each unique synthetic name appears exactly once
    assert len(cols) == len(set(cols)), f"duplicates in {cols}"
    # The shared legacy_collection_name appears exactly once
    assert cols.count("shared-knowledge") == 1


@pytest.mark.integration
def test_legacy_collection_field_backwards_compat() -> None:
    """End-to-end: a legacy YAML using the old ``collection:`` field still resolves cleanly."""
    yaml_text = textwrap.dedent("""
        agents:
          - name: legacy
            collection: legacy-memory
            write_path: 04-Agent-Knowledge/legacy
    """)
    registry = _load_registry_from_yaml(yaml_text)
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=registry)

    # Legacy single-collection access still works
    assert registry.collection_for("legacy") == "legacy-memory"

    # And resolver produces the expected name
    cols = resolver.resolve("legacy", Scope.AGENT)
    assert cols == ["legacy-memory"]


@pytest.mark.integration
def test_relative_paths_resolve_against_document_root(tmp_path: Path) -> None:
    """End-to-end: relative paths in YAML resolve against document_root for the scanner."""
    yaml_text = textwrap.dedent("""
        agents:
          - name: rel
            paths:
              - relative-area/rel
              - /absolute/path
    """)
    registry = _load_registry_from_yaml(yaml_text)
    agent = registry.get("rel")

    resolved = agent.resolved_paths(tmp_path)
    assert len(resolved) == 2
    assert resolved[0] == tmp_path / "relative-area/rel"
    assert resolved[1] == Path("/absolute/path")


@pytest.mark.integration
def test_registry_drops_legacy_name_clashing_with_auto_injected_collection() -> None:
    """Auto-injected collection names cannot be claimed by an agent's legacy ``collection:`` field.

    The embed harness injects a ``reference-library`` collection outside of
    the YAML config (see ``kairix/core/embed/cli.py``). An agent that also
    claims that name in YAML would shadow the injected collection in search
    routing. ``parse_agent_registry`` rejects the override at parse time and
    falls through to synthetic ``{name}-{i}`` naming so the auto-injected
    collection keeps routing correctly.

    Replaces the historical resolver-side carve-out (deleted 2026-05-07) —
    the protection is now structural (name-collision against runtime-created
    collections) rather than policy (which collections appear in default
    scope is operator-controlled via ``in_default``).
    """
    yaml_text = textwrap.dedent("""
        agents:
          - name: rogue
            collection: reference-library
            paths:
              - /data/workspaces/rogue
    """)
    registry = _load_registry_from_yaml(yaml_text)
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=registry)

    cols = resolver.resolve("rogue", Scope.AGENT)
    assert cols is not None
    assert "reference-library" not in cols
    # The agent gets the synthetic naming since the legacy override was dropped.
    assert cols == ["rogue-0"]
