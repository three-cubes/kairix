"""Tests for kairix.agents.research.nodes — individual node functions."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kairix.agents.research.nodes import (
    ClassifyIntentDeps,
    RetrieveDeps,
    classify_intent,
    evaluate_sufficiency,
    refine_query,
    retrieve,
    route_after_evaluation,
    synthesise,
)
from kairix.agents.research.state import ResearcherState


def _state(**overrides) -> ResearcherState:
    base: ResearcherState = {
        "query": "test question",
        "refined_query": "test question",
        "intent": "",
        "retrieved_chunks": [],
        "entities_found": [],
        "gaps": [],
        "synthesis": "",
        "turns": 0,
        "confidence": 0.0,
        "max_turns": 4,
        "error": "",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestClassifyIntent:
    @pytest.mark.unit
    def test_sets_intent(self) -> None:
        result = classify_intent(
            _state(query="who is Jordan Blake"),
            deps=ClassifyIntentDeps(classify_fn=lambda q: MagicMock(value="entity")),
        )
        assert result["intent"] == "entity"

    @pytest.mark.unit
    def test_defaults_to_semantic_on_error(self) -> None:
        def _failing(q):
            raise RuntimeError("boom")

        result = classify_intent(_state(), deps=ClassifyIntentDeps(classify_fn=_failing))
        assert result["intent"] == "semantic"


def _mock_search_result(paths_snippets: list[tuple[str, str]]):
    """Build a mock SearchResult with BudgetedResult-like objects."""
    results = []
    for path, snippet in paths_snippets:
        fused = MagicMock()
        fused.path = path
        budgeted = MagicMock()
        budgeted.result = fused
        budgeted.content = snippet
        results.append(budgeted)
    sr = MagicMock()
    sr.results = results
    return sr


@pytest.mark.unit
class TestRetrieve:
    @pytest.mark.unit
    def test_calls_search(self) -> None:
        mock_search = MagicMock(return_value=_mock_search_result([("a.md", "hello")]))
        result = retrieve(_state(), deps=RetrieveDeps(search_fn=mock_search))
        assert len(result["retrieved_chunks"]) == 1
        assert result["retrieved_chunks"][0]["path"] == "a.md"

    @pytest.mark.unit
    def test_accumulates_across_turns(self) -> None:
        existing = [{"path": "old.md", "snippet": "existing"}]
        mock_search = MagicMock(return_value=_mock_search_result([("new.md", "new")]))
        result = retrieve(
            _state(retrieved_chunks=existing, turns=1),
            deps=RetrieveDeps(search_fn=mock_search),
        )
        assert len(result["retrieved_chunks"]) == 2

    @pytest.mark.unit
    def test_deduplicates_by_path(self) -> None:
        existing = [{"path": "same.md", "snippet": "v1"}]
        mock_search = MagicMock(return_value=_mock_search_result([("same.md", "v2")]))
        result = retrieve(
            _state(retrieved_chunks=existing),
            deps=RetrieveDeps(search_fn=mock_search),
        )
        assert len(result["retrieved_chunks"]) == 1

    @pytest.mark.unit
    def test_higher_budget_on_refinement(self) -> None:
        mock_search = MagicMock(return_value=_mock_search_result([]))
        retrieve(_state(turns=2), deps=RetrieveDeps(search_fn=mock_search))
        mock_search.assert_called_once()
        assert mock_search.call_args.kwargs["budget"] == 5000


@pytest.mark.unit
class TestEvaluateSufficiency:
    @pytest.mark.unit
    def test_parses_llm_response(self) -> None:
        llm_response = json.dumps(
            {
                "confidence": 0.85,
                "sufficient": True,
                "refined_query": None,
                "reasoning": "good",
            }
        )
        mock_backend = MagicMock()
        mock_backend.chat.return_value = llm_response
        result = evaluate_sufficiency(
            _state(retrieved_chunks=[{"path": "a.md", "snippet": "content"}]),
            llm_backend=mock_backend,
        )
        assert result["confidence"] == pytest.approx(0.85)

    @pytest.mark.unit
    def test_returns_zero_on_empty_chunks(self) -> None:
        result = evaluate_sufficiency(_state(retrieved_chunks=[]))
        assert result["confidence"] == pytest.approx(0.0)

    @pytest.mark.unit
    def test_returns_zero_on_llm_failure(self) -> None:
        mock_backend = MagicMock()
        mock_backend.chat.side_effect = RuntimeError("llm down")
        result = evaluate_sufficiency(
            _state(retrieved_chunks=[{"path": "a.md", "snippet": "x"}]),
            llm_backend=mock_backend,
        )
        assert result["confidence"] == pytest.approx(0.0)

    @pytest.mark.unit
    def test_returns_gaps_from_llm(self) -> None:
        """S18-5: evaluate_sufficiency parses and returns gaps from LLM response."""
        llm_response = json.dumps(
            {
                "confidence": 0.6,
                "sufficient": False,
                "refined_query": "better query",
                "gaps": ["missing deployment details", "no cost information"],
                "reasoning": "partial coverage",
            }
        )
        mock_backend = MagicMock()
        mock_backend.chat.return_value = llm_response
        result = evaluate_sufficiency(
            _state(retrieved_chunks=[{"path": "a.md", "snippet": "content"}]),
            llm_backend=mock_backend,
        )
        assert result["gaps"] == ["missing deployment details", "no cost information"]

    @pytest.mark.unit
    def test_returns_empty_gaps_on_failure(self) -> None:
        """S18-5: gaps defaults to empty list on LLM failure."""
        mock_backend = MagicMock()
        mock_backend.chat.side_effect = RuntimeError("llm down")
        result = evaluate_sufficiency(
            _state(retrieved_chunks=[{"path": "a.md", "snippet": "x"}]),
            llm_backend=mock_backend,
        )
        assert result["gaps"] == []


@pytest.mark.unit
class TestRefineQuery:
    @pytest.mark.unit
    def test_increments_turns(self) -> None:
        result = refine_query(_state(turns=1))
        assert result["turns"] == 2


@pytest.mark.unit
class TestSynthesise:
    @pytest.mark.unit
    def test_calls_llm(self) -> None:
        mock_backend = MagicMock()
        mock_backend.chat.return_value = "Here is the answer based on sources."
        result = synthesise(
            _state(retrieved_chunks=[{"path": "doc.md", "snippet": "content"}]),
            llm_backend=mock_backend,
        )
        assert "answer" in result["synthesis"].lower()

    @pytest.mark.unit
    def test_handles_llm_failure(self) -> None:
        mock_backend = MagicMock()
        mock_backend.chat.side_effect = RuntimeError("down")
        result = synthesise(
            _state(retrieved_chunks=[{"path": "a.md"}]),
            llm_backend=mock_backend,
        )
        assert "failed" in result["synthesis"].lower()

    @pytest.mark.unit
    def test_synthesise_carries_confidence_from_state(self) -> None:
        """S18-5: synthesise must re-emit confidence from state so it reaches the final result."""
        mock_backend = MagicMock()
        mock_backend.chat.return_value = "Synthesised answer."
        result = synthesise(
            _state(confidence=0.85, retrieved_chunks=[{"path": "a.md", "snippet": "x"}]),
            llm_backend=mock_backend,
        )
        assert "confidence" in result
        assert result["confidence"] == pytest.approx(0.85)

    @pytest.mark.unit
    def test_synthesise_carries_confidence_on_failure(self) -> None:
        """S18-5: even on LLM failure, confidence from state is preserved."""
        mock_backend = MagicMock()
        mock_backend.chat.side_effect = RuntimeError("down")
        result = synthesise(
            _state(confidence=0.42, retrieved_chunks=[{"path": "a.md"}]),
            llm_backend=mock_backend,
        )
        assert result["confidence"] == pytest.approx(0.42)


@pytest.mark.unit
class TestRouteAfterEvaluation:
    @pytest.mark.unit
    def test_sufficient_routes_to_synthesise(self) -> None:
        assert route_after_evaluation(_state(confidence=0.8)) == "synthesise"

    @pytest.mark.unit
    def test_insufficient_with_turns_left_routes_to_refine(self) -> None:
        assert route_after_evaluation(_state(confidence=0.3, turns=1, max_turns=4)) == "refine_query"

    @pytest.mark.unit
    def test_insufficient_at_max_turns_routes_to_synthesise(self) -> None:
        """When turns are exhausted, synthesise anyway (best effort) instead of giving up."""
        assert route_after_evaluation(_state(confidence=0.3, turns=3, max_turns=4)) == "synthesise"

    @pytest.mark.unit
    def test_threshold_boundary(self) -> None:
        assert route_after_evaluation(_state(confidence=0.5)) == "synthesise"
        assert route_after_evaluation(_state(confidence=0.49)) == "refine_query"
