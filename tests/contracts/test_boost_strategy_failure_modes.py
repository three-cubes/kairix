"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`BoostStrategy`.

``BoostStrategy.boost`` re-ranks a result list. If the boost raises,
the caller (``SearchPipeline``) must surface the exception rather than
silently returning the unboosted list — silent fallback is the
behavioural anti-pattern this Protocol replaces.

Every body runs over BOTH a production strategy and the canonical
:class:`tests.fakes.FakeBoost` (F43 behavioural parity). The production
side is :class:`EntityFirstRoutingBoost`, whose live flag read is an
injected seam (``flag_reader``): a failing dependency on that seam is
the real-world analogue of ``FakeBoost(raises=...)``.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import BoostStrategy
from kairix.core.search.boosts import EntityFirstRoutingBoost
from kairix.core.search.intent import QueryIntent
from tests.fakes import FakeBoost

pytestmark = pytest.mark.contract

# A factory takes the exception the strategy's dependency should raise
# (``None`` = healthy) and returns the strategy under test.
BoostFactory = Callable[[BaseException | None], BoostStrategy]


def _real_boost(raises: BaseException | None) -> BoostStrategy:
    """Production strategy with its flag-reader seam either healthy (flag
    ON) or raising the configured error."""

    def _reader() -> bool:
        if raises is not None:
            raise raises
        return True

    return EntityFirstRoutingBoost(flag_reader=_reader)


def _fake_boost(raises: BaseException | None) -> BoostStrategy:
    return FakeBoost(raises=raises)


_IMPLEMENTATIONS: list[tuple[str, BoostFactory]] = [
    ("real", _real_boost),
    ("fake", _fake_boost),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_boost_raises_propagates_typed_exception(name: str, factory: BoostFactory) -> None:
    """A boost whose dependency raises must surface the exception type
    and message verbatim — no silent fallback to the unboosted list.

    Sabotage proof (executed): in ``EntityFirstRoutingBoost.boost`` stop
    consulting ``self._flag_reader()`` (swallow the dependency). Re-run:
    the ``real`` case fails because no exception fires. Restored.
    """
    boost = factory(RuntimeError("F68-boost-raises"))
    with pytest.raises(RuntimeError, match="F68-boost-raises"):
        boost.boost([{"path": "a.md"}], "alpha", {"intent": QueryIntent.ENTITY})


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_boost_returns_empty_when_results_empty(name: str, factory: BoostFactory) -> None:
    """An empty input list MUST round-trip as empty — the boost must
    not invent entries. This is the ``returns_empty`` failure class
    (no candidates to boost, observable as empty output). The context
    passes every gate (flag ON, ENTITY intent) so the real strategy
    actually runs its boost path.

    Sabotage proof: in ``EntityFirstRoutingBoost.boost`` change the final
    ``return sorted(...)`` to ``return [object()]``. Re-run: the ``real``
    case fails because the result has one entry instead of zero. Restored.
    """
    boost = factory(None)
    out = boost.boost([], "anything", {"intent": QueryIntent.ENTITY})
    assert out == [], f"{name}: empty input must yield empty output; got {out!r}"
