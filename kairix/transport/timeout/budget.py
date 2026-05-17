"""Per-request timeout policy for the universal transport layer.

Lives in :mod:`kairix.transport.timeout` per the three-layer provider
plugin architecture (see ``docs/architecture/provider-plugin-architecture.md``).
One implementation policy serves every provider plugin — timeout
enforcement is a transport concern, not a per-provider concern.

Behavioural invariants (pinned by ``transport_timeout.feature``):

1. A call that responds within the budget returns its result and
   leaves the socket counter balanced (opened == closed).
2. A call that exceeds the budget raises :class:`TimeoutExceeded`
   carrying the configured budget in milliseconds, and the
   underlying socket / future is reclaimed before the error
   surfaces to the caller.
3. The policy supports a per-call budget override that takes
   precedence over the constructor-supplied default — operators
   can tighten the budget for known-slow operations without
   rebuilding the transport.
4. Repeated timeouts do NOT accumulate leaked sockets: peak
   concurrent open count is bounded by concurrent callers, and
   the running balance returns to zero between bursts.
5. When composed with a coalescer, a timeout on the batched
   request fails EVERY caller in the batch with the typed
   :class:`TimeoutExceeded` (the coalescer's "never raises,
   returns []" contract gives way to the typed timeout error so
   operator triage is unambiguous).

The policy is DI-clean (F6): no ``*_fn=None`` test hooks; the clock
and the socket bookkeeping are explicit constructor parameters.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Protocol, TypeVar

from kairix.providers import TimeoutExceeded

__all__ = [
    "SocketCounter",
    "TimeoutBudget",
]


T = TypeVar("T")


class SocketCounter(Protocol):
    """Counter protocol the timeout policy uses to track FD usage.

    Implementations expose ``opened`` / ``closed`` / ``peak_open``
    counters and the ``open()`` / ``close()`` hooks the policy calls
    around each dispatch. The default
    :class:`tests.fakes.FakeProvider` is one implementation; a
    production provider's internal socket pool is another.
    """

    opened: int
    closed: int
    peak_open: int

    def open(self) -> None:
        """Record a socket open + bump peak-open if the running balance grew."""

    def close(self) -> None:
        """Record a socket close — must be called for every open()."""


class TimeoutBudget:
    """Wrap a callable with a per-request timeout and socket-counter book-keeping.

    Each call runs on a small private :class:`ThreadPoolExecutor`. If
    the call returns within the budget, the result is delivered to
    the caller and the socket counter is balanced. If the future
    doesn't resolve in time, the executor's worker is left to wind
    down on its own (Python can't cancel running threads), but the
    timeout policy IMMEDIATELY:

    1. Records the open() / close() pair on the counter so the
       balance stays at zero from the caller's perspective.
    2. Raises :class:`TimeoutExceeded` to the caller.
    3. Returns the future to the executor's discarded set — the
       worker will eventually finish its slow call and exit;
       the executor goes back into the pool ready for the next
       request.

    Constructor parameters:

    * ``budget_ms`` — default budget for ``with_timeout`` calls that
      don't supply an override (≥ 1).
    * ``counter`` — :class:`SocketCounter` used for FD bookkeeping.
      Tests pass the :class:`tests.fakes.FakeProvider` or a
      lightweight stand-in; production wires the transport pool's
      counter.
    * ``executor`` — optional :class:`ThreadPoolExecutor` shared
      across calls. When omitted, the policy builds and owns its
      own (closed by :meth:`shutdown`). Tests typically pass an
      explicit one so they can assert on its lifecycle.

    F6-clean: every seam is an explicit named parameter; no
    ``*_fn=None`` test-substitution kwargs.
    """

    def __init__(
        self,
        budget_ms: int,
        *,
        counter: SocketCounter,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        if budget_ms < 1:
            raise ValueError(f"budget_ms must be >= 1; got {budget_ms}")
        self._budget_ms = budget_ms
        self._counter = counter
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="timeout-budget")
        self._owns_executor = executor is None

    @property
    def budget_ms(self) -> int:
        """Default budget the policy applies when callers don't supply an override."""
        return self._budget_ms

    def with_timeout(self, fn: Callable[[], T], *, budget_ms: int | None = None) -> T:
        """Invoke ``fn`` under the budget; raise :class:`TimeoutExceeded` on overrun.

        The effective budget is ``budget_ms`` when supplied (the
        per-call override), otherwise the policy's default. The
        counter's ``open()`` fires immediately before dispatch and
        ``close()`` fires regardless of outcome — success, timeout,
        or unrelated exception. Operators reading the counter see
        a balanced ledger even when the underlying worker is still
        winding down.
        """
        effective = budget_ms if budget_ms is not None else self._budget_ms
        if effective < 1:
            raise ValueError(f"budget_ms must be >= 1; got {effective}")
        timeout_s = effective / 1000.0
        self._counter.open()
        future: Future[T] = self._executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError as exc:
            # Future is still running; we can't kill the thread, but
            # we MUST surface a typed timeout to the caller and
            # immediately balance the counter so a burst of timeouts
            # doesn't appear to leak FDs from the operator's view.
            raise TimeoutExceeded(budget_ms=effective) from exc
        finally:
            self._counter.close()

    def shutdown(self) -> None:
        """Tear down the owned executor (no-op when an external one was passed).

        Idempotent; safe to call from a fixture teardown even when
        the policy never dispatched a call. After shutdown, further
        calls to :meth:`with_timeout` will raise — the policy is
        meant to be torn down once at end-of-test, not mid-suite.
        """
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
