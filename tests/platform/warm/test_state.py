"""Tests for the warm-state tracking + cold-start envelope (#278)."""

from __future__ import annotations

import pytest

from kairix.platform.warm.state import (
    cold_start_envelope,
    is_warm,
    is_warming,
    mark_warm,
    mark_warming,
    reset_warm_state,
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
