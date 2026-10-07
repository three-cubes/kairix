"""F68 failure-mode contract for the ``SocketCounter`` Protocol.

``TimeoutBudget`` brackets every dispatch with ``counter.open()`` /
``counter.close()``. The operator-visible invariant is the ledger: no
matter how the call ends — a timeout, or the call itself raising — the
counter must read balanced (``opened == closed``) so a burst of failures
never looks like leaked FDs.

F43 parity: ONE body per method runs over both counter implementations
— the ``FakeProvider`` (whose counters a provider doubles as) and the
standalone ``FakeSocketCounter``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from kairix.providers import TimeoutExceeded
from kairix.transport.timeout.budget import SocketCounter, TimeoutBudget
from tests.fakes import FakeProvider, FakeSocketCounter

pytestmark = pytest.mark.contract

_COUNTERS: list[tuple[str, Callable[[], Any]]] = [
    ("provider", FakeProvider),
    ("standalone", FakeSocketCounter),
]


@pytest.mark.parametrize("name,factory", _COUNTERS)
def test_open_times_out_counter_stays_balanced(name: str, factory: Callable[[], SocketCounter]) -> None:
    """``times_out``: a call that overruns its budget raises the typed
    ``TimeoutExceeded`` carrying the budget, and the socket the policy
    opened is closed before the error reaches the caller.

    Sabotage proof (executed): move ``self._counter.close()`` out of the
    ``finally`` in ``TimeoutBudget.with_timeout`` into the success path
    → both cases fail (``closed == 0`` after the timeout). Restored.
    """
    counter = factory()
    release = threading.Event()
    budget = TimeoutBudget(1, counter=counter)
    try:
        with pytest.raises(TimeoutExceeded) as excinfo:
            budget.with_timeout(lambda: release.wait(5))
    finally:
        release.set()
        budget.shutdown()
    assert excinfo.value.budget_ms == 1, name
    assert (counter.opened, counter.closed, counter.peak_open) == (1, 1, 1)


@pytest.mark.parametrize("name,factory", _COUNTERS)
def test_close_raises_wrapped_call_error_still_balances_counter(
    name: str, factory: Callable[[], SocketCounter]
) -> None:
    """``raises``: when the wrapped call raises, the error propagates
    unchanged AND ``close()`` still fires — the ledger stays balanced.

    Sabotage proof (executed): replace the ``finally:`` around
    ``self._counter.close()`` with an ``else:`` → both cases fail
    (``closed == 0``). Restored.
    """
    counter = factory()
    budget = TimeoutBudget(1000, counter=counter)

    def _upstream_rejects() -> None:
        raise ValueError("upstream rejected the request")

    try:
        with pytest.raises(ValueError, match="upstream rejected the request"):
            budget.with_timeout(_upstream_rejects)
    finally:
        budget.shutdown()
    assert (counter.opened, counter.closed) == (1, 1), name
