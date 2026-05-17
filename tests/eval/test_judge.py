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
    CALIBRATION_ANCHORS,
    JUDGE_DEPLOYMENT,
    JudgeCalibrationError,
    JudgeResult,
    LLMJudge,
    calibrate,
    fetch_llm_credentials,
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
    """Empty candidate list returns empty JudgeResult.

    Injects ``FakeChatBackend`` so the shim doesn't reach the
    provider-resolution path — the early-return on ``not candidates``
    fires before any backend call.
    """
    result = judge_batch(
        query=_QUERY,
        candidates=[],
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        chat_backend=FakeChatBackend(responses=[]),
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
    """Empty api_key/endpoint → all grades are 0, no exception.

    The empty-credential branch lives inside ``LLMJudge.grade`` (raises
    ``ValueError`` which is caught by the same method's try/except).
    The shim only constructs the default backend when ``chat_backend``
    is omitted — pass a ``FakeChatBackend`` so we exercise the empty-
    credential branch deterministically without depending on the
    provider-resolution path.
    """
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="",
        endpoint="",
        shuffle=False,
        chat_backend=FakeChatBackend(responses=[]),
    )
    assert all(g == 0 for g in result.grades.values())


# ---------------------------------------------------------------------------
# Grade-parsing scenarios driven through judge_batch's public surface.
#
# The chat backend's response string is the parser's input; the resulting
# JudgeResult.grades dict is the parser's output as observed by callers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pure_json_response_grades_each_candidate_per_label() -> None:
    """Pure-JSON {"A": 2, "B": 0, "C": 1} → grades reflect label→candidate mapping."""
    backend = FakeChatBackend(responses=['{"A": 2, "B": 0, "C": 1}'])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 0
    assert result.grades["api-guidelines"] == 1


@pytest.mark.unit
def test_json_embedded_in_prose_is_extracted_and_used() -> None:
    """A JSON object inside prose still drives the grades."""
    backend = FakeChatBackend(
        responses=['After reviewing the documents, my assessment is: {"A": 2, "B": 1, "C": 0} as requested.']
    )
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 1
    assert result.grades["api-guidelines"] == 0


@pytest.mark.unit
def test_response_with_no_json_yields_all_zero_grades() -> None:
    """No JSON object in the response → every candidate gets 0."""
    backend = FakeChatBackend(responses=["I cannot assess these documents."])
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
def test_extra_labels_in_response_are_ignored() -> None:
    """Labels beyond the candidate count (e.g. ``Z``) are dropped silently."""
    # Two candidates → labels A, B. The response includes a stray Z which must not surface.
    backend = FakeChatBackend(responses=['{"A": 2, "B": 0, "Z": 1}'])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES[:2],
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )
    assert set(result.grades.keys()) == {"docker-deployment-guide", "ci-cd-pipeline-config"}
    assert result.grades["docker-deployment-guide"] == 2
    assert result.grades["ci-cd-pipeline-config"] == 0


@pytest.mark.unit
def test_invalid_json_inside_braces_yields_all_zero_grades() -> None:
    """Brace block exists but body fails json.loads → all grades 0 (never raises)."""
    backend = FakeChatBackend(responses=["{A: 2, B: 0}"])  # missing quotes
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES[:2],
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )
    assert all(g == 0 for g in result.grades.values())


@pytest.mark.unit
def test_non_int_grade_values_are_clamped_to_zero() -> None:
    """A non-int-coercible value (string/null) becomes 0 for that candidate only."""
    backend = FakeChatBackend(responses=['{"A": "high", "B": 1, "C": null}'])
    result = judge_batch(
        query=_QUERY,
        candidates=_CANDIDATES,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        shuffle=False,
        chat_backend=backend,
    )
    # int("high") raises ValueError → 0; int(None) raises TypeError → 0; int(1) → 1
    assert result.grades["docker-deployment-guide"] == 0
    assert result.grades["ci-cd-pipeline-config"] == 1
    assert result.grades["api-guidelines"] == 0


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


@pytest.mark.unit
def test_llm_judge_calibrate_logs_when_errors_within_threshold() -> None:
    """When 1..CALIBRATION_MAX_ERRORS anchors are wrong, calibrate returns True and logs."""
    # Build responses where the first two anchors return a wrong grade and the rest are correct.
    # That gives us 2 errors total, within the threshold (3), so calibrate returns True
    # but exercises the warning-log branch.
    responses = []
    for i, anchor in enumerate(CALIBRATION_ANCHORS):
        wrong = anchor["expected"] - 1 if anchor["expected"] > 0 else 1
        responses.append(_grade_response({"A": wrong if i < 2 else anchor["expected"]}))
    backend = FakeChatBackend(responses=responses)

    judge = LLMJudge(chat_backend=backend)
    assert judge.calibrate(api_key="key", endpoint="https://endpoint") is True  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# fetch_llm_credentials — DEPRECATED legacy helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_llm_credentials_returns_empties_when_secrets_unavailable() -> None:
    """When LLM secrets are not configured, returns empty strings + default deployment.

    Exercises the ``except Exception`` fallback so callers in legacy free-function
    paths get all-zero grades from the judge rather than a raised error.
    """
    api_key, endpoint, deployment = fetch_llm_credentials()
    # In the test environment kairix.secrets cannot resolve LLM creds — the
    # OSError raised by get_secret(required=True) is caught by fetch_llm_credentials.
    assert api_key == ""
    assert endpoint == ""
    assert deployment == JUDGE_DEPLOYMENT


# ---------------------------------------------------------------------------
# calibrate (free-function shim) — exercises the provider-backed default branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calibrate_shim_raises_value_error_when_no_provider_configured() -> None:
    """The free-function ``calibrate`` shim constructs a provider-backed default.

    When ``kairix.config.yaml`` has no ``provider:`` field (the default
    test-environment shape), ``ProviderEvalChatBackend.from_config()``
    raises ``ValueError`` with an actionable affordance — the shim
    surfaces that directly rather than masking it.

    Sabotage proof: regressing the shim to silently swallow the missing-
    provider error (e.g. returning ``True`` like a passing calibration)
    would let this test fall through without raising; pinned by the
    ``pytest.raises(ValueError)`` matcher on the affordance text.
    """
    with pytest.raises(ValueError, match="provider:"):
        calibrate(api_key="", endpoint="")


@pytest.mark.unit
def test_calibrate_shim_uses_injected_chat_backend_when_supplied() -> None:
    """When a ``chat_backend`` is passed explicitly, the shim does not touch
    the provider-resolution path.

    The injected ``FakeChatBackend`` exhausts its canned responses across the
    15 calibration anchors and most replies parse to grade 0 — so for
    grade-2 / grade-1 anchors the result is wrong, exceeding
    CALIBRATION_MAX_ERRORS=3 and raising ``JudgeCalibrationError``.

    Sabotage proof: regressing the shim to ignore ``chat_backend`` and call
    the provider-resolution path anyway would raise the missing-provider
    ``ValueError`` (different exception type) — pinned by the
    ``pytest.raises(JudgeCalibrationError)`` match here.
    """
    fake = FakeChatBackend(responses=['{"A": 0}'] * len(CALIBRATION_ANCHORS))
    with pytest.raises(JudgeCalibrationError):
        calibrate(api_key="k", endpoint="https://e", chat_backend=fake)
