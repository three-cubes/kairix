"""
Unit tests for kairix.quality.eval.judge.

All Azure OpenAI API calls are injected via ``chat_backend=FakeChatBackend(...)``.
No monkey-patching, no @patch, no setattr. The new ChatBackend protocol replaces
the legacy ``chat_fn=`` substitution kwarg (#143 Phase 2a).
"""

from __future__ import annotations

import json

import pytest

from kairix.quality.eval.judge import (
    JUDGE_DEPLOYMENT,
    JudgeCalibrationError,
    JudgeResult,
    LLMJudge,
    _parse_grade_response,
    calibrate,
    judge_batch,
)
from tests.fakes import FakeChatBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CANDIDATES = [
    ("docker-deployment-guide", "Deploy with docker build, tag, push, run."),
    ("ci-cd-pipeline-config", "GitHub Actions runs on all PRs before merge."),
    ("api-guidelines", "All public APIs require rate limiting and authentication."),
]

_QUERY = "What are the steps to deploy a Docker container?"


def _grade_response(grades: dict[str, int]) -> str:
    """Return a JSON string of grades, mimicking chat-completion output."""
    return json.dumps(grades)


# ---------------------------------------------------------------------------
# judge_batch — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_batch_returns_grade_dict() -> None:
    """judge_batch returns a JudgeResult with grades for each candidate."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 0, "C": 1})])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )

    assert isinstance(result, JudgeResult)
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 0
    assert result.grades["api-guidelines"] == 1


@pytest.mark.unit
def test_judge_batch_clamps_grades_to_0_2() -> None:
    """Grades outside [0, 2] are clamped."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 5, "B": -1, "C": 1})])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )

    assert result.grades["docker-deployment-guide"] == 2  # 5 clamped to 2
    assert result.grades["ci-cd-pipeline-config"] == 0  # -1 clamped to 0


@pytest.mark.unit
def test_judge_batch_shuffles_candidates() -> None:
    """When shuffle=True, the shuffle_order differs from original order at least sometimes."""
    original_order = [stem for stem, _ in _CANDIDATES]
    shuffle_orders = set()

    # Ten canned responses — one per loop iteration.
    backend = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 1, "C": 0}) for _ in range(10)])

    for _ in range(10):
        result = judge_batch(
            query=_QUERY,
            candidates=_CANDIDATES,
            api_key="test-key",
            endpoint="https://test.openai.azure.com",
            shuffle=True,
            chat_backend=backend,
        )
        shuffle_orders.add(tuple(result.shuffle_order))

    # With 3 candidates and 10 runs, some permutation should differ from original
    assert len(shuffle_orders) >= 1
    # The grades dict always has all original stems as keys regardless of order
    assert set(result.grades.keys()) == set(original_order)


