"""Unit tests for LLMJudgeScorer + prompt/parse helpers.

Pins:

* happy-path: a 1.0 judge response → score 1.0; FakeLLMBackend
  captures the prompt for inspection.
* judge failure (backend error, empty / malformed / nan / inf reply) →
  typed ``JudgeFailedError``, never a 0.0 score; the aggregator then
  averages the judge metric over judged queries only.
* missing expected_answer → 0.0 with reason; no LLM call.
* missing synthesised_answer → 0.0 with reason; no LLM call.
* error path → 0.0 with reason.
* sabotage: a backend that returns "0.0" lands score 0.0; a backend
  returning "1.0" lands 1.0 — proves the score is actually parsed
  from the response, not hardcoded.
* prompt format: pins the system message + user message shape — drift
  in this shape would change the judge calibration silently.
"""

from __future__ import annotations

import pytest

from kairix.quality.scoring.aggregator import aggregate_by_category
from kairix.quality.scoring.llm_judge import (
    LLMJudgeScorer,
    build_judge_prompt,
    parse_judge_score,
)
from kairix.quality.scoring.types import (
    JUDGE_FAILURE_BACKEND_ERROR,
    JUDGE_FAILURE_UNPARSEABLE,
    JudgeFailedError,
    QueryRunResult,
    ScorerResult,
)
from tests.fakes import FakeLLMBackend

pytestmark = pytest.mark.unit


def _run(answer: str | None = "the answer") -> QueryRunResult:
    return QueryRunResult(
        query_id="J-01",
        category="conceptual",
        query_text="What is the answer?",
        synthesised_answer=answer,
    )


class TestParseJudgeScore:
    def test_clean_float_string(self) -> None:
        assert parse_judge_score("0.85") == pytest.approx(0.85, abs=1e-6)

    def test_clean_float_one(self) -> None:
        assert parse_judge_score("1.0") == 1.0

    def test_first_float_in_noisy_string(self) -> None:
        assert parse_judge_score("score: 0.5 (partial)") == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.parametrize("reply", ["not-a-number", "", "   ", "nan", "inf", "-inf"])
    def test_unparseable_or_non_finite_raises_typed_failure(self, reply: str) -> None:
        """A reply with no finite float is a judge failure, never 0.0.

        Sabotage-proof: restore the old fallbacks in ``parse_judge_score``
        (``return 0.0`` when no float is found, no ``math.isfinite`` guard)
        — every leg returns a number instead of raising and fails. Restored.
        """
        with pytest.raises(JudgeFailedError) as excinfo:
            parse_judge_score(reply)
        assert excinfo.value.reason == JUDGE_FAILURE_UNPARSEABLE
        if reply.strip() in ("not-a-number", ""):
            assert f"({len(reply.strip())} chars)" in excinfo.value.detail

    def test_negative_clamped_to_zero(self) -> None:
        assert parse_judge_score("-0.5") == 0.0

    def test_above_one_clamped(self) -> None:
        assert parse_judge_score("2.0") == 1.0


class TestBuildJudgePrompt:
    def test_returns_system_then_user_messages(self) -> None:
        prompt = build_judge_prompt(question="Q?", expected="E", context="C")
        assert len(prompt) == 2
        assert prompt[0]["role"] == "system"
        assert prompt[1]["role"] == "user"

    def test_system_prompt_carries_score_scale(self) -> None:
        # Sabotage-proof: change "0.0 to 1.0" → "0 to 100" in the prompt
        # and this assertion catches the drift; calibration would change.
        prompt = build_judge_prompt(question="Q", expected="E", context="C")
        assert "0.0 and 1.0" in prompt[0]["content"]
        assert "exact match" in prompt[0]["content"]

    def test_user_prompt_embeds_question_expected_context(self) -> None:
        prompt = build_judge_prompt(
            question="What's the date?",
            expected="2026-05-21",
            context="The date is 2026-05-21.",
        )
        user_content = prompt[1]["content"]
        assert "What's the date?" in user_content
        assert "2026-05-21" in user_content
        assert "The date is 2026-05-21." in user_content


