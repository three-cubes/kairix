"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ProviderRegistry`.

``ProviderRegistry.resolve(name)`` maps the operator's configured provider
name to a plugin; ``available()`` lists what is installed. An operator who
typos ``provider:`` or runs without any plugin installed must get the typed
:class:`~kairix.providers.ProviderNotRegistered` from
:func:`kairix.providers.get_provider` — carrying the requested name, the
installed list, and an actionable ``fix:`` line — never a bare
``KeyError`` / ``None``.

One body per method, two implementations (F43 limb 2):

* the real :class:`EntryPointRegistry` whose ``entry_points`` seam reports
  no installed ``kairix.providers`` entry points (a broken / partial
  install);
* the canonical :class:`tests.fakes.FakeProviderRegistry` with an empty
  mapping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from kairix.providers import EntryPointRegistry, ProviderNotRegistered, get_provider
from tests.fakes import FakeProviderRegistry

pytestmark = pytest.mark.contract


def _no_entry_points(**_selectors: Any) -> Iterable[Any]:
    """Stand-in for ``importlib.metadata.entry_points`` on an install with no plugins."""
    return []


_FACTORIES: list[Callable[[], Any]] = [
    lambda: EntryPointRegistry(entry_points=_no_entry_points),
    lambda: FakeProviderRegistry({}),
]
_IDS = ["real-entry-points", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_resolve_raises_provider_not_registered_naming_the_request(factory: Callable[[], Any]) -> None:
    """An unknown provider name raises ``ProviderNotRegistered`` through
    ``get_provider`` carrying the requested name and an empty installed list.

    Sabotage proof: in ``EntryPointRegistry.resolve`` replace the
    ``raise ProviderNotRegistered(...)`` with ``return None``. Re-run: the
    real case fails because ``get_provider`` returns ``None`` instead of
    raising. Restored.
    """
    registry = factory()

    with pytest.raises(ProviderNotRegistered, match="'azure_foundy'") as excinfo:
        get_provider("azure_foundy", registry=registry)

    assert excinfo.value.name == "azure_foundy"
    assert excinfo.value.available == []


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_available_returns_empty_when_no_plugins_installed_error_says_install(
    factory: Callable[[], Any],
) -> None:
    """With nothing installed ``available()`` is ``[]`` and the resolve error
    switches to the "no providers installed — pip install" remediation.

    Sabotage proof: in ``EntryPointRegistry.available`` return
    ``["openai"]`` unconditionally. Re-run: the real case fails on both the
    ``available() == []`` assertion and the "No providers are currently
    installed" message match. Restored.
    """
    registry = factory()

    assert registry.available() == []
    with pytest.raises(ProviderNotRegistered, match="No providers are currently installed") as excinfo:
        registry.resolve("openai")
    assert "fix: pip install" in str(excinfo.value)
