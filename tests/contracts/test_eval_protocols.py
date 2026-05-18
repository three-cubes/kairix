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
        """The provider-backed eval ``ChatBackend`` adapter satisfies the protocol.

        :class:`~kairix.quality.eval.chat_backend.ProviderEvalChatBackend`
        wraps a :class:`kairix.providers.Provider` plugin and exposes
        the ``ChatBackend.complete`` shape. The adapter is constructed
        directly with a ``FakeProvider`` so the test does not depend on
        ``kairix.config.yaml``.

        Sabotage proof: dropping the ``complete`` method (or changing
        its signature off the protocol shape) would fail the
        ``isinstance(...)`` assertion since ``ChatBackend`` is
        ``runtime_checkable``.
        """
        from kairix.quality.eval.chat_backend import ProviderEvalChatBackend
        from tests.fakes import FakeProvider

        backend = ProviderEvalChatBackend(FakeProvider(chat_reply="ok"))
        assert isinstance(backend, ChatBackend)

    def test_provider_eval_chat_backend_routes_complete_through_provider(self) -> None:
        """``ProviderEvalChatBackend.complete`` delegates to ``Provider.chat``.

        Pins the wiring contract: the adapter must call the configured
        provider's ``chat`` method (with messages built from ``system`` +
        ``prompt``) and return its reply verbatim.

        Sabotage proof: rerouting ``complete`` to a different provider
        method (e.g. ``embed``) or hard-coding an empty string would
        fail the ``"ok"`` equality assertion AND the call-count check.
        """
        from kairix.quality.eval.chat_backend import ProviderEvalChatBackend
        from tests.fakes import FakeProvider

        provider = FakeProvider(chat_reply="ok")
        backend = ProviderEvalChatBackend(provider)

        reply = backend.complete(
            "the prompt",
            api_key="ignored",  # pragma: allowlist secret
            endpoint="ignored",
            deployment="ignored",
            system="you are a judge",
        )

        assert reply == "ok"
        assert len(provider.chat_calls) == 1
        messages = provider.chat_calls[0]["messages"]
        assert messages[0] == {"role": "system", "content": "you are a judge"}
        assert messages[1] == {"role": "user", "content": "the prompt"}

    def test_provider_eval_chat_backend_swallows_provider_errors(self) -> None:
        """The adapter returns ``""`` when the provider raises.

        Matches the never-raises contract that eval callers (LLMJudge,
        QueryGenerator) depend on so they can short-circuit on an empty
        reply rather than aborting the surrounding workflow. Uses
        a hand-rolled provider object that raises on ``chat`` — the
        ``FakeProvider`` in ``tests/fakes.py`` exposes ``embed_raises``
        but not a chat-side raise hook.

        Sabotage proof: removing the try/except around ``provider.chat``
        would let the RuntimeError propagate and fail the assertion that
        ``reply == ""``.
        """
        from kairix.providers import ProviderHealth
        from kairix.quality.eval.chat_backend import ProviderEvalChatBackend

        class _BoomProvider:
            name = "boom"

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[] for _ in texts]

            def chat(self, messages: list[dict[str, object]], *, max_tokens: int = 800) -> str:
                del messages, max_tokens
                raise RuntimeError("provider broke")

            def dimension(self) -> int:
                return 0

            def healthcheck(self) -> ProviderHealth:
                return ProviderHealth(ok=False, endpoint="boom://", cold_ms=0.0, warm_ms=0.0, error="boom")

        backend = ProviderEvalChatBackend(_BoomProvider())
        reply = backend.complete("p", api_key="", endpoint="", deployment="")
        assert reply == ""


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