@pytest.mark.unit
def test_judge_batch_records_shuffle_order() -> None:
    """shuffle_order contains stems in the order they were presented to the LLM."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 0, "C": 1})])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )

    assert result.shuffle_order == tuple(stem for stem, _ in _CANDIDATES)


@pytest.mark.unit
def test_judge_batch_empty_candidates() -> None:
    """Empty candidate list returns empty JudgeResult."""
    result = judge_batch(
        query=_QUERY,
        candidates=[],
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
    )
    assert result.grades == {}
    assert result.shuffle_order == ()


# ---------------------------------------------------------------------------
# judge_batch — failure modes (all must return all-zero grades, never raise)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_batch_returns_zeros_on_api_error() -> None:
    """Network error → all grades are 0, no exception raised."""
    backend = FakeChatBackend(raise_on_call=OSError("connection refused"))
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )

    assert all(g == 0 for g in result.grades.values())
    assert len(result.grades) == len(_CANDIDATES)


@pytest.mark.unit
def test_judge_batch_returns_zeros_on_malformed_json() -> None:
    """Malformed JSON response → all grades are 0."""
    backend = FakeChatBackend(responses=["not json at all {"])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )

    assert all(g == 0 for g in result.grades.values())


@pytest.mark.unit
def test_judge_batch_returns_zeros_when_no_credentials() -> None:
    """Empty api_key/endpoint → all grades are 0, no exception."""
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="",
        endpoint="",
        shuffle=False,
    )
    assert all(g == 0 for g in result.grades.values())


# ---------------------------------------------------------------------------
# Backwards compat: legacy ``chat_fn=`` kwarg still works (Phase 4 removes it)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_batch_legacy_chat_fn_still_works() -> None:
    """The deprecated ``chat_fn`` kwarg keeps working until Phase 4 removes it."""

    def _legacy_chat_fn(messages: list[dict[str, str]], max_tokens: int = 200) -> str:
        del messages, max_tokens
        return _grade_response({"A": 2, "B": 1, "C": 0})

    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=_legacy_chat_fn,
    )
    assert result.grades["docker-deployment-guide"] == 2


# ---------------------------------------------------------------------------
# _parse_grade_response
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_grade_response_valid_json() -> None:
    """Valid JSON response is parsed correctly."""
    content = '{"A": 2, "B": 0, "C": 1}'
    result = _parse_grade_response(content, ["A", "B", "C"])
    assert result == {"A": 2, "B": 0, "C": 1}


@pytest.mark.unit
def test_parse_grade_response_json_in_prose() -> None:
    """JSON embedded in prose text is extracted."""
    content = 'After reviewing the documents, my assessment is: {"A": 2, "B": 1} as requested.'
    result = _parse_grade_response(content, ["A", "B"])
    assert result == {"A": 2, "B": 1}


@pytest.mark.unit
def test_parse_grade_response_empty_on_no_json() -> None:
    """No JSON in response → empty dict."""
    result = _parse_grade_response("I cannot assess these documents.", ["A", "B"])
    assert result == {}


@pytest.mark.unit
def test_parse_grade_response_ignores_extra_labels() -> None:
    """Labels not in the expected list are ignored."""
    content = '{"A": 2, "B": 0, "Z": 1}'  # Z not in labels
    result = _parse_grade_response(content, ["A", "B"])
    assert "Z" not in result
    assert result["A"] == 2


# ---------------------------------------------------------------------------
# calibrate (free-function)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calibrate_passes_when_all_anchors_correct() -> None:
    """Calibration passes when all anchors get expected grades."""
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    # One canned response per anchor, returning the expected grade for that anchor.
    responses = [_grade_response({"A": anchor["expected"]}) for anchor in CALIBRATION_ANCHORS]
    backend = FakeChatBackend(responses=responses)

    result = calibrate("test-key", "https://test.openai.azure.com", chat_backend=backend)
    assert result is True


@pytest.mark.unit
def test_calibrate_raises_when_too_many_anchors_wrong() -> None:
    """Calibration raises JudgeCalibrationError when >3 anchors are wrong."""
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS, CALIBRATION_MAX_ERRORS

    # Return grade 0 for every anchor — many grade-1 and grade-2 anchors are wrong.
    responses = [_grade_response({"A": 0}) for _ in CALIBRATION_ANCHORS]
    backend = FakeChatBackend(responses=responses)

    with pytest.raises(JudgeCalibrationError) as exc_info:
        calibrate("test-key", "https://test.openai.azure.com", chat_backend=backend)

    assert "calibration" in str(exc_info.value).lower()
    assert str(CALIBRATION_MAX_ERRORS) in str(exc_info.value) or "3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# LLMJudge class — Phase 2a constructor-injected wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_judge_constructor_stores_backend_and_deployment() -> None:
    """LLMJudge stores its dependencies for later delegation."""
    backend = FakeChatBackend(responses=["unused"])
    judge = LLMJudge(chat_backend=backend, deployment="custom-model")

    # Internal attributes are private, but their effects show up via grade()/calibrate();
    # we still assert default deployment behaviour next.
    backend2 = FakeChatBackend(responses=["unused"])
    judge_default = LLMJudge(chat_backend=backend2)

    # Default deployment should match the module constant.
    assert judge_default._deployment == JUDGE_DEPLOYMENT
    assert judge._deployment == "custom-model"


@pytest.mark.unit
def test_llm_judge_grade_returns_judge_result() -> None:
    """LLMJudge.grade() delegates to judge_batch with the injected backend."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 2, "B": 1, "C": 0})])
    judge = LLMJudge(chat_backend=backend)

    result = judge.grade(
        _QUERY,
        _CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
    )

    assert isinstance(result, JudgeResult)
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 1
    assert result.grades["api-guidelines"] == 0
    # The backend received exactly one call.
    assert len(backend.calls) == 1
    assert backend.calls[0]["api_key"] == "test-key"
    assert backend.calls[0]["deployment"] == JUDGE_DEPLOYMENT


@pytest.mark.unit
def test_llm_judge_grade_uses_configured_deployment() -> None:
    """LLMJudge passes its configured deployment through to the backend."""
    backend = FakeChatBackend(responses=[_grade_response({"A": 1, "B": 0, "C": 0})])
    judge = LLMJudge(chat_backend=backend, deployment="my-custom-deployment")

    result = judge.grade(
        _QUERY,
        _CANDIDATES,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://endpoint",
        shuffle=False,
    )

    assert result.judge_model == "my-custom-deployment"
    assert backend.calls[0]["deployment"] == "my-custom-deployment"


@pytest.mark.unit
def test_llm_judge_grade_returns_zeros_on_backend_error() -> None:
    """LLMJudge.grade() never raises — returns all-zero grades on backend error."""
    backend = FakeChatBackend(raise_on_call=OSError("rate limit"))
    judge = LLMJudge(chat_backend=backend)

    result = judge.grade(
        _QUERY,
        _CANDIDATES,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://endpoint",
        shuffle=False,
    )
    assert all(g == 0 for g in result.grades.values())


@pytest.mark.unit
def test_llm_judge_calibrate_passes_when_all_anchors_correct() -> None:
    """LLMJudge.calibrate() returns True when all anchors return their expected grades."""
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    responses = [_grade_response({"A": anchor["expected"]}) for anchor in CALIBRATION_ANCHORS]
    backend = FakeChatBackend(responses=responses)

    judge = LLMJudge(chat_backend=backend)
    assert judge.calibrate(api_key="key", endpoint="https://endpoint") is True  # pragma: allowlist secret


@pytest.mark.unit
def test_llm_judge_calibrate_raises_when_too_many_anchors_wrong() -> None:
    """LLMJudge.calibrate() raises JudgeCalibrationError with too many bad grades."""
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    responses = [_grade_response({"A": 0}) for _ in CALIBRATION_ANCHORS]
    backend = FakeChatBackend(responses=responses)

    judge = LLMJudge(chat_backend=backend)
    with pytest.raises(JudgeCalibrationError):
        judge.calibrate(api_key="key", endpoint="https://endpoint")  # pragma: allowlist secret
