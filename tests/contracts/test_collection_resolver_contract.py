"""Contract tests for the CollectionResolver Protocol.

Verifies that DefaultCollectionResolver and FakeCollectionResolver both
satisfy the Protocol via isinstance(), and that callers can rely on the
declared surface.
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import CollectionResolver
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
