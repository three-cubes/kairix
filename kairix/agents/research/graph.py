"""Research agent graph — LangGraph state machine.

Builds a search → evaluate → refine loop that keeps looking until it
finds a good answer or runs out of turns.
"""

from __future__ import annotations

import logging
from typing import Any

from kairix.agents.research.nodes import (
    classify_intent,
    evaluate_sufficiency,
    refine_query,
    retrieve,
    route_after_evaluation,
    synthesise,
)
from kairix.agents.research.state import DEFAULT_MAX_TURNS, ResearcherState

logger = logging.getLogger(__name__)


def build_researcher_graph(
    *,
    search_fn: Any | None = None,
    llm_backend: Any | None = None,
    classify_fn: Any | None = None,
    confidence_parser: Any | None = None,
) -> Any:
    """Build the LangGraph state machine for iterative research.

    Args:
        search_fn:         Injectable search function (passed to retrieve node).
        llm_backend:       Injectable LLM backend (passed to evaluate/synthesise nodes).
        classify_fn:       Injectable intent classifier (passed to classify_intent node).
        confidence_parser: Injectable ConfidenceParser (passed to evaluate node).
                           If None, evaluate_sufficiency uses default_confidence_parser_chain().

    Returns a compiled graph ready to invoke with an initial state.
    """
    from functools import partial

    from langgraph.graph import END, StateGraph

    graph = StateGraph(ResearcherState)

    # Add nodes — inject dependencies via partial where provided
    _classify = partial(classify_intent, classify_fn=classify_fn) if classify_fn else classify_intent
    _retrieve = partial(retrieve, search_fn=search_fn) if search_fn else retrieve

    # evaluate gets both llm_backend and confidence_parser if either is set
    _eval_kwargs: dict[str, Any] = {}
    if llm_backend is not None:
        _eval_kwargs["llm_backend"] = llm_backend
    if confidence_parser is not None:
        _eval_kwargs["confidence_parser"] = confidence_parser
    _eval = partial(evaluate_sufficiency, **_eval_kwargs) if _eval_kwargs else evaluate_sufficiency

    _synth = partial(synthesise, llm_backend=llm_backend) if llm_backend else synthesise

    graph.add_node("classify_intent", _classify)
    graph.add_node("retrieve", _retrieve)
    graph.add_node("evaluate_sufficiency", _eval)
    graph.add_node("refine_query", refine_query)
    graph.add_node("synthesise", _synth)

    # Wire edges
    graph.set_entry_point("classify_intent")
    graph.add_edge("classify_intent", "retrieve")
    graph.add_edge("retrieve", "evaluate_sufficiency")
    graph.add_conditional_edges(
        "evaluate_sufficiency",
        route_after_evaluation,
        {
            "synthesise": "synthesise",
            "refine_query": "refine_query",
        },
    )
    graph.add_edge("refine_query", "retrieve")
    graph.add_edge("synthesise", END)

    return graph.compile()


def run_research(
    query: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    *,
    search_fn: Any | None = None,
    llm_backend: Any | None = None,
    classify_fn: Any | None = None,
    confidence_parser: Any | None = None,
) -> dict[str, Any]:
    """Run a research query through the full iterative search pipeline.

    Searches your knowledge base, evaluates whether the results answer the
    question, and refines the search if needed — up to max_turns rounds.

    Args:
        query:             The question to research.
        max_turns:         Maximum search rounds before giving up (default 4).
        search_fn:         Injectable search function for testing.
        llm_backend:       Injectable LLM backend for testing.
        classify_fn:       Injectable intent classifier for testing.
        confidence_parser: Injectable ConfidenceParser for testing.

    Returns:
        dict with: query, synthesis, retrieved_chunks, entities_found,
        gaps, confidence, turns, error.
    """
    try:
        compiled = build_researcher_graph(
            search_fn=search_fn,
            llm_backend=llm_backend,
            classify_fn=classify_fn,
            confidence_parser=confidence_parser,
        )

        initial_state: ResearcherState = {
            "query": query,
            "refined_query": query,
            "intent": "",
            "retrieved_chunks": [],
            "entities_found": [],
            "gaps": [],
            "synthesis": "",
            "turns": 0,
            "confidence": 0.0,
            "max_turns": max_turns,
            "error": "",
        }

        final_state = compiled.invoke(initial_state)
        return dict(final_state)

    except Exception as exc:
        logger.warning("research: run_research failed — %s", exc, exc_info=True)
        return {
            "query": query,
            "synthesis": "",
            "retrieved_chunks": [],
            "entities_found": [],
            "gaps": [f"Research failed: {type(exc).__name__}"],
            "confidence": 0.0,
            "turns": 0,
            "error": "Research failed — check server logs for details.",
        }
