"""Tests for the warm-state tracking + cold-start envelope (#278)."""

from __future__ import annotations

import pytest

from kairix.platform.warm.state import (
    cold_start_envelope,
    is_warm,
    is_warm_persisted,
    is_warming,
    mark_warm,
    mark_warming,
    reset_warm_state,
    warm_flag_path,
    warm_status,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Each test starts with a cold process — state shouldn't bleed."""
    reset_warm_state()
    yield
    reset_warm_state()


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_initial_state_is_cold() -> None:
    assert is_warm() is False
    assert is_warming() is False


def test_mark_warming_then_warm_transitions_state_correctly() -> None:
    mark_warming()
    assert is_warming() is True
    assert is_warm() is False

    mark_warm()
    assert is_warm() is True
    assert is_warming() is False


def test_reset_returns_to_cold() -> None:
    mark_warming()
    mark_warm()
    reset_warm_state()
    assert is_warm() is False
    assert is_warming() is False


# ---------------------------------------------------------------------------
# warm_status envelope
# ---------------------------------------------------------------------------


def test_status_when_cold_shows_zero_elapsed() -> None:
    status = warm_status()
    assert status["warm"] is False
    assert status["warming"] is False
    assert status["elapsed_seconds"] == 0.0


def test_status_when_warming_reports_elapsed_time() -> None:
    """While warming, the status surfaces elapsed wall time + ETA.

    Sabotage-proof: replace the time math with a constant and the
    elapsed_seconds assertion fails.
    """
    import time

    mark_warming()
    time.sleep(0.05)
    status = warm_status()
    assert status["warming"] is True
    assert status["elapsed_seconds"] > 0.0
    assert status["estimated_seconds_remaining"] > 0.0


def test_status_when_warm_clears_warming_flag() -> None:
    mark_warming()
    mark_warm()
    status = warm_status()
    assert status["warm"] is True
    assert status["warming"] is False


# ---------------------------------------------------------------------------
# Cross-process flag — `kairix onboard ready` reads this via is_warm_persisted
# (running as docker healthcheck in a separate process from the MCP server).
# ---------------------------------------------------------------------------


def test_warm_flag_absent_when_process_is_cold() -> None:
    """A fresh process has no flag on disk — ``kairix onboard ready`` reports
    not-ready, ``docker compose up --wait`` keeps polling.
    """
    assert not is_warm_persisted()


def test_mark_warm_writes_flag_visible_to_other_processes() -> None:
    """``mark_warm`` writes the cross-process flag so the docker healthcheck
    (running as a separate ``docker exec`` shell) can see the MCP server's
    warm state.

    Sabotage-proof: remove the ``flag.touch(...)`` call in ``mark_warm``
    and ``is_warm_persisted`` keeps returning False even after warm completes;
    the healthcheck never flips to ready and ``compose up --wait`` hangs.
    """
    mark_warm()

    assert is_warm_persisted()
    assert warm_flag_path().exists()


def test_reset_warm_state_removes_the_flag() -> None:
    """Resetting warm state also removes the flag so the next process
    starts fresh (mirrors a container restart wiping ``/tmp``).

    Sabotage-proof: remove the ``warm_flag_path().unlink(...)`` call in
    ``reset_warm_state`` and tests / containers carry warm flags across
    restarts that should have been cold-starting from scratch.
    """
    mark_warm()
    assert is_warm_persisted()

    reset_warm_state()

    assert not is_warm_persisted()


# ---------------------------------------------------------------------------
# cold_start_envelope — the agent-facing affordance
# ---------------------------------------------------------------------------


def test_cold_envelope_shape_carries_required_fields() -> None:
    """Every field the agent needs to know what to do next."""
    env = cold_start_envelope("search")
    for key in ("error", "tool", "status", "elapsed_seconds", "estimated_seconds_remaining", "guidance", "see_also"):
        assert key in env, f"envelope missing {key!r}; got {sorted(env.keys())}"
    assert env["error"] == "ColdStart"
    assert env["tool"] == "search"


def test_cold_envelope_guidance_includes_retry_eta() -> None:
    """The guidance string carries a concrete 'retry in N seconds' marker.

    Sabotage-proof: if the guidance becomes generic ('try again later'),
    this test fails because no number appears.
    """
    env = cold_start_envelope("entity")
    guidance = env["guidance"]
    assert "Retry" in guidance, f"guidance must say 'Retry'; got {guidance!r}"
    assert "second" in guidance.lower(), f"guidance must give a time-frame in seconds; got {guidance!r}"
    # Must contain at least one digit (the ETA).
    assert any(c.isdigit() for c in guidance), f"guidance must include a numeric ETA; got {guidance!r}"


def test_cold_envelope_status_label_reflects_state() -> None:
    """status is 'warming' when a warm-up is in progress, 'cold' otherwise."""
    env_cold = cold_start_envelope("search")
    assert env_cold["status"] == "cold"

    mark_warming()
    env_warming = cold_start_envelope("search")
    assert env_warming["status"] == "warming"


def test_cold_envelope_tool_name_threads_through() -> None:
    """The envelope names which tool the agent invoked, so the retry is targeted."""
    assert cold_start_envelope("search")["tool"] == "search"
    assert cold_start_envelope("entity")["tool"] == "entity"
    assert cold_start_envelope("prep")["tool"] == "prep"


# ---------------------------------------------------------------------------
# trigger_background_warm — daemon-thread orchestration
# ---------------------------------------------------------------------------


def _wait_for_warming_to_clear(timeout_s: float = 2.0) -> None:
    """Poll until is_warming() returns False, with a small timeout cap.

    The background thread is daemonised; we don't hold a reference to it.
    Polling is_warming() is the public observability seam and matches how
    a real caller (e.g. tool_search) would detect warm-up completion.
    """
    import time as _t

    deadline = _t.monotonic() + timeout_s
    while is_warming() and _t.monotonic() < deadline:
        _t.sleep(0.01)


def test_trigger_background_warm_is_noop_when_already_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """When warm=True, trigger_background_warm returns without starting a thread.

    Sabotage-proof: remove the ``if _state[_K_WARM] or _state[_K_WARMING]: return``
    guard and run_warm gets called again — the call_count assertion fails.
    """
    from kairix.platform.warm.state import trigger_background_warm

    calls = {"n": 0}

    def fake_run_warm() -> object:  # pragma: no cover — should never run on this path
        calls["n"] += 1
        raise AssertionError("run_warm must not be called when already warm")

    mark_warm()  # process is already warm
    trigger_background_warm(warm_runner=fake_run_warm)
    _wait_for_warming_to_clear()
    assert calls["n"] == 0


def test_trigger_background_warm_marks_warm_on_ok_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: run_warm returns ok=True → state transitions to warm.

    Sabotage-proof: drop the ``if result.ok: mark_warm()`` branch and is_warm()
    stays False after the thread completes.
    """
    from kairix.platform.warm.runner import WarmResult
    from kairix.platform.warm.state import trigger_background_warm

    trigger_background_warm(warm_runner=lambda: WarmResult(ok=True))
    _wait_for_warming_to_clear()
    assert is_warm() is True
    assert is_warming() is False


def test_trigger_background_warm_clears_warming_on_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_warm returns ok=False → warming flag clears; warm stays False.

    Operator can retry later — the next trigger call won't be short-circuited
    by a stale warming flag. Sabotage-proof: drop the ``with _lock:
    _state[_K_WARMING] = False`` in the else branch and is_warming() stays True
    forever after a failed warm.
    """
    from kairix.platform.warm.runner import WarmFailure, WarmResult
    from kairix.platform.warm.state import trigger_background_warm

    failed_result = WarmResult(ok=False, failures=[WarmFailure(step="vector", detail="boom")])
    trigger_background_warm(warm_runner=lambda: failed_result)
    _wait_for_warming_to_clear()
    assert is_warm() is False
    assert is_warming() is False  # the next trigger isn't blocked


def test_trigger_background_warm_clears_warming_when_run_warm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_warm raises → background thread swallows + clears warming flag.

    Otherwise a transient exception during warm-up would deadlock retries
    (subsequent triggers would see ``warming=True`` and no-op forever).
    Sabotage-proof: remove the ``except Exception`` block and the thread
    propagates uncaught + leaves warming=True; the assertion catches it.
    """
    from kairix.platform.warm.state import trigger_background_warm

    def raiser() -> object:
        raise RuntimeError("transient warm-up failure")

    trigger_background_warm(warm_runner=raiser)
    _wait_for_warming_to_clear()
    assert is_warm() is False
    assert is_warming() is False
