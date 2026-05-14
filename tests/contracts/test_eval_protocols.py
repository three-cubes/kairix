"""Contract tests for the eval-module protocols (#143 Phase 1).

Verifies that the FakeXxx implementations in tests/fakes.py satisfy each
protocol via runtime isinstance() checks. Production class conformance
(LLMJudge wrapping judge_batch, etc.) is added in Phase 2a/2b — those PRs
extend these contract tests with their concrete classes.
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import (
    ChatBackend,
    LLMJudge,
    QueryGenerator,
    Retriever,
)
from tests.fakes import (
    FakeChatBackend,
    FakeLLMJudge,
    FakeQueryGenerator,
    FakeRetriever,
)


@pytest.mark.contract
class TestChatBackendContract:
    def test_fake_chat_backend_satisfies_protocol(self) -> None:
        assert isinstance(FakeChatBackend(), ChatBackend)

    def test_fake_chat_backend_returns_canned_response(self) -> None:
        backend = FakeChatBackend(responses=["hello", "world"])
        assert backend.complete("p1", api_key="k", endpoint="e", deployment="d") == "hello"
        assert backend.complete("p2", api_key="k", endpoint="e", deployment="d") == "world"

    def test_fake_chat_backend_records_calls_for_inspection(self) -> None:
        backend = FakeChatBackend(responses=["x"])
        backend.complete("the prompt", api_key="K", endpoint="E", deployment="D", temperature=0.5)
        assert len(backend.calls) == 1
        assert backend.calls[0]["prompt"] == "the prompt"
        assert backend.calls[0]["temperature"] == pytest.approx(0.5)

    def test_fake_chat_backend_raises_when_responses_exhausted(self) -> None:
        backend = FakeChatBackend(responses=["only-one"])
        backend.complete("p1", api_key="k", endpoint="e", deployment="d")
        with pytest.raises(IndexError, match="ran out of canned responses"):
            backend.complete("p2", api_key="k", endpoint="e", deployment="d")

    def test_fake_chat_backend_raises_configured_error(self) -> None:
        boom = ValueError("No API credentials")
        backend = FakeChatBackend(raise_on_call=boom)
        with pytest.raises(ValueError, match="No API credentials"):
            backend.complete("p", api_key="k", endpoint="e", deployment="d")

    def test_production_chat_backend_satisfies_protocol(self) -> None:
        """The production chat-backend factory returns a ``ChatBackend``.

        Phase 2a adds the adapter so production callers can inject a
        ``ChatBackend`` rather than calling ``chat_completion`` directly.

        F5-clean: drive through the public ``default_chat_backend()``
        factory rather than importing the concrete adapter class from the
        private ``kairix._azure`` module. The concrete class is an
        implementation detail; what callers depend on is the protocol
        surface.
        """
        from kairix.quality.eval.generate import default_chat_backend

        backend = default_chat_backend()
        assert isinstance(backend, ChatBackend)


@pytest.mark.contract
class TestLLMJudgeContract:
    def test_fake_llm_judge_satisfies_protocol(self) -> None:
        assert isinstance(FakeLLMJudge(), LLMJudge)

    def test_fake_llm_judge_returns_zero_grades_for_unknown_query(self) -> None:
        judge = FakeLLMJudge()
        result = judge.grade("unseen", [("doc-a", "snippet"), ("doc-b", "snippet")])
        assert result.grades == {"doc-a": 0, "doc-b": 0}
        assert result.shuffle_order == ("doc-a", "doc-b")

    def test_fake_llm_judge_returns_configured_grades(self) -> None:
        judge = FakeLLMJudge(grades_by_query={"q1": {"doc-a": 2, "doc-b": 1}})
        result = judge.grade("q1", [("doc-a", "x"), ("doc-b", "y"), ("doc-c", "z")])
        assert result.grades == {"doc-a": 2, "doc-b": 1, "doc-c": 0}

    def test_fake_llm_judge_calibrate_returns_configured_pass(self) -> None:
        assert FakeLLMJudge(calibration_passed=True).calibrate() is True
        assert FakeLLMJudge(calibration_passed=False).calibrate() is False

    def test_production_llm_judge_satisfies_protocol(self) -> None:
        """The production ``LLMJudge`` class in judge.py satisfies the protocol.

        Phase 2a wraps the free functions ``judge_batch`` / ``calibrate`` in a
        class injected with a ``ChatBackend``. This test asserts the class is
        a structural match for the ``LLMJudge`` protocol.
        """
        from kairix.quality.eval.judge import LLMJudge as ProductionLLMJudge

        production = ProductionLLMJudge(chat_backend=FakeChatBackend())
        assert isinstance(production, LLMJudge)


@pytest.mark.contract
class TestQueryGeneratorContract:
    def test_fake_query_generator_satisfies_protocol(self) -> None:
        assert isinstance(FakeQueryGenerator(), QueryGenerator)

    def test_fake_query_generator_returns_empty_for_unknown_title(self) -> None:
        gen = FakeQueryGenerator()
        assert gen.generate("unknown.md", "body", n=5, categories=["recall"]) == []

    def test_fake_query_generator_returns_configured_queries_capped_at_n(self) -> None:
        gen = FakeQueryGenerator(queries_by_title={"deploy.md": ["q1", "q2", "q3"]})
        assert gen.generate("deploy.md", "body", n=2, categories=["recall"]) == ["q1", "q2"]


@pytest.mark.contract
class TestRetrieverContract:
    def test_fake_retriever_satisfies_protocol(self) -> None:
        assert isinstance(FakeRetriever(), Retriever)

    def test_fake_retriever_returns_empty_for_unknown_query(self) -> None:
        retriever = FakeRetriever()
        result = retriever.retrieve("unknown query")
        assert result.results == []
        assert result.vec_failed is False

    def test_fake_retriever_returns_configured_result(self) -> None:
        from types import SimpleNamespace

        canned = SimpleNamespace(results=[{"path": "doc.md"}], vec_failed=False)
        retriever = FakeRetriever(results_by_query={"q1": canned})
        assert retriever.retrieve("q1") is canned

    def test_fake_retriever_records_calls(self) -> None:
        retriever = FakeRetriever()
        retriever.retrieve("q", collections=["c1", "c2"], cfg={"factor": 1.0})
        assert len(retriever.calls) == 1
        assert retriever.calls[0]["query"] == "q"
        assert retriever.calls[0]["collections"] == ["c1", "c2"]
