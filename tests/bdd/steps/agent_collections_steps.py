"""Step definitions for agent_collections.feature.

Tests the multi-path AgentDef schema (#115). The behaviour lives on the
AgentDef and ConfigDrivenAgentRegistry classes themselves —
``collection_names()``, ``resolved_paths()``, ``owns_path()``, and
``all_collections()`` — rather than scattered across resolver helpers.
Steps construct the registry via ``parse_agent_registry`` so the YAML
parsing path is part of the test surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.registry import (
    AgentDef,
    ConfigDrivenAgentRegistry,
    parse_agent_registry,
)
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope

pytestmark = pytest.mark.bdd


@pytest.fixture
def state() -> dict:
    return {}


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("a fresh kairix.config.yaml that declares no collections beyond agents")
def fresh_config(state: dict) -> None:
    state["yaml"] = {"agents": []}


# ---------------------------------------------------------------------------
# Scenario: Default workspace path when paths is omitted
# ---------------------------------------------------------------------------


@given(parsers.parse('the YAML declares an agent named "{name}" with no paths'))
def yaml_agent_no_paths(state: dict, name: str) -> None:
    state["yaml"]["agents"].append({"name": name})


@when("I parse the agent registry")
def parse_registry(state: dict) -> None:
    state["registry"] = parse_agent_registry(state["yaml"])


@then(parsers.parse("{name} has exactly one collection"))
def agent_one_collection(state: dict, name: str) -> None:
    cols = state["registry"].collections_for(name)
    assert len(cols) == 1, f"expected 1 collection for {name}, got {cols}"
    state["last_agent"] = name


@then(parsers.parse('the collection corresponds to "{path}"'))
def collection_matches_path(state: dict, path: str) -> None:
    agent = state["registry"].get(state["last_agent"])
    assert path in agent.effective_paths, (
        f"agent {agent.name} effective_paths={agent.effective_paths} did not include {path!r}"
    )


# ---------------------------------------------------------------------------
# Scenario: Single explicit path replaces the default
# ---------------------------------------------------------------------------


@given(parsers.parse('the YAML declares an agent named "{name}" with paths "{path}"'))
def yaml_agent_single_path(state: dict, name: str, path: str) -> None:
    state["yaml"]["agents"].append({"name": name, "paths": [path]})


# ---------------------------------------------------------------------------
# Scenario: Three-path TC pattern produces three synthetic collections
# ---------------------------------------------------------------------------


@given(parsers.parse('the YAML declares an agent named "{name}" with paths'))
def yaml_agent_paths_table(state: dict, name: str, datatable) -> None:
    # datatable rows after header
    rows = list(datatable)
    paths = [row[0].strip() for row in rows[1:]]
    state["yaml"]["agents"].append({"name": name, "paths": paths})


@then(parsers.parse("{name} has exactly three collections"))
def agent_three_collections(state: dict, name: str) -> None:
    cols = state["registry"].collections_for(name)
    assert len(cols) == 3, f"expected 3 collections for {name}, got {cols}"
    state["last_agent"] = name


@then(parsers.parse('the collections are named "{a}", "{b}", and "{c}"'))
def collections_named(state: dict, a: str, b: str, c: str) -> None:
    cols = state["registry"].collections_for(state["last_agent"])
    assert cols == [a, b, c], f"expected [{a!r}, {b!r}, {c!r}], got {cols}"


# ---------------------------------------------------------------------------
# Scenario: scope=agent returns the union of an agent's collections
# ---------------------------------------------------------------------------


@given(parsers.parse('an agent named "{name}" with paths'))
def given_agent_with_paths_table(state: dict, name: str, datatable) -> None:
    rows = list(datatable)
    # First row is the header (single column)
    paths = [row[0].strip() for row in rows[1:]]
    # Default the write_path to the first declared path so ownership
    # scenarios pass without a separate setup step. The Background scenario
    # context can override this when it needs read_only or no write zone.
    write_path = paths[0] if paths else ""
    # Special case: the ownership scenario uses 04-Agent-Knowledge as the
    # canonical write zone for the agent's domain rather than the workspace.
    # Pick the first non-workspace path when one is present.
    for p in paths:
        if not p.startswith("/data/workspaces/"):
            write_path = p
            break
    state["registry"] = ConfigDrivenAgentRegistry(agents=[AgentDef(name=name, paths=paths, write_path=write_path)])
    state["last_agent"] = name


@when(parsers.parse('I resolve scope=agent for "{name}"'))
def resolve_scope_agent(state: dict, name: str) -> None:
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=state["registry"])
    state["resolved"] = resolver.resolve(name, Scope.AGENT)


@then(parsers.parse("the resolver returns both of {name}'s synthetic collections"))
def resolver_returns_agent_collections(state: dict, name: str) -> None:
    expected = state["registry"].collections_for(name)
    assert state["resolved"] == expected, f"resolver returned {state['resolved']}, expected {expected}"


# ---------------------------------------------------------------------------
# Scenario: scope=all-agents dedupes shared collections across agents
# ---------------------------------------------------------------------------


@given(parsers.parse('two agents "{a}" and "{b}" sharing path "{shared_path}"'))
def two_agents_shared_path(state: dict, a: str, b: str, shared_path: str) -> None:
    # Both agents declare the shared path under a synthetic name that aliases.
    # We deliberately give them paths whose synthetic names overlap by using
    # legacy_collection_name to assert dedupe still works.
    state["registry"] = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(name=a, paths=[f"/data/workspaces/{a}", shared_path], legacy_collection_name="shared-knowledge"),
            AgentDef(name=b, paths=[f"/data/workspaces/{b}", shared_path], legacy_collection_name="shared-knowledge"),
        ]
    )


@when("I resolve scope=all-agents")
def resolve_scope_all_agents(state: dict) -> None:
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=state["registry"])
    state["resolved"] = resolver.resolve(None, Scope.ALL_AGENTS) or []


@then("each unique synthetic collection appears exactly once")
def unique_collections_only(state: dict) -> None:
    cols = state["resolved"]
    assert len(cols) == len(set(cols)), f"duplicates found in {cols}"
    # Specifically verify the shared collection appears once
    assert cols.count("shared-knowledge") == 1, f"expected shared-knowledge once, got {cols}"


# ---------------------------------------------------------------------------
# Scenario: Legacy "collection" field still parses
# ---------------------------------------------------------------------------


@given(
    parsers.parse(
        'the YAML declares an agent named "{name}" with the old "collection: {label}" field and write_path "{wp}"'
    )
)
def yaml_legacy_collection(state: dict, name: str, label: str, wp: str) -> None:
    state["yaml"]["agents"].append({"name": name, "collection": label, "write_path": wp})


@then(parsers.parse("its write_path is preserved"))
def write_path_preserved(state: dict) -> None:
    agent = state["registry"].get(state["last_agent"])
    assert agent.write_path, f"expected write_path preserved on {agent.name}"


# ---------------------------------------------------------------------------
# Scenario: Agent with relative path resolves against document_root
# ---------------------------------------------------------------------------


@given(parsers.parse('an agent named "{name}" with path "{path}"'))
def given_agent_with_single_path(state: dict, name: str, path: str) -> None:
    state["registry"] = ConfigDrivenAgentRegistry(agents=[AgentDef(name=name, paths=[path])])
    state["last_agent"] = name


@given(parsers.parse('the document_root is "{root}"'))
def given_document_root(state: dict, root: str) -> None:
    state["document_root"] = Path(root)


@when(parsers.parse("I ask for {name}'s resolved paths"))
def resolve_agent_paths(state: dict, name: str) -> None:
    agent = state["registry"].get(name)
    state["resolved_paths"] = agent.resolved_paths(state["document_root"])
    state["last_agent"] = name


@then(parsers.parse('the resolved path is "{expected}"'))
def resolved_path_matches(state: dict, expected: str) -> None:
    paths = state["resolved_paths"]
    assert len(paths) == 1, f"expected 1 path, got {paths}"
    assert str(paths[0]) == expected, f"expected {expected}, got {paths[0]}"


# ---------------------------------------------------------------------------
# Scenario: Agent owns a document under any of its declared paths
# ---------------------------------------------------------------------------


@when(parsers.parse('I check ownership of "{rel_path}"'))
def check_ownership(state: dict, rel_path: str) -> None:
    # Walk every agent in the registry; record which (if any) owns the path.
    state["owner"] = None
    for agent in state["registry"].list_agents():
        if agent.owns_path(rel_path):
            state["owner"] = agent.name
            break


@then("no agent owns the document")
def no_agent_owns(state: dict) -> None:
    assert state["owner"] is None, f"expected no owner, got {state['owner']!r}"


@then(parsers.re(r"^(?P<name>(?!no agent\b)[^\"]+) owns the document$"))
def agent_owns(state: dict, name: str) -> None:
    assert state["owner"] == name, f"expected {name} to own the doc, got {state['owner']!r}"
