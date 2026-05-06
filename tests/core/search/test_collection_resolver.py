"""Unit tests for DefaultCollectionResolver — exhaustive scope semantics.

Tests every Scope value against (a) configured registry, (b) bare
agent_pattern only, (c) no config, where applicable. ALL_AGENTS and
EVERYTHING are explicit NotImplementedError until WS3-3 (AgentRegistry)
lands. No @patch, no monkeypatch, no private symbol imports.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import CollectionDef, CollectionsConfig
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope


def _config_with_shared(*names: str, pattern: str = "{agent}-memory") -> CollectionsConfig:
    return CollectionsConfig(
        shared=tuple(CollectionDef(name=n, path=n, glob="*.md") for n in names),
        agent_pattern=pattern,
        agent_paths={},
    )


def _config_with_flags(*name_default_pairs: tuple[str, bool], pattern: str = "{agent}-memory") -> CollectionsConfig:
    """Build a CollectionsConfig where each entry declares its in_default flag."""
    return CollectionsConfig(
        shared=tuple(
            CollectionDef(name=n, path=n, glob="*.md", in_default=in_default) for n, in_default in name_default_pairs
        ),
        agent_pattern=pattern,
        agent_paths={},
    )


@pytest.mark.unit
def test_no_config_no_agent_no_scope_returns_none() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    assert resolver.resolve(None, Scope.SHARED) is None


@pytest.mark.unit
def test_shared_scope_returns_only_shared_collections() -> None:
    config = _config_with_shared("docs", "research")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED)
    assert cols == ["docs", "research"]
    assert "alpha-memory" not in (cols or [])


@pytest.mark.unit
def test_agent_scope_returns_only_agent_collection() -> None:
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.AGENT)
    assert cols == ["alpha-memory"]


@pytest.mark.unit
def test_agent_scope_without_agent_returns_none() -> None:
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(collections_config=config)
    assert resolver.resolve(None, Scope.AGENT) is None


@pytest.mark.unit
def test_shared_agent_scope_combines_both() -> None:
    config = _config_with_shared("docs", "research")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED_AGENT)
    assert cols == ["docs", "research", "alpha-memory"]


@pytest.mark.unit
def test_shared_agent_scope_without_agent_omits_agent_collection() -> None:
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve(None, Scope.SHARED_AGENT)
    assert cols == ["docs"]


@pytest.mark.unit
def test_extra_collections_appended_to_shared() -> None:
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        extra_collections=["operator-extra"],
    )
    cols = resolver.resolve("alpha", Scope.SHARED_AGENT)
    assert cols == ["docs", "operator-extra", "alpha-memory"]


@pytest.mark.unit
def test_custom_agent_pattern_honoured() -> None:
    config = _config_with_shared("docs", pattern="{agent}-zone")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.AGENT)
    assert cols == ["alpha-zone"]


@pytest.mark.unit
def test_default_pattern_when_no_config() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    cols = resolver.resolve("alpha", Scope.AGENT)
    assert cols == ["alpha-memory"]


@pytest.mark.unit
def test_all_agents_scope_without_registry_raises() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(NotImplementedError, match="AgentRegistry"):
        resolver.resolve("alpha", Scope.ALL_AGENTS)


@pytest.mark.unit
def test_everything_scope_without_registry_raises() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(NotImplementedError, match="AgentRegistry"):
        resolver.resolve("alpha", Scope.EVERYTHING)


@pytest.mark.unit
def test_string_scope_is_coerced_to_enum() -> None:
    """During the migration period, callers may still pass plain strings."""
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", "shared+agent")
    assert cols == ["docs", "alpha-memory"]


@pytest.mark.unit
def test_unknown_scope_string_raises_value_error() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(ValueError, match="unknown scope"):
        resolver.resolve("alpha", "not-a-scope")


# ---------------------------------------------------------------------------
# in_default flag — opt-in collections are excluded from default scopes but
# remain reachable via explicit --collection lookups. Replaces the historical
# hardcoded reference-library carve-out (deleted 2026-05-07): policy now
# lives in the operator's yaml, not in resolver source.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_in_default_false_excluded_from_shared_scope() -> None:
    config = _config_with_flags(("docs", True), ("archive", False), ("research", True))
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED)
    assert cols == ["docs", "research"]
    assert "archive" not in (cols or [])


@pytest.mark.unit
def test_in_default_false_excluded_from_shared_agent_scope() -> None:
    config = _config_with_flags(("docs", True), ("archive", False))
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED_AGENT)
    assert cols == ["docs", "alpha-memory"]
    assert "archive" not in (cols or [])


@pytest.mark.unit
def test_in_default_field_defaults_to_true_when_omitted() -> None:
    """A CollectionDef constructed without ``in_default=`` keeps the historical behaviour."""
    config = _config_with_shared("docs", "research")  # no flag → defaults True
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED)
    assert cols == ["docs", "research"]


@pytest.mark.unit
def test_extra_collections_always_in_default_scope() -> None:
    """Operator extras (KAIRIX_EXTRA_COLLECTIONS) are treated as in_default=True.

    No flag plumbing today — extras are operator-supplied at the boundary and
    deliberately additive. Phase 2 (composable named scopes) is where richer
    extras semantics will land if needed.
    """
    config = _config_with_shared("docs")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        extra_collections=["operator-extra"],
    )
    cols = resolver.resolve("alpha", Scope.SHARED_AGENT)
    assert cols == ["docs", "operator-extra", "alpha-memory"]


@pytest.mark.unit
def test_in_default_false_excluded_from_everything_scope() -> None:
    """Scope.EVERYTHING respects in_default — opt-in collections never auto-join.

    Operators reach opt-in collections only by passing
    ``collections=["archive"]`` (or whatever the name is) explicitly to the
    search pipeline. This is the same contract Scope.AGENT already had: the
    resolver builds default-scope membership; explicit collection arguments
    bypass the resolver entirely.
    """

    class _StubRegistry:
        @staticmethod
        def list_agents():
            from types import SimpleNamespace

            return [SimpleNamespace(collection="alpha-memory")]

    config = _config_with_flags(("docs", True), ("archive", False))
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=_StubRegistry(),
    )
    cols = resolver.resolve("alpha", Scope.EVERYTHING)
    assert "archive" not in (cols or [])
    assert "docs" in (cols or [])
    assert "alpha-memory" in (cols or [])
