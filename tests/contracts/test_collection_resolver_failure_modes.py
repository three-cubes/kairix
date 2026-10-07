"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`CollectionResolver`.

Single Protocol method ``resolve(agent, scope)`` returning
``list[str] | None``. The Protocol docstring pins the failure
behaviour: returning ``None`` means "no filter — search everything",
returning ``[]`` is equivalent. Both are observable as
``returns_empty`` failure shapes (the scope produces no concrete
collections to filter on).

A separate ``raises`` probe covers the case where the resolver fails
mid-resolution (e.g. the scope-profile store is unreadable) — silent
fallback to ``None`` would silently widen scope to every collection.

Every body runs over BOTH the production
:class:`TopologyCollectionResolver` (its scope-profile lookup injected
through the ``scope_profile_resolver`` seam with
:class:`tests.fakes.FakeScopeProfileResolver`) and the canonical
:class:`tests.fakes.FakeCollectionResolver` (F43 behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator

import pytest

from kairix.core.protocols import CollectionResolver
from kairix.core.search.topology_resolver import TopologyCollectionResolver
from tests.fakes import FakeCollectionResolver, FakeScopeProfileResolver

pytestmark = pytest.mark.contract

_SCOPE = "shared+agent"

# A factory takes ``(connections, error)`` — the error the scope lookup
# raises (``None`` = healthy, with agent-alpha granted ``alpha-mem``).
ResolverFactory = Callable[[list[sqlite3.Connection], Exception | None], CollectionResolver]


def _real_resolver(connections: list[sqlite3.Connection], error: Exception | None) -> CollectionResolver:
    db = sqlite3.connect(":memory:")
    connections.append(db)
    profiles = FakeScopeProfileResolver().with_actor("agent-alpha", entries=[("alpha-mem", "read", "internal")])
    if error is not None:
        profiles = profiles.with_raises(error)
    return TopologyCollectionResolver(db=db, scope_profile_resolver=profiles)


def _fake_resolver(_connections: list[sqlite3.Connection], error: Exception | None) -> CollectionResolver:
    return FakeCollectionResolver(by_key={("agent-alpha", _SCOPE): ["alpha-mem"]}, raises=error)


_IMPLEMENTATIONS: list[tuple[str, ResolverFactory]] = [
    ("real", _real_resolver),
    ("fake", _fake_resolver),
]


@pytest.fixture
def connections() -> Iterator[list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    yield opened
    for db in opened:
        db.close()


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_resolve_returns_empty_when_scope_unknown(
    name: str, factory: ResolverFactory, connections: list[sqlite3.Connection]
) -> None:
    """A resolver with no mapping for the requested ``(agent, scope)``
    returns ``None`` — the documented "no filter" sentinel.

    Sabotage proof (executed): in :meth:`TopologyCollectionResolver.resolve`
    change the final ``return names or None`` to
    ``return names or ["leaked-collection"]``. Re-run: the ``real`` case
    fails because the resolver returns a list instead of ``None``.
    Restored.
    """
    resolver = factory(connections, None)
    assert resolver.resolve(agent="unknown", scope=_SCOPE) is None, name
    # Positive control: the granted agent resolves to its collection.
    assert resolver.resolve(agent="agent-alpha", scope=_SCOPE) == ["alpha-mem"], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_resolve_raises_when_underlying_implementation_fails(
    name: str, factory: ResolverFactory, connections: list[sqlite3.Connection]
) -> None:
    """A resolver whose lookup fails must surface the exception — silent
    fallback to ``None`` would widen scope to every collection (a
    security-relevant regression).

    Sabotage proof: in :meth:`TopologyCollectionResolver.resolve` wrap
    ``self._resolver.resolve(...)`` in ``try/except Exception: return None``.
    Re-run: the ``real`` case fails because no exception fires. Restored.
    """
    resolver = factory(connections, RuntimeError("F68-resolver-corrupt-cache"))
    with pytest.raises(RuntimeError, match="F68-resolver-corrupt-cache"):
        resolver.resolve(agent="agent-alpha", scope=_SCOPE)
