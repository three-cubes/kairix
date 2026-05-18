"""Contract probes for ``kairix.core.search.resolver``.

Each test pins one docstring/source-claim of ``DefaultCollectionResolver``,
``Scope.parse`` (as called from the resolver's public surface), and
``_dedupe_preserving_order`` (probed via the public ``resolve()`` entry
point — never imported directly).

These complement the existing unit tests in
``tests/core/search/test_collection_resolver.py`` and
``tests/core/search/test_resolver_with_registry.py`` by:

  * tagging the documented contracts with ``@pytest.mark.contract`` so the
    fast contract suite catches regressions on every push, and
  * asserting claims that are easy to drift on (dedup order under overlap,
    operator-actionable error message wording, scope-routing isolation).

Driven through canonical fakes from ``tests/fakes`` only — no monkeypatch,
no ``_Stub``/``_Fake`` inline classes, no private-symbol imports from
``kairix.core.search.resolver``.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import CollectionDef, CollectionsConfig
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope
from tests.fakes import FakeAgentRegistry

# ---------------------------------------------------------------------------
# Helpers — small constructors to keep individual tests focused on the claim
# under probe rather than CollectionsConfig boilerplate.
# ---------------------------------------------------------------------------


def _config(*shared_names: str, pattern: str = "{agent}-memory") -> CollectionsConfig:
    return CollectionsConfig(
        shared=tuple(CollectionDef(name=n, path=n, glob="*.md") for n in shared_names),
        agent_pattern=pattern,
        agent_paths={},
    )


# ---------------------------------------------------------------------------
# Scope.parse — every documented value round-trips, unknowns reject.
#
# The resolver's public ``resolve()`` is the boundary that converts the
# string-typed scope into a ``Scope`` member; these probes pin that
# coercion so callers can keep passing the documented string spellings.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.parametrize(
    ("scope_str", "expected_member"),
    [
        ("shared", Scope.SHARED),
        ("agent", Scope.AGENT),
        ("shared+agent", Scope.SHARED_AGENT),
        ("all-agents", Scope.ALL_AGENTS),
        ("everything", Scope.EVERYTHING),
    ],
)
def test_scope_parse_accepts_every_canonical_string(scope_str: str, expected_member: Scope) -> None:
    """``Scope.parse`` returns the matching member for each documented value."""
    assert Scope.parse(scope_str) is expected_member


@pytest.mark.contract
@pytest.mark.parametrize("bad", ["", "SHARED", "Shared", "shared ", " shared", "all_agents", "agent+shared", "all"])
def test_scope_parse_rejects_unknown_strings_through_resolver(bad: str) -> None:
    """The resolver raises ``ValueError`` from ``Scope.parse`` on unknown scopes.

    Probes the contract through the public surface — callers never call
    ``Scope.parse`` directly inside production hot paths; the resolver does.
    """
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(ValueError, match="unknown scope"):
        resolver.resolve("alpha", bad)


# ---------------------------------------------------------------------------
# Scope routing — each Scope returns the documented collection set.
#
# Source of truth: the docstring on ``DefaultCollectionResolver``:
#
#   SHARED        — only the default-eligible shared collections
#   AGENT         — only the agent's own collections
#   SHARED_AGENT  — default-eligible shared plus the agent's collections
#   ALL_AGENTS    — every agent's collections (no shared)
#   EVERYTHING    — default-eligible shared + every agent's collections
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_shared_scope_returns_only_shared_no_agent_leak() -> None:
    config = _config("docs", "research")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=FakeAgentRegistry([{"name": "alpha"}]),
    )
    cols = resolver.resolve("alpha", Scope.SHARED)
    assert cols == ["docs", "research"]
    assert all("alpha" not in c for c in cols or [])


@pytest.mark.contract
def test_agent_scope_returns_only_agent_no_shared_leak() -> None:
    config = _config("docs", "research")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.AGENT)
    assert cols == ["alpha-memory"]
    assert "docs" not in (cols or [])
    assert "research" not in (cols or [])


@pytest.mark.contract
def test_shared_agent_scope_combines_shared_then_agent() -> None:
    """Order matters: shared first, agent appended — so RRF/budget callers
    see a stable, documented ordering."""
    config = _config("docs", "research")
    resolver = DefaultCollectionResolver(collections_config=config)
    cols = resolver.resolve("alpha", Scope.SHARED_AGENT)
    assert cols == ["docs", "research", "alpha-memory"]


@pytest.mark.contract
def test_all_agents_scope_returns_every_agent_no_shared() -> None:
    config = _config("docs", "research")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=FakeAgentRegistry([{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]),
    )
    cols = resolver.resolve(None, Scope.ALL_AGENTS)
    assert cols == ["alpha-memory", "beta-memory", "gamma-memory"]
    # No shared bleed — the docstring is explicit.
    assert "docs" not in (cols or [])
    assert "research" not in (cols or [])


@pytest.mark.contract
def test_everything_scope_returns_shared_then_every_agent() -> None:
    config = _config("docs", "research")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=FakeAgentRegistry([{"name": "alpha"}, {"name": "beta"}]),
    )
    cols = resolver.resolve(None, Scope.EVERYTHING)
    assert cols == ["docs", "research", "alpha-memory", "beta-memory"]


# ---------------------------------------------------------------------------
# _dedupe_preserving_order — probed through Scope.EVERYTHING.
#
# The helper is private; the contract it secures is the public claim that
# ``EVERYTHING`` "never duplicates" cross-agent shared paths and that the
# first-seen order is preserved. We construct a registry whose synthetic
# collection names overlap with the shared list, then assert both the
# dedup property and the original ordering of first occurrences.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_everything_dedupes_overlap_and_preserves_first_seen_order() -> None:
    """If a shared collection name and an agent collection name collide,
    the shared (earlier) one wins position; the duplicate is dropped.

    The dedup happens at line 93 in resolver.py via
    ``_dedupe_preserving_order``; we probe it here without importing the
    helper, by engineering a name collision via the agent_pattern.
    """
    # agent_pattern picks the *bare* name, so an agent named "docs" produces
    # a "docs" collection that collides with the shared "docs".
    config = _config("docs", "research", pattern="{agent}")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=FakeAgentRegistry(
            [
                {"name": "docs", "collection": "docs"},  # collides with shared[0]
                {"name": "alpha", "collection": "alpha"},
                {"name": "research", "collection": "research"},  # collides with shared[1]
            ]
        ),
    )
    cols = resolver.resolve(None, Scope.EVERYTHING)
    # First-seen order: shared "docs", shared "research", then agent "alpha".
    # The agent collisions on "docs" and "research" are dropped in-place.
    assert cols == ["docs", "research", "alpha"]
    # Property: every element appears exactly once.
    assert len(cols or []) == len(set(cols or []))


@pytest.mark.contract
def test_everything_preserves_agent_registration_order_when_no_collision() -> None:
    """No-collision case: the agent ordering from the registry survives end-to-end."""
    config = _config("docs")
    resolver = DefaultCollectionResolver(
        collections_config=config,
        agent_registry=FakeAgentRegistry(
            [
                {"name": "zulu"},  # alphabetically last but registered first
                {"name": "alpha"},
                {"name": "mike"},
            ]
        ),
    )
    cols = resolver.resolve(None, Scope.EVERYTHING)
    # Registration order must survive — alphabetical sort would break this.
    assert cols == ["docs", "zulu-memory", "alpha-memory", "mike-memory"]


# ---------------------------------------------------------------------------
# Missing-registry → NotImplementedError with operator-actionable message.
#
# The docstring on ``_all_agent_collections`` explicitly promises the error
# message tells the operator (a) to add an `agents:` section to
# ``kairix.config.yaml`` OR (b) pass ``agent_registry=`` to the factory.
# Drift on either of these phrases breaks the operator UX contract.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.parametrize("scope", [Scope.ALL_AGENTS, Scope.EVERYTHING])
def test_missing_registry_raises_not_implemented_with_actionable_message(scope: Scope) -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(NotImplementedError) as excinfo:
        resolver.resolve("alpha", scope)
    msg = str(excinfo.value)
    # Names what's missing.
    assert "AgentRegistry" in msg
    # Tells the operator how to fix it via config (G4 boundary).
    assert "kairix.config.yaml" in msg
    assert "agents:" in msg
    # And the alternative wiring path via the factory.
    assert "agent_registry=" in msg


# ---------------------------------------------------------------------------
# Empty-result → None, per the trailing ``return cols or None`` contract.
#
# Callers downstream (SearchPipeline, prep) treat ``None`` as "no collection
# filter — search the whole index" vs ``[]`` as "search nothing". The
# docstring of ``DefaultCollectionResolver`` doesn't spell this out but the
# ``return cols or None`` line in resolve() is the contract; flipping it to
# ``return cols`` (i.e. returning ``[]``) is a subtle but breaking change.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_resolve_returns_none_when_no_collections_resolved() -> None:
    resolver = DefaultCollectionResolver(collections_config=None)
    # Scope.SHARED with no config and no extras → empty → None.
    assert resolver.resolve(None, Scope.SHARED) is None


@pytest.mark.contract
def test_resolve_returns_none_for_agent_scope_with_no_agent_and_no_extras() -> None:
    """Scope.AGENT with ``agent=None`` resolves to nothing → None, not []."""
    config = _config("docs")
    resolver = DefaultCollectionResolver(collections_config=config)
    assert resolver.resolve(None, Scope.AGENT) is None


@pytest.mark.contract
def test_resolve_raises_when_registry_empty_for_all_agents() -> None:
    """An empty AgentRegistry triggers the same loud-failure path as no
    registry: NotImplementedError naming the missing config. Without this,
    the resolver returned ``[]`` → was coerced to ``None`` → downstream
    BM25 treated ``None`` as "no filter" and silently returned global
    results (#164).
    """
    resolver = DefaultCollectionResolver(
        collections_config=None,
        agent_registry=FakeAgentRegistry(agents=[]),
    )
    with pytest.raises(NotImplementedError, match="at least one agent"):
        resolver.resolve(None, Scope.ALL_AGENTS)