class TestLLMJudgeScorer:
    def test_happy_path_returns_parsed_score(self) -> None:
        # Sabotage-proof: change chat_response → "0.0" and the score
        # must drop. Executed by the next test.
        llm = FakeLLMBackend(chat_response="1.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="the answer")
        result = scorer.score(_run("the answer"))
        assert result.score == 1.0
        assert result.metric_name == "judge"
        assert result.details["raw_response"] == "1.0"

    def test_response_shape_drives_score(self) -> None:
        # Executed sabotage partner: low-score response → low score.
        llm = FakeLLMBackend(chat_response="0.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="X")
        result = scorer.score(_run("Y"))
        assert result.score == 0.0

    def test_missing_expected_answer_short_circuits(self) -> None:
        llm = FakeLLMBackend(chat_response="1.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer=None)
        result = scorer.score(_run("answer"))
        assert result.score == 0.0
        assert result.details["reason"] == "no_expected_answer"
        # Crucially: no LLM call made.
        assert llm.chat_calls == []

    def test_missing_synthesised_answer_short_circuits(self) -> None:
        llm = FakeLLMBackend(chat_response="1.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="E")
        result = scorer.score(_run(answer=None))
        assert result.score == 0.0
        assert result.details["reason"] == "no_synthesised_answer"
        assert llm.chat_calls == []

    def test_error_path_short_circuits(self) -> None:
        llm = FakeLLMBackend(chat_response="1.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="E")
        run = QueryRunResult(
            query_id="x",
            category="recall",
            query_text="q",
            synthesised_answer="ans",
            error="backend died",
        )
        result = scorer.score(run)
        assert result.score == 0.0
        assert result.details["reason"] == "query_run_failed"
        assert llm.chat_calls == []

    def test_malformed_response_raises_typed_failure(self) -> None:
        """A malformed reply raises ``JudgeFailedError`` — no 0.0 ScorerResult.

        Sabotage-proof: make ``parse_judge_score`` return 0.0 when no float
        is found — ``score`` returns a 0.0 result and pytest.raises fails.
        Restored.
        """
        llm = FakeLLMBackend(chat_response="not-a-float")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="E")
        with pytest.raises(JudgeFailedError) as excinfo:
            scorer.score(_run("ans"))
        assert excinfo.value.reason == JUDGE_FAILURE_UNPARSEABLE
        # Did call the LLM (unlike the short-circuit branches).
        assert len(llm.chat_calls) == 1

    def test_sequence_input_rejected(self) -> None:
        llm = FakeLLMBackend(chat_response="1.0")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="E")
        result = scorer.score([_run("a")])
        assert result.score == 0.0
        assert "sequence" in result.details["reason"]

    def test_name_property(self) -> None:
        scorer = LLMJudgeScorer(llm=FakeLLMBackend(), expected_answer="x")
        assert scorer.name == "judge"

    def test_prompt_carries_query_expected_and_synthesised(self) -> None:
        llm = FakeLLMBackend(chat_response="0.5")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="reference text")
        run = QueryRunResult(
            query_id="x",
            category="conceptual",
            query_text="the question",
            synthesised_answer="actual answer",
        )
        scorer.score(run)
        [call] = llm.chat_calls
        user_msg = call["messages"][1]["content"]
        assert "the question" in user_msg
        assert "reference text" in user_msg
        assert "actual answer" in user_msg
        assert call["max_tokens"] == 8

    def test_custom_metric_name(self) -> None:
        llm = FakeLLMBackend(chat_response="0.5")
        scorer = LLMJudgeScorer(llm=llm, expected_answer="E", metric_name="judge_correctness")
        result = scorer.score(_run("ans"))
        assert result.metric_name == "judge_correctness"

    @pytest.mark.parametrize("reply", ["", "  \n"])
    def test_blank_reply_raises_backend_error(self, reply: str) -> None:
        """``LLMBackend.chat`` returns ``""`` on failure — a blank reply is a
        backend failure, not a 0.0 verdict.

        Sabotage-proof: drop the blank-reply ``JudgeFailedError`` in
        ``LLMJudgeScorer.score`` (or change its ``or`` to ``and``) — the
        reply reaches ``parse_judge_score`` and is reported as
        ``unparseable_response``. Restored.
        """
        scorer = LLMJudgeScorer(llm=FakeLLMBackend(chat_response=reply), expected_answer="E")
        with pytest.raises(JudgeFailedError) as excinfo:
            scorer.score(_run("ans"))
        assert excinfo.value.reason == JUDGE_FAILURE_BACKEND_ERROR

    def test_raising_backend_raises_backend_error(self) -> None:
        """A backend that raises is reported as ``backend_error``.

        Sabotage-proof: drop the ``except Exception`` around ``self._llm.chat``
        — the raw TimeoutError escapes and ``pytest.raises(JudgeFailedError)``
        fails. Restored.
        """
        scorer = LLMJudgeScorer(
            llm=FakeLLMBackend(chat_raises=TimeoutError("timed out; api-key credential-sentinel-7f3a")),
            expected_answer="E",
        )
        with pytest.raises(JudgeFailedError) as excinfo:
            scorer.score(_run("ans"))
        assert excinfo.value.reason == JUDGE_FAILURE_BACKEND_ERROR
        # Class name only — the exception message is never copied (it can carry secrets).
        assert excinfo.value.detail == "backend raised TimeoutError"
        assert "credential-sentinel-7f3a" not in str(excinfo.value)

    def test_failed_judgement_is_excluded_from_category_mean(self) -> None:
        """Fan two queries out to the judge: one judged 1.0, one whose reply
        is malformed. The failure yields no ``ScorerResult``, so the
        category's judge mean is 1.0 over the judged query — not 0.5.

        Sabotage-proof: make ``parse_judge_score`` return 0.0 when no float
        is found — the second query scores 0.0 and the mean drops to 0.5.
        Restored.
        """
        runs = [
            (_run("ans"), FakeLLMBackend(chat_response="1.0")),
            (_run("ans"), FakeLLMBackend(chat_response="garbled")),
        ]
        pairs: list[tuple[QueryRunResult, list[ScorerResult]]] = []
        failures = 0
        for run, llm in runs:
            try:
                results = [LLMJudgeScorer(llm=llm, expected_answer="E").score(run)]
            except JudgeFailedError:
                failures += 1
                results = []
            pairs.append((run, results))

        [aggregate] = aggregate_by_category(pairs)
        assert failures == 1
        assert aggregate.metrics["judge"] == pytest.approx(1.0)
