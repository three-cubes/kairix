"""Step definitions for eval_judge.feature."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_bdd import given, then, when

from kairix.quality.eval.judge import JudgeCalibrationError, JudgeResult, LLMJudge
from tests.fakes import FakeChatBackend

# Module-level state shared across steps. Reset implicitly per scenario via
# the autouse fixture below.
_state: dict[str, Any] = {}


def _grade_response(grades: dict[str, int]) -> str:
    return json.dumps(grades)


@pytest.fixture(autouse=True)
def _reset_judge_state() -> None:
    _state.clear()


@given("a chat backend that returns grades A=2, B=1, C=0")
def chat_backend_with_canonical_grades() -> None:
    _state["backend"] = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 1, "C": 0})])


@given("a chat backend that always raises a connection error")
def chat_backend_raising_connection_error() -> None:
    _state["backend"] = FakeChatBackend(raise_on_call=OSError("connection refused"))


@given("a chat backend that returns grades A=5, B=-1, C=1")
def chat_backend_with_out_of_range_grades() -> None:
    _state["backend"] = FakeChatBackend(responses=[_grade_response({"A": 5, "B": -1, "C": 1})])


@given('three candidate documents for the query "How do I deploy?"')
def three_candidates() -> None:
    _state["query"] = "How do I deploy?"
    _state["candidates"] = [
        ("docker-deployment-guide", "Deploy with docker build, tag, push, run."),
        ("ci-cd-pipeline", "GitHub Actions runs on all PRs before merge."),
        ("api-guidelines", "Public APIs require rate limiting and authentication."),
    ]


@given("a chat backend that answers each calibration anchor correctly")
def chat_backend_anchors_correct() -> None:
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    _state["backend"] = FakeChatBackend(
        responses=[_grade_response({"A": anchor["expected"]}) for anchor in CALIBRATION_ANCHORS]
    )


@given("a chat backend that returns wrong grades for every calibration anchor")
def chat_backend_anchors_all_wrong() -> None:
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    _state["backend"] = FakeChatBackend(responses=[_grade_response({"A": 0}) for _ in CALIBRATION_ANCHORS])


@when("the operator runs the LLM judge against the candidates")
def run_judge() -> None:
    judge = LLMJudge(chat_backend=_state["backend"])
    _state["result"] = judge.grade(
        _state["query"],
        _state["candidates"],
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://test.openai.azure.com",
        shuffle=False,
    )


@when("the operator runs the calibration sweep")
def run_calibration() -> None:
    judge = LLMJudge(chat_backend=_state["backend"])
    try:
        _state["calibration_passed"] = judge.calibrate(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://test.openai.azure.com",
        )
        _state["calibration_error"] = None
    except JudgeCalibrationError as e:
        _state["calibration_passed"] = False
        _state["calibration_error"] = e


@then("each candidate receives its assigned grade")
def each_grade_assigned() -> None:
    result: JudgeResult = _state["result"]
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline"] == 1
    assert result.grades["api-guidelines"] == 0


@then("the judge model name is recorded in the result")
def judge_model_recorded() -> None:
    result: JudgeResult = _state["result"]
    assert result.judge_model  # non-empty


@then("every candidate receives grade 0")
def every_candidate_zero() -> None:
    result: JudgeResult = _state["result"]
    assert all(g == 0 for g in result.grades.values())
    assert len(result.grades) == 3


@then("the judge call returns without raising")
def no_exception_raised() -> None:
    # If we got here, the @when step completed without raising.
    assert "result" in _state


@then("the candidate scoring 5 is clamped to grade 2")
def first_clamped_to_two() -> None:
    result: JudgeResult = _state["result"]
    assert result.grades["docker-deployment-guide"] == 2


@then("the candidate scoring -1 is clamped to grade 0")
def second_clamped_to_zero() -> None:
    result: JudgeResult = _state["result"]
    assert result.grades["ci-cd-pipeline"] == 0


@then("the candidate scoring 1 keeps grade 1")
def third_keeps_one() -> None:
    result: JudgeResult = _state["result"]
    assert result.grades["api-guidelines"] == 1


@then("calibration passes")
def calibration_passes() -> None:
    assert _state["calibration_passed"] is True
    assert _state["calibration_error"] is None


@then("calibration raises an error so the operator can stop the run")
def calibration_raises_error() -> None:
    assert _state["calibration_passed"] is False
    assert isinstance(_state["calibration_error"], JudgeCalibrationError)
