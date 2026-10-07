"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FeatureFlagResolver`.

Two methods + canonical failure shapes:

  * ``get(name)`` — raises ``KeyError`` on unknown flag (typo-flags
    surface immediately instead of silently returning False).
  * ``iter_all()`` — returns an iterator that yields nothing for an
    empty registry (the ``returns_empty`` shape).

Every body runs over BOTH the production resolver — the
:mod:`kairix.core.features.resolver` ``flag`` / ``iter_status`` functions
composed into the Protocol surface, with their ``registry_reader`` /
``env_reader`` / ``overlay_reader`` DI seams pinned to an empty registry
and no overrides — and the canonical
:class:`tests.fakes.FakeFeatureFlagResolver` (F43 behavioural parity).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import pytest

from kairix.core.features.resolver import FlagStatus, flag, iter_status
from kairix.core.protocols import FeatureFlagResolver
from tests.fakes import FakeFeatureFlagResolver

pytestmark = pytest.mark.contract


class _ProductionResolver:
    """The production resolver functions behind the Protocol surface."""

    _SEAMS: ClassVar[dict[str, Any]] = {
        "registry_reader": dict,  # empty registry
        "env_reader": lambda _name: None,  # no env overrides
        "overlay_reader": dict,  # no config overlay
    }

    def get(self, name: str) -> bool:
        return flag(name, **self._SEAMS)

    def iter_all(self) -> Iterator[FlagStatus]:
        return iter_status(**self._SEAMS)


_IMPLEMENTATIONS: list[tuple[str, Callable[[], FeatureFlagResolver]]] = [
    ("real", _ProductionResolver),
    ("fake", FakeFeatureFlagResolver),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_get_raises_keyerror_when_flag_unknown(name: str, factory: Callable[[], FeatureFlagResolver]) -> None:
    """An unknown flag name raises KeyError — the contract pins "unknown
    flags MUST surface", not "silently return False" (which would mask
    typos forever).

    Sabotage proof (executed): in ``kairix.core.features.resolver.flag``
    replace ``raise _unknown_flag_error(...)`` with ``return False``.
    Re-run: the ``real`` case's ``pytest.raises`` sees nothing. Restored.
    """
    with pytest.raises(KeyError, match="unknown feature flag"):
        factory().get("never-registered-flag")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_iter_all_returns_empty_when_no_flags_declared(name: str, factory: Callable[[], FeatureFlagResolver]) -> None:
    """An empty registry yields nothing — callers iterate without a null
    check.

    Sabotage proof: in ``iter_status`` yield a sentinel FlagStatus when
    the registry is empty. Re-run: the ``real`` case fails. Restored.
    """
    assert list(factory().iter_all()) == [], name
