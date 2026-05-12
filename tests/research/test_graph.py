"""Tests for kairix.agents.research.graph — full graph execution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


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
def test_run_research_sufficient_first_pass() -> None:
    """Graph completes in one pass when results are sufficient."""
    from kairix.agents.research.graph import ResearchGraphDeps, run_research

    mock_backend = MagicMock()
    # evaluate_sufficiency returns high confidence
    mock_backend.chat.side_effect = [
        json.dumps(
            {
                "confidence": 0.9,
                "sufficient": True,
                "refined_query": None,
                "reasoning": "good",
            }
        ),
        "Synthesised answer from search results.",
    ]

    result = run_research(
        "simple question",
        max_turns=4,
        deps=ResearchGraphDeps(
            search_fn=lambda **kwargs: _mock_search_result([("doc.md", "content")]),
            classify_fn=lambda q: MagicMock(value="semantic"),
            llm_backend=mock_backend,
        ),
    )

    assert result["confidence"] >= 0.7
    assert result["synthesis"] != ""
    assert result["turns"] == 0  # no refinement needed


@pytest.mark.unit
def test_run_research_refines_then_succeeds() -> None:
    """Graph refines query and succeeds on second pass."""
    from kairix.agents.research.graph import ResearchGraphDeps, run_research

    mock_backend = MagicMock()
    mock_backend.chat.side_effect = [
        # First eval: insufficient
        json.dumps(
            {
                "confidence": 0.3,
                "sufficient": False,
                "refined_query": "better query",
                "reasoning": "need more",
            }
        ),
        # Second eval: sufficient
        json.dumps(
            {
                "confidence": 0.9,
                "sufficient": True,
                "refined_query": None,
                "reasoning": "good now",
            }
        ),
        # Synthesis
        "Final answer with citations.",
    ]

    call_count = 0

    def mock_search(**kwargs):
        nonlocal call_count
        call_count += 1
        return _mock_search_result([(f"doc{call_count}.md", f"content {call_count}")])

    result = run_research(
        "complex question",
        max_turns=4,
        deps=ResearchGraphDeps(
            search_fn=mock_search,
            classify_fn=lambda q: MagicMock(value="semantic"),
            llm_backend=mock_backend,
        ),
    )

    assert result["turns"] == 1  # refined once
    assert result["confidence"] >= 0.7
    assert len(result["retrieved_chunks"]) == 2  # accumulated from both passes


@pytest.mark.unit
def test_run_research_synthesises_best_effort_after_max_turns() -> None:
    """Graph synthesises a best-effort answer when max turns exhausted at low confidence."""
    from kairix.agents.research.graph import ResearchGraphDeps, run_research

    mock_backend = MagicMock()
    # Evaluation always returns low confidence; final call is synthesis
    eval_low = {
        "confidence": 0.2,
        "sufficient": False,
        "refined_query": "still trying",
        "reasoning": "not enough",
    }
    eval_low2 = {
        "confidence": 0.3,
        "sufficient": False,
        "refined_query": "still trying",
        "reasoning": "not enough",
    }
    mock_backend.chat.side_effect = [
        # Turn 0 eval: insufficient
        json.dumps(eval_low),
        # Turn 1 eval: still insufficient, turns exhausted -> synthesise
        json.dumps(eval_low2),
        # Synthesis (best effort)
        "Best-effort answer from limited results.",
    ]

    call_count = 0

    def mock_search(**kwargs):
        nonlocal call_count
        call_count += 1
        return _mock_search_result([(f"a{call_count}.md", "x")])

    result = run_research(
        "hard question",
        max_turns=2,
        deps=ResearchGraphDeps(
            search_fn=mock_search,
            classify_fn=lambda q: MagicMock(value="semantic"),
            llm_backend=mock_backend,
        ),
    )

    assert result["synthesis"] != ""
    assert result["turns"] >= 1
    assert result["confidence"] < 0.5  # below threshold but still got synthesis


@pytest.mark.unit
def test_run_research_handles_exception() -> None:
    """Graph returns error dict when something goes wrong."""
    from kairix.agents.research.graph import run_research

    with patch(
        "kairix.agents.research.graph.build_researcher_graph",
        side_effect=RuntimeError("boom"),
    ):
        result = run_research("broken query")

    assert result["error"] != ""
    assert len(result["gaps"]) >= 1
