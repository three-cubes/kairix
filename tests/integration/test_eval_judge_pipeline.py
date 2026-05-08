"""End-to-end integration tests for the LLM judge.

Exercises ``LLMJudge.grade`` and ``LLMJudge.calibrate`` directly with the
production wiring (via ``FakeChatBackend`` from tests/fakes.py — no
monkeypatch). Closes the integration-coverage gap: previously the judge was
only reached transitively through the SuiteGenerator pipeline.
"""

from __future__ import annotations

import json

import pytest

from kairix.quality.eval.judge import (
    CALIBRATION_ANCHORS,
    JudgeCalibrationError,
    LLMJudge,
)
from tests.fakes import FakeChatBackend

pytestmark = pytest.mark.integration


def _grade_response(grades: dict[str, int]) -> str:
    return json.dumps(grades)


@pytest.mark.integration
def test_llm_judge_full_grade_cycle_produces_judge_result() -> None:
    """A complete grade cycle returns a JudgeResult with grades, model, shuffle order."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 1, "C": 0})])
    judge = LLMJudge(chat_backend=backend, deployment="gpt-4o-mini-test")

    result = judge.grade(
        "How do I deploy a Docker container?",
        [
            ("docker-deployment-guide", "Build, tag, push, run."),
            ("ci-cd-pipeline", "GitHub Actions runs on PR merge."),
            ("api-guidelines", "Public APIs require rate limiting."),
        ],
        api_key="integration-key",  # pragma: allowlist secret
        endpoint="https://integration-endpoint",
        shuffle=False,
    )

    assert result.judge_model == "gpt-4o-mini-test"
    assert set(result.grades.keys()) == {"docker-deployment-guide", "ci-cd-pipeline", "api-guidelines"}
    assert result.shuffle_order == (
        "docker-deployment-guide",
        "ci-cd-pipeline",
        "api-guidelines",
    )
    # Backend received the full prompt with credentials passed through.
    assert len(backend.calls) == 1
    assert backend.calls[0]["api_key"] == "integration-key"  # pragma: allowlist secret
    assert backend.calls[0]["deployment"] == "gpt-4o-mini-test"


@pytest.mark.integration
def test_llm_judge_full_calibration_cycle_passes_with_correct_anchors() -> None:
    """The 15-anchor calibration sweep returns True when every anchor grades correctly."""
    responses = [_grade_response({"A": anchor["expected"]}) for anchor in CALIBRATION_ANCHORS]
    backend = FakeChatBackend(responses=responses)
    judge = LLMJudge(chat_backend=backend)

    passed = judge.calibrate(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert passed is True
    # One backend call per anchor.
    assert len(backend.calls) == len(CALIBRATION_ANCHORS)


@pytest.mark.integration
def test_llm_judge_calibration_raises_when_too_many_anchors_wrong() -> None:
    """When more than CALIBRATION_MAX_ERRORS anchors grade wrong, calibrate() raises."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 0}) for _ in CALIBRATION_ANCHORS])
    judge = LLMJudge(chat_backend=backend)

    with pytest.raises(JudgeCalibrationError) as exc:
        judge.calibrate(api_key="k", endpoint="https://ep")  # pragma: allowlist secret
    assert "calibration" in str(exc.value).lower()


@pytest.mark.integration
def test_llm_judge_returns_zero_grades_when_credentials_missing() -> None:
    """Empty credentials short-circuit the grade path before any backend call."""
    backend = FakeChatBackend(responses=[])  # would IndexError if called
    judge = LLMJudge(chat_backend=backend)

    result = judge.grade(
        "any query",
        [("doc-a", "snippet a"), ("doc-b", "snippet b")],
        api_key="",
        endpoint="",
    )
    assert all(g == 0 for g in result.grades.values())
    assert len(backend.calls) == 0
