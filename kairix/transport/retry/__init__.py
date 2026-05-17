"""Retry / backoff policy for the universal transport layer.

See ``docs/architecture/provider-plugin-architecture.md`` for the
three-layer split. This is the one retry policy every provider plugin
inherits; per-provider retry behaviour is forbidden by F27.

Public surface:

- :class:`RetryPolicy` — wraps a callable with attempt-bounded retry
  semantics. Raises :class:`kairix.providers.RetryExhausted` on
  exhaustion; surfaces :class:`kairix.providers.ClientError` unchanged
  on 4xx short-circuit.
- :class:`AttemptEvent` — per-attempt record streamed through the
  policy's optional ``telemetry_sink`` callable.
"""

from __future__ import annotations

from kairix.transport.retry.policy import AttemptEvent, RetryPolicy

__all__ = [
    "AttemptEvent",
    "RetryPolicy",
]
