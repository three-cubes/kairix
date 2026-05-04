"""Readiness gating for the MCP server.

Kairix's MCP server has cold-start cost: Neo4j driver init, LLM client
construction, sqlite-vec extension load. The 2026-05-02 dogfood report
saw "request before initialization complete" errors when the gateway
dispatched tool calls before kairix had finished warming up.

This module exposes a small Protocol + Adapter pair the server uses to
signal readiness:

  - ``ReadinessGate`` Protocol — ``is_ready()`` for /healthz reflection,
    ``mark_ready()`` to flip to ready once warm-up completes.
  - ``EventReadinessGate`` Adapter — backed by a simple boolean flag.
    Thread-safe for the simple ready/not-ready semantics it implements
    (single mutation, atomic read).

The ``build_mcp_app`` composer accepts ``readiness_check: Callable[[], bool] | None``;
pass ``gate.is_ready`` as the callable. The lifecycle is:

  1. Server starts; ``EventReadinessGate()`` constructed (not ready).
  2. /healthz returns ``{"ready": false, "uptime_s": N}``.
  3. Background warm-up completes; ``gate.mark_ready()`` flips the flag.
  4. /healthz returns ``{"ready": true, "uptime_s": N}``.

The Protocol is domain-local to ``kairix/agents/mcp/`` because no other
subsystem currently needs readiness gating. If a second consumer shows
up, promote it to ``kairix/core/protocols.py``.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable


@runtime_checkable
class ReadinessGate(Protocol):
    """Signals whether the MCP server has finished warming up.

    ``is_ready()`` is called by the /healthz route on every request and
    by tool handlers that want to short-circuit with a structured
    "still initializing" error. ``mark_ready()`` is called once, by the
    startup hook, after lazy-init of Neo4j/LLM/vector clients.
    """

    def is_ready(self) -> bool: ...

    def mark_ready(self) -> None: ...


class EventReadinessGate:
    """Threadsafe boolean-flag implementation of ReadinessGate.

    Default state is not-ready. ``mark_ready()`` is idempotent — calling
    it after the gate is already ready is a no-op.
    """

    def __init__(self, *, ready: bool = False) -> None:
        self._ready = ready
        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        with self._lock:
            self._ready = True
