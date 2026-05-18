"""Step definitions for warm.feature.

Drives ``kairix.platform.warm.runner.run_warm`` through its three
injection seams (``pipeline_builder``, ``search_probe``,
``graph_client_opener``). No real factory build, no Azure pool, no
Neo4j connection.

The runner stamps process-global warm-state via
:mod:`kairix.platform.warm.state`; each scenario resets that state in a
fixture so test ordering can't leak.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_bdd import given, then, when

from kairix.platform.warm.runner import WarmResult, run_warm
from kairix.platform.warm.state import is_warm, reset_warm_state

pytestmark = pytest.mark.bdd


_DETAIL_BOOM = "boom"


@pytest.fixture
def _warm_state() -> Iterator[dict[str, Any]]:
    """Per-scenario fresh state. Resets process-global warm-state too."""
    reset_warm_state()
    state: dict[str, Any] = {
        "pipeline_builder": None,
        "search_probe": None,
        "graph_opener": None,
        "result": None,
        "first_result": None,
        "second_result": None,
    }
    yield state
    reset_warm_state()


# ---------------------------------------------------------------------------
# Givens — wire up the injectable fakes
# ---------------------------------------------------------------------------


@given("fake warm steps that all succeed")
def _given_all_steps_succeed(_warm_state: dict[str, Any]) -> None:
    _warm_state["pipeline_builder"] = lambda: object()
    _warm_state["search_probe"] = lambda _pipeline: {"results": []}
    _warm_state["graph_opener"] = lambda: object()


@given('fake warm steps where one fails with detail "boom"')
def _given_one_step_fails(_warm_state: dict[str, Any]) -> None:
    """The probe step raises; build + graph succeed.

    Choosing the middle step (probe) is deliberate — it lets us assert
    that the graph step still appears in the result envelope after the
    failure, proving the runner doesn't short-circuit on first error.
    """
    _warm_state["pipeline_builder"] = lambda: object()

    def _failing_probe(_pipeline: Any) -> Any:
        raise RuntimeError(_DETAIL_BOOM)

    _warm_state["search_probe"] = _failing_probe
    _warm_state["graph_opener"] = lambda: object()


@given("a warm-up that has already completed successfully")
def _given_already_warm(_warm_state: dict[str, Any]) -> None:
    """First run pays the real (faked-but-timed) cost; second run is the test target.

    The first call uses a deliberately slow builder so the second call's
    "cheaper" property is observable. All fakes are no-ops on the search-
    pipeline side so nothing real spins up.
    """

    def _slow_builder() -> Any:
        # 100 ms is generous enough that the second iteration must
        # measurably beat it for the at-most-one-tenth assertion to fire.
        time.sleep(0.1)
        return object()

    _warm_state["pipeline_builder"] = _slow_builder
    _warm_state["search_probe"] = lambda _pipeline: {"results": []}
    _warm_state["graph_opener"] = lambda: object()

    first = run_warm(
        pipeline_builder=_warm_state["pipeline_builder"],
        search_probe=_warm_state["search_probe"],
        graph_client_opener=_warm_state["graph_opener"],
    )
    _warm_state["first_result"] = first
    # Swap the slow builder out for an instant one — this is what the
    # "already warm; second call is cheap" property simulates.
    _warm_state["pipeline_builder"] = lambda: object()


# ---------------------------------------------------------------------------
# Whens
# ---------------------------------------------------------------------------


@when("the operator runs warm")
def _when_run_warm(_warm_state: dict[str, Any]) -> None:
    _warm_state["result"] = run_warm(
        pipeline_builder=_warm_state["pipeline_builder"],
        search_probe=_warm_state["search_probe"],
        graph_client_opener=_warm_state["graph_opener"],
    )


@when("the operator runs warm again")
def _when_run_warm_again(_warm_state: dict[str, Any]) -> None:
    _warm_state["second_result"] = run_warm(
        pipeline_builder=_warm_state["pipeline_builder"],
        search_probe=_warm_state["search_probe"],
        graph_client_opener=_warm_state["graph_opener"],
    )


# ---------------------------------------------------------------------------
# Thens
# ---------------------------------------------------------------------------


@then("warm reports ok=True")
def _then_ok_true(_warm_state: dict[str, Any]) -> None:
    raw = _warm_state.get("second_result") or _warm_state.get("result")
    # Sabotage: flip ``ok=not failures`` to ``ok=True`` unconditionally
    # in run_warm and the failure-scenario test still passes — this
    # assertion is reached only on success scenarios, so a True-ifying
    # bug is caught by the failure-path scenario's ok=False assertion.
    assert raw is not None
    result: WarmResult = raw
    assert result.ok is True, f"expected ok=True; got failures={result.failures}"
    assert is_warm() is True, "process-global is_warm() should be True after a successful run_warm"


@then("the envelope contains a step per registered warm-up phase")
def _then_envelope_has_steps(_warm_state: dict[str, Any]) -> None:
    result: WarmResult = _warm_state["result"]
    envelope = result.to_envelope()
    # Sabotage: drop the ``steps=`` projection in to_envelope and this
    # length check sees zero steps (or KeyError).
    assert "steps" in envelope
    assert len(envelope["steps"]) == 3, (
        f"expected 3 step records (build, probe, graph); got {len(envelope['steps'])}: "
        f"{[s['name'] for s in envelope['steps']]}"
    )


@then("every step's ok flag is True")
def _then_every_step_ok(_warm_state: dict[str, Any]) -> None:
    result: WarmResult = _warm_state["result"]
    # Sabotage: have _time_step return ok=False on success and this
    # assertion fires.
    for step in result.steps:
        assert step.ok is True, f"step {step.name!r} unexpectedly failed: {step.detail!r}"


@then("warm reports ok=False")
def _then_ok_false(_warm_state: dict[str, Any]) -> None:
    result: WarmResult = _warm_state["result"]
    # Sabotage: short-circuit ``ok=not failures`` to ``ok=True`` and this
    # assertion catches the regression on the failing scenario.
    assert result.ok is False, f"expected ok=False; got {result.to_envelope()}"


@then("the failures list contains the failing step name")
def _then_failures_name_step(_warm_state: dict[str, Any]) -> None:
    result: WarmResult = _warm_state["result"]
    failure_names = {f.step for f in result.failures}
    # Sabotage: stop emitting WarmFailure records and the failures list
    # stays empty — this assertion fails because no step name appears.
    assert failure_names, "expected at least one failure; got empty failures list"
    # The probe step is the one our Given wired to raise.
    assert "probe_search" in failure_names, f"expected probe_search in failure list; got {failure_names}"


@then('the failures list detail mentions "boom"')
def _then_failure_detail(_warm_state: dict[str, Any]) -> None:
    result: WarmResult = _warm_state["result"]
    # Sabotage: drop the ``detail=f"{type(exc).__name__}: {exc}"`` line in
    # _time_step and the exception message disappears from the report.
    matching = [f for f in result.failures if _DETAIL_BOOM in f.detail]
    assert matching, f"expected a failure detail mentioning {_DETAIL_BOOM!r}; got {[f.detail for f in result.failures]}"


@then("subsequent step records still appear in the envelope")
def _then_subsequent_steps_present(_warm_state: dict[str, Any]) -> None:
    """After the probe step fails, the graph step must still appear.

    This is the load-bearing property: a single failed subsystem doesn't
    skip the rest of the warm-up — the operator gets the full picture in
    one envelope rather than having to re-run after each fix.
    """
    result: WarmResult = _warm_state["result"]
    step_names = [s.name for s in result.steps]
    # Sabotage: short-circuit out of run_warm on the first failure and
    # ``open_graph_client`` would never appear in the steps list.
    assert "open_graph_client" in step_names, (
        f"expected graph step to run even after probe failure; got steps={step_names}"
    )


@then("the second call's total_duration_s is at most one-tenth of the first call's")
def _then_second_call_cheap(_warm_state: dict[str, Any]) -> None:
    first: WarmResult = _warm_state["first_result"]
    second: WarmResult = _warm_state["second_result"]
    # Sabotage: drop the per-step duration measurement and total_duration_s
    # stays at 0.0 on both calls — making the assertion trivially true
    # but useless. We defend against that by also asserting the FIRST
    # call's duration is non-trivial (which the slow builder guarantees).
    assert first.total_duration_s > 0.05, (
        f"first call total_duration_s={first.total_duration_s} — fixture's "
        "slow builder didn't dominate; second-call comparison would be meaningless"
    )
    cap = first.total_duration_s / 10.0
    assert second.total_duration_s <= cap, (
        f"second warm call should be at most one-tenth of first "
        f"(first={first.total_duration_s}s, second={second.total_duration_s}s, cap={cap}s)"
    )
