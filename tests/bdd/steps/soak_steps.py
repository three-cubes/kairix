"""Step definitions for soak.feature.

Drives ``kairix.quality.soak.run_soak`` and ``kairix.quality.soak.cli.main``
through injected workload fakes (no @patch on kairix internals; the soak
CLI test uses ``mock.patch.object`` on the CLI module's binding site for
``run_soak`` — same shape as ``tests/quality/probe/test_cli.py``).

Each scenario builds a fresh workload closure with a controlled envelope
or side-effect (memory allocation, drifting envelope) and asserts on the
returned :class:`SoakResult` or the CLI's stdout/stderr output.
"""

from __future__ import annotations

import io
import time
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.quality.soak import cli as soak_cli
from kairix.quality.soak import run_soak
from kairix.quality.soak.runner import SoakFailure, SoakIteration, SoakResult

pytestmark = pytest.mark.bdd


# Step-phrase fragments lifted to constants so the same literal isn't
# duplicated across given/when/then sites (F17: no >=10-char string
# repeated >=3 times in a module).
_PHRASE_TIME_DRIFT = "time_drift"
_PHRASE_SAME_ENVELOPE = "a workload that returns the same envelope on every call"
_PHRASE_DIFFERENT_ENVELOPES = "a workload that returns different envelopes on each call"
_PHRASE_SLOWS_DOWN = "a workload that runs progressively slower on each iteration"
_PHRASE_FIRES_A_GATE = "a workload that fires a soak gate"


@pytest.fixture
def _soak_state() -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "workload_runner": None,
        "result": None,
        "cli_stdout": "",
        "cli_stderr": "",
        "cli_exit_code": 0,
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
@given(_PHRASE_FIRES_A_GATE)
def _given_slowing_workload(_soak_state: dict[str, Any]) -> None:
    """Workload that runs progressively slower → fires the time_drift gate.

    The soak runner's time_drift check skips a baseline below 100 ms (noise
    floor) and fires when a later iteration exceeds ``max_time_drift_pct``
    of iter-0. We give iter-0 a 200 ms baseline (above the floor) and have
    iter-1+ sleep 600 ms, which is +200% — well over the default 20% cap.

    Pure workload-level injection — no internals touched, no env vars.
    Deterministic across Python versions because ``time.sleep`` is portable.
    """
    call_index = {"i": -1}

    def _runner(_suite: str) -> dict[str, Any]:
        call_index["i"] += 1
        if call_index["i"] == 0:
            time.sleep(0.2)  # 200 ms baseline (above the 100 ms drift-check floor)
        else:
            time.sleep(0.6)  # 600 ms → +200% drift, gate FIRES
        return {"summary": {"weighted_total": 0.9}, "case_count": 1}

    _soak_state["workload_runner"] = _runner


# ---------------------------------------------------------------------------
# When — invoke run_soak / soak CLI
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs soak with repeat {n:d}"))
def _when_run_soak(_soak_state: dict[str, Any], n: int) -> None:
    runner = _soak_state["workload_runner"]
    _soak_state["result"] = run_soak(suite="fake", repeat=n, workload_runner=runner)


@when(parsers.parse("the operator invokes the soak CLI with repeat {n:d}"))
def _when_invoke_soak_cli(_soak_state: dict[str, Any], n: int) -> None:
    """Drive the CLI's ``main`` directly. ``run_soak`` is rebound on the
    CLI module so the fake workload's effect is reflected in the CLI output.

    ``mock.patch.object`` against the CLI module's binding site is the
    documented seam — F1 forbids @patch on ``kairix.<...>`` *target strings*,
    not patch.object on a local module reference (same pattern as
    tests/quality/probe/test_cli.py).
    """
    fake_workload = _soak_state["workload_runner"]

    def _fake_run_soak(*args: Any, **kwargs: Any) -> SoakResult:
        # Forward every arg the CLI passed, just inject our workload runner.
        kwargs["workload_runner"] = fake_workload
        return run_soak(*args, **kwargs)

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(soak_cli, "run_soak", side_effect=_fake_run_soak):
        with redirect_stdout(out), redirect_stderr(err):
            rc = soak_cli.main(["run", "--suite", "fake", "--repeat", str(n)])
    _soak_state["cli_exit_code"] = rc
    _soak_state["cli_stdout"] = out.getvalue()
    _soak_state["cli_stderr"] = err.getvalue()


# ---------------------------------------------------------------------------
# Then — assertions on SoakResult / CLI output
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


@then(parsers.parse('the stderr or stdout contains "{marker}"'))
def _then_output_contains(_soak_state: dict[str, Any], marker: str) -> None:
    combined = _soak_state["cli_stdout"] + _soak_state["cli_stderr"]
    # Sabotage: remove the "fix:" / "next:" lines from soak_cli._format_text
    # and an operator reading the failure output loses the F21 affordance —
    # this assertion catches that regression.
    assert marker in combined, (
        f"expected affordance marker {marker!r} in CLI output; "
        f"stdout={_soak_state['cli_stdout']!r} stderr={_soak_state['cli_stderr']!r}"
    )
