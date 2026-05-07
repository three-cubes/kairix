"""Integration tests for DefaultCollectionResolver + AgentRegistry — closes the
NotImplementedError gates on Scope.ALL_AGENTS and Scope.EVERYTHING (KFEAT-GAP-8).
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import CollectionDef, CollectionsConfig
from kairix.core.search.registry import AgentDef, ConfigDrivenAgentRegistry
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope


def _registry_with(*names: str) -> ConfigDrivenAgentRegistry:
    return ConfigDrivenAgentRegistry(
        agents=[AgentDef(name=n, legacy_collection_name=f"{n}-memory", write_path=f"agents/{n}") for n in names]
    )


def _config(*shared: str) -> CollectionsConfig:
    return CollectionsConfig(
        shared=tuple(CollectionDef(name=s, path=s, glob="*.md") for s in shared),
        agent_pattern="{agent}-memory",
        agent_paths={},
    )


@pytest.mark.unit
def test_all_agents_returns_every_agent_collection_no_shared() -> None:
    resolver = DefaultCollectionResolver(
        collections_config=_config("docs", "research"),
        agent_registry=_registry_with("alpha", "beta", "gamma"),
    )
    cols = resolver.resolve(None, Scope.ALL_AGENTS)
    assert cols == ["alpha-memory", "beta-memory", "gamma-memory"]
    assert "docs" not in (cols or [])
    assert "research" not in (cols or [])


@pytest.mark.unit
def test_everything_combines_shared_and_all_agents() -> None:
    resolver = DefaultCollectionResolver(
        collections_config=_config("docs", "research"),
        agent_registry=_registry_with("alpha", "beta"),
    )
    cols = resolver.resolve(None, Scope.EVERYTHING)
    assert cols == ["docs", "research", "alpha-memory", "beta-memory"]


@pytest.mark.unit
def test_all_agents_without_registry_still_raises() -> None:
    """The NotImplementedError survives when the operator forgets to wire a registry."""
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(NotImplementedError, match="AgentRegistry"):
        resolver.resolve(None, Scope.ALL_AGENTS)


@pytest.mark.unit
def test_empty_registry_returns_none_for_all_agents() -> None:
    """A registry with no agents is a valid configuration; resolve returns None."""
    resolver = DefaultCollectionResolver(
        collections_config=None,
        agent_registry=ConfigDrivenAgentRegistry(),
    )
    assert resolver.resolve(None, Scope.ALL_AGENTS) is None


@pytest.mark.unit
def test_existing_scopes_unaffected_by_registry_presence() -> None:
    """Adding a registry must not change SHARED / AGENT / SHARED_AGENT semantics."""
    resolver = DefaultCollectionResolver(
        collections_config=_config("docs"),
        agent_registry=_registry_with("alpha", "beta"),
    )
    assert resolver.resolve(None, Scope.SHARED) == ["docs"]
    assert resolver.resolve("alpha", Scope.AGENT) == ["alpha-memory"]
    assert resolver.resolve("alpha", Scope.SHARED_AGENT) == ["docs", "alpha-memory"]
