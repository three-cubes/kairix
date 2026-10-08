"""Step definitions for soak.feature.

Drives ``kairix.quality.soak.run_soak`` through injected workload fakes
(no @patch on kairix internals). The legacy ``kairix soak`` CLI was
removed in v2026.6; the CLI-output affordance scenario was deleted with
it.

Each scenario builds a fresh workload closure with a controlled envelope
or side-effect (memory allocation, drifting envelope) and asserts on the
returned :class:`SoakResult`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.quality.soak import run_soak
from kairix.quality.soak.runner import SoakFailure, SoakIteration, SoakResult
from tests.fakes import FakeLatencyClock

pytestmark = pytest.mark.bdd


# Step-phrase fragments lifted to constants so the same literal isn't
# duplicated across given/when/then sites (F17: no >=10-char string
# repeated >=3 times in a module).
_PHRASE_TIME_DRIFT = "time_drift"
_PHRASE_SAME_ENVELOPE = "a workload that returns the same envelope on every call"
_PHRASE_DIFFERENT_ENVELOPES = "a workload that returns different envelopes on each call"
_PHRASE_SLOWS_DOWN = "a workload that runs progressively slower on each iteration"


@pytest.fixture
def _soak_state() -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "workload_runner": None,
        "result": None,
    }


# ---------------------------------------------------------------------------
# Given — build the workload runner
# ---------------------------------------------------------------------------


@given(_PHRASE_SAME_ENVELOPE)
def _given_deterministic_workload(_soak_state: dict[str, Any]) -> None:
    def _runner(_suite: str) -> dict[str, Any]:
        return {"summary": {"weighted_total": 0.9}, "case_count": 1}

    _soak_state["workload_runner"] = _runner


@given(_PHRASE_DIFFERENT_ENVELOPES)
def _given_drifting_workload(_soak_state: dict[str, Any]) -> None:
    counter = {"n": 0}

    def _runner(_suite: str) -> dict[str, Any]:
        counter["n"] += 1
        # Distinct envelope every call → distinct signature every call.
        return {"summary": {"weighted_total": 0.9 + 0.001 * counter["n"]}, "case_count": counter["n"]}

    _soak_state["workload_runner"] = _runner


@given(_PHRASE_SLOWS_DOWN)
def _given_slowing_workload(_soak_state: dict[str, Any]) -> None:
    """Workload that runs progressively slower → fires the time_drift gate.

    The soak runner's time_drift check skips a baseline below 100 ms (noise
    floor) and fires when a later iteration exceeds ``max_time_drift_pct``
    of iter-0. We give iter-0 a 200 ms baseline (above the floor) and have
    iter-1+ sleep 600 ms, which is +200% — well over the default 20% cap.

    Pure workload-level injection — no internals touched, no env vars.
    The durations are spent on a ``FakeLatencyClock`` handed to ``run_soak``
    as its ``clock`` — no real sleeps, so the drift is exact on any host.
    """
    call_index = {"i": -1}
    latency = FakeLatencyClock()

    def _runner(_suite: str) -> dict[str, Any]:
        call_index["i"] += 1
        if call_index["i"] == 0:
            latency.spend(0.2)  # 200 ms baseline (above the 100 ms drift-check floor)
        else:
            latency.spend(0.6)  # 600 ms → +200% drift, gate FIRES
        return {"summary": {"weighted_total": 0.9}, "case_count": 1}

    _soak_state["workload_runner"] = _runner
    _soak_state["clock"] = latency.now


# ---------------------------------------------------------------------------
# When — invoke run_soak
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs soak with repeat {n:d}"))
def _when_run_soak(_soak_state: dict[str, Any], n: int) -> None:
    runner = _soak_state["workload_runner"]
    # A virtual-clock workload hands run_soak its clock; others use the real one.
    clock = _soak_state.get("clock")
    clock_kwargs = {"clock": clock} if clock is not None else {}
    _soak_state["result"] = run_soak(suite="fake", repeat=n, workload_runner=runner, **clock_kwargs)


# ---------------------------------------------------------------------------
# Then — assertions on SoakResult
# ---------------------------------------------------------------------------


@then("soak passes")
def _then_passes(_soak_state: dict[str, Any]) -> None:
    result: SoakResult = _soak_state["result"]
    # Sabotage: flip ``passed=not failures`` to ``passed=False`` in run_soak
    # and a deterministic workload that should obviously pass would fail
    # this assertion, exposing the regression.
    assert result.passed, f"expected soak to pass; got failures={[(f.kind, f.detail) for f in result.failures]}"
    assert result.error == "", f"expected no error; got {result.error!r}"


@then("every iteration has a measurement record")
def _then_iterations_recorded(_soak_state: dict[str, Any]) -> None:
    result: SoakResult = _soak_state["result"]
    # Sabotage: skip the ``iterations.append`` in run_soak's loop and this
    # length assertion fails (the result would carry zero iterations even
    # though the workload ran).
    assert len(result.iterations) == result.repeat
    for it in result.iterations:
        assert isinstance(it, SoakIteration)
        assert it.duration_s >= 0.0


@then("soak fails")
def _then_fails(_soak_state: dict[str, Any]) -> None:
    result: SoakResult = _soak_state["result"]
    # Sabotage: leave ``passed=True`` regardless of failures and this
    # assertion misses (the gate would silently pass under regression).
    assert result.passed is False, f"expected soak to fail; result.passed=True, failures={result.failures}"


@then(parsers.parse('the failure kind is "{kind}"'))
def _then_failure_kind(_soak_state: dict[str, Any], kind: str) -> None:
    result: SoakResult = _soak_state["result"]
    kinds = [f.kind for f in result.failures]
    # Sabotage: drop the specific check (e.g. _check_signature_drift) and
    # the expected kind disappears from the failures list, tripping this
    # assertion.
    assert kind in kinds, (
        f"expected failure kind {kind!r}; got kinds={kinds}, details={[f.detail for f in result.failures]}"
    )


@then("the failure mentions the iteration that breached the cap")
def _then_failure_mentions_iter(_soak_state: dict[str, Any]) -> None:
    result: SoakResult = _soak_state["result"]
    drift_failures = [f for f in result.failures if f.kind == _PHRASE_TIME_DRIFT]
    # Sabotage: stop populating ``iteration=`` on _per_iter_failure and the
    # iteration attribute stays None, breaking this assertion.
    assert drift_failures, f"expected at least one time_drift failure; got {result.failures}"
    for f in drift_failures:
        assert isinstance(f, SoakFailure)
        assert f.iteration is not None and f.iteration >= 1, f"time_drift failure missing iteration index: {f}"
