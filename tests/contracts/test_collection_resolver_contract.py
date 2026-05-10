"""Contract tests for the CollectionResolver Protocol.

Verifies that DefaultCollectionResolver and FakeCollectionResolver both
satisfy the Protocol via isinstance(), and that callers can rely on the
declared surface.
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import CollectionResolver
from kairix.core.search.registry import ConfigDrivenAgentRegistry
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope
from tests.fakes import FakeCollectionResolver


@pytest.mark.contract
def test_default_resolver_satisfies_protocol() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    assert isinstance(resolver, CollectionResolver)


@pytest.mark.contract
def test_fake_resolver_satisfies_protocol() -> None:
    fake = FakeCollectionResolver()
    assert isinstance(fake, CollectionResolver)


@pytest.mark.contract
def test_resolve_returns_list_or_none_per_protocol() -> None:
    """The Protocol declares list[str] | None; both implementations honour it."""
    real = DefaultCollectionResolver(collections_config=None, extra_collections=["c1"])
    result = real.resolve("alpha", Scope.SHARED_AGENT)
    assert result is None or isinstance(result, list)

    fake = FakeCollectionResolver(by_key={(None, "shared"): None, ("alpha", "agent"): ["alpha-mem"]})
    assert fake.resolve(None, Scope.SHARED) is None
    assert fake.resolve("alpha", Scope.AGENT) == ["alpha-mem"]


# ---------------------------------------------------------------------------
# Loud-failure contract for scope=ALL_AGENTS / EVERYTHING when no agents are
# registered. Closes #164 — production yaml without an `agents:` section
# previously fell through to "search everything", silently returning content
# from the wrong collections.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_resolve_all_agents_raises_when_registry_is_none() -> None:
    """No registry at all → loud NotImplementedError, never silent fallback."""
    resolver = DefaultCollectionResolver(collections_config=None, agent_registry=None)
    with pytest.raises(NotImplementedError, match="agents:"):
        resolver.resolve(None, Scope.ALL_AGENTS)


@pytest.mark.contract
def test_resolve_all_agents_raises_when_registry_is_empty() -> None:
    """Registry present but empty (yaml has no agents: entries) → same loud
    failure as no registry. Without this guard, _all_agent_collections returns
    [] and the resolver coerces it to None, which downstream BM25 treats as
    'no filter — search everything'.
    """
    empty_registry = ConfigDrivenAgentRegistry(agents=[])
    resolver = DefaultCollectionResolver(
        collections_config=None,
        agent_registry=empty_registry,
    )
    with pytest.raises(NotImplementedError, match="at least one agent"):
        resolver.resolve(None, Scope.ALL_AGENTS)


@pytest.mark.contract
def test_resolve_everything_raises_when_registry_is_empty() -> None:
    """Same loud-failure contract for scope=EVERYTHING — sabotage check that
    the empty-registry guard fires for both scopes that consume the registry.
    """
    empty_registry = ConfigDrivenAgentRegistry(agents=[])
    resolver = DefaultCollectionResolver(
        collections_config=None,
        agent_registry=empty_registry,
    )
    with pytest.raises(NotImplementedError, match="at least one agent"):
        resolver.resolve(None, Scope.EVERYTHING)
