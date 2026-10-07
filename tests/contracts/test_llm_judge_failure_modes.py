"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`LLMJudge`.

Two methods on the relevance-judge surface (``grade``, ``calibrate``).

Per the Protocol docstring, production implementations promise "never
raise — return all-zero grades on any error". Failure surface:

  * ``grade`` — returns_empty when ``candidates`` is empty (no documents
    to grade); the grades dict is empty.
  * ``grade`` — returns_partial when the judge's answer omits some
    candidates: those default to 0 (the "never raise" contract).
  * ``calibrate`` — raises :class:`JudgeCalibrationError` when the
    calibration anchors fail; downstream (``generate.py``) catches it and
    publishes no eval data.

F43 parity: every body runs over BOTH the real
:class:`kairix.quality.eval.judge.LLMJudge` (ChatBackend injected through
its public ``chat_backend=`` seam) AND the canonical
:class:`tests.fakes.FakeLLMJudge`. Construction differs per impl — the real
judge needs per-call ``api_key`` / ``endpoint`` kwargs (as production
passes them), the fake takes none — so each case supplies ``(judge,
call_kwargs)``; the assertions are shared.

Finding (fake-vs-real drift): the real ``LLMJudge.calibrate`` NEVER
returns False — a failed calibration raises ``JudgeCalibrationError``.
``FakeLLMJudge(calibration_passed=False).calibrate()`` returns False (kept
for its existing callers); the parity body drives the fake through its
``raise_on_failed_calibration=True`` knob, which mirrors the real.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.quality.eval.judge import JudgeCalibrationError, LLMJudge
from tests.fakes import FakeChatBackend, FakeLLMJudge, FakePromptGradingChatBackend

pytestmark = pytest.mark.contract

_IDS = ["real", "fake"]
_CREDS: dict[str, str] = {"api_key": "contract-key", "endpoint": "https://judge.invalid"}

_JudgeCase = Callable[[], tuple[Any, dict[str, str]]]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: (LLMJudge(chat_backend=FakeChatBackend(responses=[])), _CREDS),
        lambda: (FakeLLMJudge(), {}),
    ],
    ids=_IDS,
)
def test_grade_returns_empty_when_candidates_list_empty(factory: _JudgeCase) -> None:
    """Empty candidates list yields an empty grades dict — the judge
    must not invent scores for non-existent documents.

    Sabotage proof: in ``LLMJudge.grade`` change the early-return
    ``grades={}`` to ``grades={"phantom-stem": 0}``. Re-ran: the real
    leg's ``== {}`` assertion fails. Restored.
    """
    judge, call_kwargs = factory()
    result = judge.grade("any query", [], **call_kwargs)
    assert result.grades == {}, f"empty candidates must yield empty grades; got {result.grades!r}"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: (LLMJudge(chat_backend=FakePromptGradingChatBackend(grades_by_stem={"known-stem": 2})), _CREDS),
        lambda: (FakeLLMJudge(grades_by_query={"q1": {"known-stem": 2}}), {}),
    ],
    ids=_IDS,
)
def test_grade_returns_partial_when_configured_grades_omit_some_candidates(factory: _JudgeCase) -> None:
    """The "never raise — return zeros on error" contract: candidates
    the judge's answer omits get 0, not an exception.

    Sabotage proof: in ``LLMJudge.grade`` change
    ``grades[stem] = label_grades.get(label, 0)`` to
    ``grades[stem] = label_grades[label]``. Re-ran: the real leg raises
    KeyError for the omitted stem and fails. Restored.
    """
    judge, call_kwargs = factory()
    result = judge.grade("q1", [("known-stem", "body1"), ("unknown-stem", "body2")], **call_kwargs)
    # Graded stem keeps its grade; omitted stem defaults to 0 (the
    # "return zeros on error" contract surface).
    assert result.grades == {"known-stem": 2, "unknown-stem": 0}, (
        f"missing stems must default to 0; got {result.grades!r}"
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: (LLMJudge(chat_backend=FakeChatBackend(raise_on_call=RuntimeError("judge backend down"))), _CREDS),
        lambda: (FakeLLMJudge(calibration_passed=False, raise_on_failed_calibration=True), {}),
    ],
    ids=_IDS,
)
def test_calibrate_raises_typed_error_when_calibration_fails(factory: _JudgeCase) -> None:
    """``calibrate`` raises :class:`JudgeCalibrationError` when the anchor
    suite trips (here: the backend errors, so every anchor grades 0 and
    10/15 anchors are wrong) — downstream callers catch it and publish
    no eval data.

    Sabotage proof: in ``LLMJudge.calibrate`` change the threshold check
    to ``if len(errors) > len(CALIBRATION_ANCHORS):``. Re-ran: the real
    leg returns True and ``pytest.raises`` sees nothing. Restored.
    """
    judge, call_kwargs = factory()
    with pytest.raises(JudgeCalibrationError):
        judge.calibrate(**call_kwargs)
