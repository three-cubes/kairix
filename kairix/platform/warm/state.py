"""Warm-state tracking + cold-start envelope generation.

An agent that hits a not-yet-warmed kairix gets an immediate structured
response — never a silent 8s block, never an opaque error. The envelope
follows the F21 affordance pattern: every dead-end carries a marked next
step ('retry in N seconds; this is normal startup behaviour').

API:
    is_warm() -> bool          — has run_warm completed successfully?
    warm_status() -> dict      — full state for diagnostic envelope
    cold_start_envelope(tool)  — structured response for a cold-hit call
    trigger_background_warm()  — kick off warm in a daemon thread
    mark_warming() / mark_warm() — called by run_warm internally

Process-global state, threading.Lock-protected. Module-level state is the
right shape here because warm/cold is a process invariant, not a request
concern.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# Estimated wall time for a full warm-up, captured from live profile on
# v2026.5.16a3: build_search_pipeline ~2.5s + probe_search ~4.5s + graph
# open ~0s = ~7s. We round up to 8 so the agent's "retry in ~N seconds"
# message slightly over-promises the wait rather than under-promises.
_ESTIMATED_WARM_SECONDS = 8.0


# State-dict keys — extracted so the same string isn't repeated across
# every read/write site (F17).
_K_WARM = "warm"
_K_WARMING = "warming"
_K_STARTED_AT = "warm_started_at"
_K_COMPLETED_AT = "warm_completed_at"

# Envelope keys.
_K_ELAPSED = "elapsed_seconds"
_K_REMAINING = "estimated_seconds_remaining"


_lock = threading.Lock()
_state: dict[str, Any] = {
    _K_WARM: False,
    _K_WARMING: False,
    _K_STARTED_AT: 0.0,
    _K_COMPLETED_AT: 0.0,
}


def is_warm() -> bool:
    """True if a successful run_warm has completed in this process."""
    with _lock:
        return bool(_state[_K_WARM])


def is_warming() -> bool:
    """True if a warm-up is currently running in the background."""
    with _lock:
        return bool(_state[_K_WARMING])


def mark_warming() -> None:
    """Record that warm-up has started. Called by run_warm."""
    with _lock:
        _state[_K_WARMING] = True
        _state[_K_STARTED_AT] = time.time()


def mark_warm() -> None:
    """Record successful warm-up completion. Called by run_warm on ok=True."""
    with _lock:
        _state[_K_WARM] = True
        _state[_K_WARMING] = False
        _state[_K_COMPLETED_AT] = time.time()


def reset_warm_state() -> None:
    """Clear all state. Tests use this between cases."""
    with _lock:
        _state[_K_WARM] = False
        _state[_K_WARMING] = False
        _state[_K_STARTED_AT] = 0.0
        _state[_K_COMPLETED_AT] = 0.0


def warm_status() -> dict[str, Any]:
    """Diagnostic envelope for the current warm state.

    Used by tool_warm to report state, and by cold_start_envelope to
    compose the agent-facing response.
    """
    with _lock:
        now = time.time()
        elapsed = round(now - _state[_K_STARTED_AT], 1) if _state[_K_STARTED_AT] else 0.0
        estimated_remaining = max(0.0, round(_ESTIMATED_WARM_SECONDS - elapsed, 1)) if _state[_K_WARMING] else 0.0
        return {
            "warm": bool(_state[_K_WARM]),
            "warming": bool(_state[_K_WARMING]),
            _K_ELAPSED: elapsed,
            _K_REMAINING: estimated_remaining,
        }


def cold_start_envelope(tool_name: str) -> dict[str, Any]:
    """Structured response for an agent that hit a not-yet-warm kairix.

    The agent receives a marked next step ('retry in N seconds') instead
    of a slow first call that anchors 'kairix is flaky' in their memory.
    """
    status = warm_status()
    eta = status[_K_REMAINING] or _ESTIMATED_WARM_SECONDS
    state_label = "warming" if status["warming"] else "cold"
    guidance = (
        f"kairix is {state_label} (one-time cost per process). "
        f"Retry this call in ~{int(eta)} seconds. "
        "Subsequent calls in this process will be fast — the warm-up is amortised."
    )
    return {
        "error": "ColdStart",
        "tool": tool_name,
        "status": state_label,
        _K_ELAPSED: status[_K_ELAPSED],
        _K_REMAINING: eta,
        "guidance": guidance,
        "see_also": ["docs/runbooks/kairix-retrieval-health.md"],
    }


def trigger_background_warm() -> None:
    """Start a warm-up in a background thread, if not already running.

    Idempotent: calling this when already warm or already warming is a
    no-op. The background thread is daemonised so an exit while warming
    doesn't block process shutdown.
    """
    with _lock:
        if _state[_K_WARM] or _state[_K_WARMING]:
            return
        _state[_K_WARMING] = True
        _state[_K_STARTED_AT] = time.time()

    def _warm_target() -> None:
        from kairix.platform.warm.runner import run_warm

        try:
            result = run_warm()
            if result.ok:
                mark_warm()
            else:
                logger.warning("background warm-up returned ok=False; %d failure(s)", len(result.failures))
                # Stay in 'warming' state — next trigger_background_warm
                # call will retry after another cold-start envelope expires.
                with _lock:
                    _state[_K_WARMING] = False
        except Exception as exc:
            logger.warning("background warm-up raised: %s", exc, exc_info=True)
            with _lock:
                _state[_K_WARMING] = False

    thread = threading.Thread(target=_warm_target, daemon=True, name="kairix-background-warm")
    thread.start()
