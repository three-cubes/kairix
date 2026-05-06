"""Contract tests for the in_default collection flag.

Asserts the data-class predicates (`default_collection_names`,
`all_collection_names`) and the resolver's interaction with them respect
the operator's intent uniformly across every default scope, while
explicit `--collection X` lookups bypass the predicate entirely.

These tests are the regression boundary for the policy lift performed in
v2026.5.4 — the hardcoded `_RESERVED_COLLECTIONS = {"reference-library"}`
constant was deleted and replaced with operator-yaml-driven membership.
If a future change reintroduces a hardcoded reserve, these tests will not
catch it directly — see tests/contracts/test_no_reflib_resolver_hardcode.py
for the source-string regression guard.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import CollectionDef, CollectionsConfig
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope


def _config(*pairs: tuple[str, bool]) -> CollectionsConfig:
    return CollectionsConfig(
        shared=tuple(CollectionDef(name=n, path=n, in_default=flag) for n, flag in pairs),
    )


@pytest.mark.contract
class TestCollectionsConfigPredicates:
    def test_default_collection_names_filters_opt_in(self) -> None:
        cfg = _config(("home", True), ("archive", False), ("knowledge", True))
        assert cfg.default_collection_names() == ["home", "knowledge"]

    def test_all_collection_names_returns_every_entry(self) -> None:
        cfg = _config(("home", True), ("archive", False))
        assert cfg.all_collection_names() == ["home", "archive"]

    def test_empty_config_returns_empty_lists(self) -> None:
        cfg = CollectionsConfig(shared=())
        assert cfg.default_collection_names() == []
        assert cfg.all_collection_names() == []


@pytest.mark.contract
class TestResolverHonoursInDefault:
    def test_shared_scope_excludes_opt_in(self) -> None:
        cfg = _config(("home", True), ("archive", False))
        resolver = DefaultCollectionResolver(collections_config=cfg)
        assert resolver.resolve("alpha", Scope.SHARED) == ["home"]

    def test_shared_agent_scope_excludes_opt_in(self) -> None:
        cfg = _config(("home", True), ("archive", False))
        resolver = DefaultCollectionResolver(collections_config=cfg)
        assert resolver.resolve("alpha", Scope.SHARED_AGENT) == ["home", "alpha-memory"]

    def test_agent_scope_unaffected_by_in_default(self) -> None:
        """AGENT scope returns only agent collections — opt-in flag is irrelevant here."""
        cfg = _config(("home", True), ("archive", False))
        resolver = DefaultCollectionResolver(collections_config=cfg)
        assert resolver.resolve("alpha", Scope.AGENT) == ["alpha-memory"]

    def test_default_in_default_value_is_true(self) -> None:
        """A CollectionDef constructed without specifying in_default is in default scope.

        Backwards-compatibility guarantee: existing yamls that have not been
        edited still produce identical behaviour to pre-v2026.5.4.
        """
        cfg = CollectionsConfig(shared=(CollectionDef(name="legacy", path="legacy"),))
        resolver = DefaultCollectionResolver(collections_config=cfg)
        assert resolver.resolve(None, Scope.SHARED) == ["legacy"]
