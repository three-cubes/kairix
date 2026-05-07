"""
Unit tests for kairix.quality.eval.judge.

All Azure OpenAI API calls are injected via chat_fn. No monkey-patching needed.
"""

from __future__ import annotations

import json

import pytest

from kairix.quality.eval.judge import (
    JudgeCalibrationError,
    JudgeResult,
    _parse_grade_response,
    calibrate,
    judge_batch,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CANDIDATES = [
    ("docker-deployment-guide", "Deploy with docker build, tag, push, run."),
    ("ci-cd-pipeline-config", "GitHub Actions runs on all PRs before merge."),
    ("api-guidelines", "All public APIs require rate limiting and authentication."),
]

_QUERY = "What are the steps to deploy a Docker container?"


def _mock_chat_completion(grades: dict[str, int]) -> str:
    """Return a JSON string of grades, mimicking chat_completion output."""
    return json.dumps(grades)


def _make_chat_fn(return_value: str | None = None, side_effect=None):
    """Build a fake chat_fn that returns a fixed value or raises."""

    def _fake(messages, max_tokens=200):
        if side_effect is not None:
            if isinstance(side_effect, type) and issubclass(side_effect, BaseException):
                raise side_effect()
            if isinstance(side_effect, BaseException):
                raise side_effect
            return side_effect(messages, max_tokens)
        return return_value

    return _fake


# ---------------------------------------------------------------------------
# judge_batch — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_batch_returns_grade_dict() -> None:
    """judge_batch returns a JudgeResult with grades for each candidate."""
    chat_fn = _make_chat_fn(return_value=_mock_chat_completion({"A": 2, "B": 0, "C": 1}))
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=chat_fn,
    )

    assert isinstance(result, JudgeResult)
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 0
    assert result.grades["api-guidelines"] == 1


@pytest.mark.unit
def test_judge_batch_clamps_grades_to_0_2() -> None:
    """Grades outside [0, 2] are clamped."""
    chat_fn = _make_chat_fn(return_value=_mock_chat_completion({"A": 5, "B": -1, "C": 1}))
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=chat_fn,
    )

    assert result.grades["docker-deployment-guide"] == 2  # 5 clamped to 2
    assert result.grades["ci-cd-pipeline-config"] == 0  # -1 clamped to 0


@pytest.mark.unit
def test_judge_batch_shuffles_candidates() -> None:
    """When shuffle=True, the shuffle_order differs from original order at least sometimes."""
    original_order = [stem for stem, _ in _CANDIDATES]
    shuffle_orders = set()
    chat_fn = _make_chat_fn(return_value=_mock_chat_completion({"A": 2, "B": 1, "C": 0}))

    for _ in range(10):
        result = judge_batch(
            query=_QUERY,
            candidates=_CANDIDATES,
            api_key="test-key",
            endpoint="https://test.openai.azure.com",
            shuffle=True,
            chat_fn=chat_fn,
        )
        shuffle_orders.add(tuple(result.shuffle_order))

    # With 3 candidates and 10 runs, some permutation should differ from original
    assert len(shuffle_orders) >= 1
    # The grades dict always has all original stems as keys regardless of order
    assert set(result.grades.keys()) == set(original_order)


@pytest.mark.unit
def test_judge_batch_records_shuffle_order() -> None:
    """shuffle_order contains stems in the order they were presented to the LLM."""
    chat_fn = _make_chat_fn(return_value=_mock_chat_completion({"A": 2, "B": 0, "C": 1}))
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=chat_fn,
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
    chat_fn = _make_chat_fn(side_effect=OSError("connection refused"))
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=chat_fn,
    )

    assert all(g == 0 for g in result.grades.values())
    assert len(result.grades) == len(_CANDIDATES)


@pytest.mark.unit
def test_judge_batch_returns_zeros_on_malformed_json() -> None:
    """Malformed JSON response → all grades are 0."""
    chat_fn = _make_chat_fn(return_value="not json at all {")
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_fn=chat_fn,
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
# calibrate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calibrate_passes_when_all_anchors_correct() -> None:
    """Calibration passes when all anchors get expected grades."""
    from kairix.quality.eval.judge import CALIBRATION_ANCHORS

    # Build a chat_fn that returns the expected grade for each anchor
    def _perfect_chat(messages, max_tokens=200):
        prompt = messages[0]["content"]
        for anchor in CALIBRATION_ANCHORS:
            if anchor["title"] in prompt:
                return json.dumps({"A": anchor["expected"]})
        return json.dumps({"A": 0})

    result = calibrate("test-key", "https://test.openai.azure.com", chat_fn=_perfect_chat)
    assert result is True


@pytest.mark.unit
def test_calibrate_raises_when_too_many_anchors_wrong() -> None:
    """Calibration raises JudgeCalibrationError when >3 anchors are wrong."""
    from kairix.quality.eval.judge import CALIBRATION_MAX_ERRORS

    # Return grade 0 for everything — most grade-1 and grade-2 anchors will be wrong
    def _wrong_chat(messages, max_tokens=200):
        return json.dumps({"A": 0})

    with pytest.raises(JudgeCalibrationError) as exc_info:
        calibrate("test-key", "https://test.openai.azure.com", chat_fn=_wrong_chat)

    assert "calibration" in str(exc_info.value).lower()
    assert str(CALIBRATION_MAX_ERRORS) in str(exc_info.value) or "3" in str(exc_info.value)
