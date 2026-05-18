"""Retry / backoff policy for the universal transport layer.

Lives in :mod:`kairix.transport.retry` per the three-layer provider
plugin architecture (see ``docs/architecture/provider-plugin-architecture.md``).
One implementation policy serves every provider plugin — retry
behaviour is a transport concern, not a per-provider concern.

Behavioural invariants (pinned by ``transport_retry.feature``):

1. A call that succeeds on attempt 1 is invoked exactly once
   (no eager retries on success).
2. A call that fails transiently then succeeds is retried up to
   ``max_attempts`` before raising :class:`RetryExhausted`.
3. A call that exhausts retries raises :class:`RetryExhausted`
   carrying the attempt count and the last underlying cause.
4. A call that raises :class:`ClientError` (4xx) short-circuits
   on the first attempt — no retries, no quota burnt.
5. Successive retries wait ``backoff_factor`` seconds between
   attempts (linear, not exponential — the operator-facing knob
   is "how long do we pause before the next attempt").
6. Every attempt is recorded as an event via the optional
   ``telemetry_sink`` callable so per-attempt observability is
   first-class, not log-scraping.

The policy is DI-clean (F6): no ``*_fn=None`` test hooks; sleep and
telemetry seams are explicit constructor parameters with production
defaults that callers don't need to think about. Tests inject a
:class:`tests.fakes.FakeClock` for both sleep and clock-readings, so
``backoff_factor`` assertions don't pay real wall-clock time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from kairix.providers import ClientError, RetryExhausted

__all__ = [
    "AttemptEvent",
    "RetryPolicy",
]


T = TypeVar("T")


@dataclass(frozen=True)
class AttemptEvent:
    """One attempt's record, surfaced through the telemetry sink.

    Carries the attempt number (1-indexed), the outcome
    (``"success"``/``"transient"``/``"client_error"``), and the
    monotonic timestamp at which the attempt was made. Operator
    dashboards consume the stream to graph retry density and
    backoff respect.

    Attributes:
        attempt: 1-indexed attempt number this event refers to.
        outcome: ``"success"`` if the call returned, ``"transient"``
            if a retryable exception was caught, ``"client_error"``
            if a :class:`ClientError` short-circuited the policy.
        timestamp: Monotonic clock reading captured immediately
            before the attempt was dispatched.
    """

    attempt: int
    outcome: str
    timestamp: float


class RetryPolicy:
    """Wrap a callable with attempt-bounded retries and linear backoff.

    The policy distinguishes three outcome classes per attempt:

    * Success — return the value; record a ``success`` attempt event.
    * :class:`ClientError` — short-circuit; surface the error to the
      caller unchanged; record a ``client_error`` attempt event.
      4xx responses are not retried because they indicate a
      caller-side problem the policy can't recover from (bad
      credentials, missing model, ...).
    * Any other exception — record a ``transient`` event; if more
      attempts remain, wait ``backoff_factor`` seconds and retry;
      otherwise raise :class:`RetryExhausted` carrying the cause.

    Constructor parameters:

    * ``max_attempts`` — total attempts allowed (≥ 1). Production
      default is 3 for embed/chat; tests pass a deterministic value.
    * ``backoff_factor`` — seconds to sleep between attempts
      (linear). Set to 0 in tests that care only about call shape.
    * ``sleep`` — callable taking ``seconds: float``. Production
      defaults to ``time.sleep``; tests pass ``FakeClock.advance``
      so backoff assertions cost zero wall-clock time.
    * ``clock`` — callable returning a monotonic float; tests pass
      ``FakeClock.now`` so attempt timestamps are deterministic.
    * ``telemetry_sink`` — optional callable accepting an
      :class:`AttemptEvent`; tests append events to a list.

    F6-clean: every seam is an explicit named parameter with a
    production default. No ``*_fn=None`` test-substitution kwargs.
    """

    def __init__(
        self,
        max_attempts: int,
        backoff_factor: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry_sink: Callable[[AttemptEvent], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1; got {max_attempts}")
        if backoff_factor < 0:
            raise ValueError(f"backoff_factor must be >= 0; got {backoff_factor}")
        self._max_attempts = max_attempts
        self._backoff_factor = float(backoff_factor)
        self._sleep = sleep
        self._clock = clock
        self._telemetry_sink = telemetry_sink

    def with_retry(self, fn: Callable[[], T]) -> T:
        """Invoke ``fn`` with attempt-bounded retry semantics.

        Returns the first successful return value of ``fn``. Raises
        :class:`ClientError` unchanged when ``fn`` raises one. Raises
        :class:`RetryExhausted` when every attempt fails transiently.

        Attempts beyond the first wait ``backoff_factor`` seconds
        before dispatching. The wait happens BETWEEN attempts (after
        attempt N fails, before attempt N+1 fires), not at the start
        of the first attempt — a happy-path success on attempt 1
        pays zero backoff cost.
        """
        last_cause: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            if attempt > 1 and self._backoff_factor > 0:
                self._sleep(self._backoff_factor)
            timestamp = self._clock()
            try:
                result = fn()
            except ClientError:
                # 4xx — short-circuit; caller-side problem, no retry.
                self._emit(AttemptEvent(attempt=attempt, outcome="client_error", timestamp=timestamp))
                raise
            except Exception as exc:
                # Any other exception is treated as transient — retry
                # while attempts remain; record the cause for the
                # eventual RetryExhausted if we run out.
                last_cause = exc
                self._emit(AttemptEvent(attempt=attempt, outcome="transient", timestamp=timestamp))
                continue
            else:
                self._emit(AttemptEvent(attempt=attempt, outcome="success", timestamp=timestamp))
                return result
        raise RetryExhausted(attempts=self._max_attempts, last_cause=last_cause)

    def _emit(self, event: AttemptEvent) -> None:
        """Forward an attempt event to the telemetry sink (no-op when unset)."""
        if self._telemetry_sink is not None:
            self._telemetry_sink(event)
