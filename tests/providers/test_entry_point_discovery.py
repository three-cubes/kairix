"""Entry-point discovery tests for ``EntryPointRegistry``.

Covers the two-state contract from the ADR:

  - Happy path: a configured provider name resolves via
    ``importlib.metadata.entry_points(group="kairix.providers", name=name)``
    and the factory's return value is returned to the caller.
  - Failure path: an unknown name raises ``ProviderNotRegistered`` with
    a populated ``available`` list so the CLI can render a "did you
    mean..." message.

Test design: the production ``EntryPointRegistry`` accepts an
``entry_points`` callable parameter (defaults to the stdlib
``importlib.metadata.entry_points``). Tests pass a small fake callable
that returns deterministic ``_FakeEntryPoint`` instances. This avoids
needing ``pip install -e .`` in the worktree just to make the stdlib
mechanism find kairix's own entries.

Constructor injection rather than ``@patch`` keeps the test inside
F1 (no internal patches) and F6 (no ``*_fn=None`` test-only kwargs —
``entry_points`` is a real production seam, not a test-only kwarg).

The first-party stub factories themselves raise NotImplementedError;
the happy-path test uses a tiny fake factory to keep this file focused
on *discovery*. The "stubs raise NotImplementedError" check lives in
``test_protocol_compliance.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.providers import (
    ENTRY_POINT_GROUP,
    EntryPointRegistry,
    ProviderNotRegistered,
)
from tests.fakes import FakeProvider


@dataclass
class _FakeEntryPoint:
    """Stand-in for ``importlib.metadata.EntryPoint``.

    Only the two attributes ``EntryPointRegistry`` actually uses are
    modelled: ``.name`` and ``.load()``. Keeping this in the test file
    rather than ``tests/fakes.py`` because it's an entry-point-shaped
    transport detail, not a domain fake.
    """

    name: str
    target: Callable[[], Any]

    def load(self) -> Callable[[], Any]:
        return self.target


def _make_entry_points_callable(
    entries: dict[str, Callable[[], Any]],
) -> Callable[..., list[_FakeEntryPoint]]:
    """Return a callable matching ``importlib.metadata.entry_points``.

    Filters by ``group=`` (only the canonical kairix.providers group
    yields anything) and optionally by ``name=`` for the happy-path
    lookup. Mirrors the actual stdlib signature subset
    ``EntryPointRegistry`` calls.
    """

    def entry_points(*, group: str, name: str | None = None) -> list[_FakeEntryPoint]:
        if group != ENTRY_POINT_GROUP:
            return []
        all_eps = [_FakeEntryPoint(n, t) for n, t in entries.items()]
        if name is None:
            return all_eps
        return [ep for ep in all_eps if ep.name == name]

    return entry_points


@pytest.mark.unit
class TestEntryPointDiscoveryHappyPath:
    """A configured provider name resolves via entry_points()."""

    @pytest.mark.unit
    def test_resolve_returns_factory_output(self) -> None:
        fake_provider = FakeProvider(name="openai")

        def factory() -> FakeProvider:
            return fake_provider

        eps = _make_entry_points_callable({"openai": factory})
        registry = EntryPointRegistry(entry_points=eps)

        assert registry.resolve("openai") is fake_provider

    @pytest.mark.unit
    def test_resolve_uses_name_filtered_lookup(self) -> None:
        # Sabotage-proof: insertion order puts "openai" first; if
        # EntryPointRegistry.resolve drops the ``name=`` filter, the
        # unfiltered ``entry_points(group=...)`` call returns both
        # entries and ``eps[0].load()`` would construct openai_factory
        # when the operator asked for bedrock — caught by the
        # ``provider.name == "bedrock"`` assertion below.
        openai_calls = {"count": 0}
        bedrock_calls = {"count": 0}

        def openai_factory() -> FakeProvider:
            openai_calls["count"] += 1
            return FakeProvider(name="openai")

        def bedrock_factory() -> FakeProvider:
            bedrock_calls["count"] += 1
            return FakeProvider(name="bedrock")

        # Insertion order: openai first, bedrock second. So unfiltered
        # ``eps[0]`` would be openai — but the operator asked for bedrock.
        eps = _make_entry_points_callable({"openai": openai_factory, "bedrock": bedrock_factory})
        registry = EntryPointRegistry(entry_points=eps)

        provider = registry.resolve("bedrock")

        assert provider.name == "bedrock"
        # Only the bedrock factory ran — name-filtering avoided eager-loading
        # every installed plugin.
        assert bedrock_calls["count"] == 1
        assert openai_calls["count"] == 0


@pytest.mark.unit
class TestEntryPointDiscoveryFailurePath:
    """Unknown names raise ProviderNotRegistered with available=[...]."""

    @pytest.mark.unit
    def test_unknown_name_raises_with_available_populated(self) -> None:
        def openai_factory() -> FakeProvider:
            return FakeProvider(name="openai")

        def bedrock_factory() -> FakeProvider:
            return FakeProvider(name="bedrock")

        eps = _make_entry_points_callable({"openai": openai_factory, "bedrock": bedrock_factory})
        registry = EntryPointRegistry(entry_points=eps)

        with pytest.raises(ProviderNotRegistered) as exc_info:
            registry.resolve("typo_provider")

        err = exc_info.value
        assert err.name == "typo_provider"
        # The available list is sorted and contains every installed plugin —
        # populates the "did you mean..." surface in the CLI error.
        assert err.available == ["bedrock", "openai"]
        # And the error message itself enumerates the installed names so
        # operators see them in the raw traceback too.
        assert "bedrock" in str(err)
        assert "openai" in str(err)

    @pytest.mark.unit
    def test_unknown_name_with_no_plugins_installed_message(self) -> None:
        # When no plugins exist at all (e.g. kairix shipped without
        # entry-points registered in the wheel), the error guides the
        # operator to ``pip install -e .`` rather than leaving them
        # staring at an empty list.
        eps = _make_entry_points_callable({})
        registry = EntryPointRegistry(entry_points=eps)

        with pytest.raises(ProviderNotRegistered) as exc_info:
            registry.resolve("openai")

        err = exc_info.value
        assert err.name == "openai"
        assert err.available == []
        assert "pip install" in str(err).lower()


@pytest.mark.unit
class TestEntryPointRegistryAvailable:
    """available() returns the sorted list of installed plugin names."""

    @pytest.mark.unit
    def test_available_returns_sorted_names(self) -> None:
        eps = _make_entry_points_callable(
            {
                "openai": lambda: FakeProvider(name="openai"),
                "bedrock": lambda: FakeProvider(name="bedrock"),
                "anthropic": lambda: FakeProvider(name="anthropic"),
            }
        )
        registry = EntryPointRegistry(entry_points=eps)

        # Sorted, not insertion-order — operators reading the available
        # list scan alphabetically.
        assert registry.available() == ["anthropic", "bedrock", "openai"]

    @pytest.mark.unit
    def test_available_is_empty_when_no_plugins(self) -> None:
        eps = _make_entry_points_callable({})
        registry = EntryPointRegistry(entry_points=eps)
        assert registry.available() == []
