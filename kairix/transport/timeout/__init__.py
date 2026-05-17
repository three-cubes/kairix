"""Per-request timeout policy for the universal transport layer.

See ``docs/architecture/provider-plugin-architecture.md`` for the
three-layer split. This is the one timeout policy every provider
plugin inherits; per-provider timeout enforcement is forbidden by F27.

Public surface:

- :class:`TimeoutBudget` — wraps a callable with a per-request
  timeout. Raises :class:`kairix.providers.TimeoutExceeded` on overrun.
- :class:`SocketCounter` — Protocol the policy uses for FD bookkeeping;
  the default :class:`tests.fakes.FakeProvider` implements it.
"""

from __future__ import annotations

from kairix.transport.timeout.budget import SocketCounter, TimeoutBudget

__all__ = [
    "SocketCounter",
    "TimeoutBudget",
]
