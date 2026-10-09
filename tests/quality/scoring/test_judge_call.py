"""Direct tests for :mod:`kairix.quality.scoring.judge_call` — the shared judge protocol."""

from __future__ import annotations

import pytest

from kairix.quality.scoring.judge_call import call_judge, parse_judge_score
from kairix.quality.scoring.types import (
    JUDGE_FAILURE_BACKEND_ERROR,
    JUDGE_FAILURE_UNPARSEABLE,
    JudgeFailedError,
)

pytestmark = pytest.mark.unit

_SENTINEL = "credential-sentinel-7f3a"  # stand-in for a secret an exception message could echo


class TestCallJudge:
    def test_returns_the_reply(self) -> None:
        assert call_judge(lambda: "0.7") == "0.7"

    def test_backend_exception_is_backend_error_with_class_name_only(self) -> None:
        """Sabotage-proof: drop the ``except`` in ``call_judge`` — the raw
        TimeoutError escapes and ``pytest.raises(JudgeFailedError)`` fails."""

        def _boom() -> str:
            raise TimeoutError(f"timed out; Authorization: Bearer {_SENTINEL}")

        with pytest.raises(JudgeFailedError) as excinfo:
            call_judge(_boom)
        assert excinfo.value.reason == JUDGE_FAILURE_BACKEND_ERROR
        assert excinfo.value.detail == "backend raised TimeoutError"
        assert _SENTINEL not in str(excinfo.value)

    def test_formatted_traceback_never_carries_the_backend_message(self) -> None:
        """An uncaught JudgeFailedError (e.g. propagated by
        ``LLMJudgeScorer.score``) must not render the provider exception —
        neither as ``__cause__`` nor as implicit ``__context__``.

        Sabotage-proof: raise with ``from exc`` (or no ``from``) in
        ``call_judge`` — the formatted traceback includes the sentinel."""
        import traceback

        def _boom() -> str:
            raise RuntimeError(f"401 for api-key={_SENTINEL}")

        with pytest.raises(JudgeFailedError) as excinfo:
            call_judge(_boom)
        rendered = "".join(traceback.format_exception(excinfo.value))
        assert _SENTINEL not in rendered
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__

    @pytest.mark.parametrize("reply", ["", "   \n"])
    def test_blank_reply_is_backend_error(self, reply: str) -> None:
        """Sabotage-proof: drop the blank-reply check — ``call_judge`` returns
        the blank reply instead of raising."""
        with pytest.raises(JudgeFailedError) as excinfo:
            call_judge(lambda: reply)
        assert excinfo.value.reason == JUDGE_FAILURE_BACKEND_ERROR


class TestParseJudgeScore:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("0.85", 0.85),
            (" 1.0\n", 1.0),
            ("Score: 0.5 (partial)", 0.5),
            ("0.4, maybe", 0.4),
            ("2.0", 1.0),
            ("-0.3", 0.0),
        ],
    )
    def test_parses_and_clamps(self, reply: str, expected: float) -> None:
        """First parseable float wins; out-of-range values clamp to [0, 1].

        Sabotage-proof: drop the clamp — "2.0" and "-0.3" escape the interval.
        """
        assert parse_judge_score(reply) == pytest.approx(expected)

    @pytest.mark.parametrize("reply", ["not a number", "", "nan", "inf", "-inf", "score: NaN"])
    def test_unparseable_or_non_finite_raises(self, reply: str) -> None:
        """No float, or a non-finite one, is ``unparseable_response`` — and the
        reply text never appears in the detail.

        Sabotage-proof: drop the ``math.isfinite`` check — the nan / inf legs
        return a number and fail.
        """
        with pytest.raises(JudgeFailedError) as excinfo:
            parse_judge_score(reply)
        assert excinfo.value.reason == JUDGE_FAILURE_UNPARSEABLE
        if reply.strip() and reply.strip().lower() not in {"nan", "inf", "-inf"}:
            assert reply.strip() not in excinfo.value.detail

    def test_non_numeric_detail_reports_length_only(self) -> None:
        with pytest.raises(JudgeFailedError) as excinfo:
            parse_judge_score("  not a number  ")
        assert excinfo.value.detail == "non-numeric reply (12 chars)"
