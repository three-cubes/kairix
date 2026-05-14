"""Research agent graph — LangGraph state machine.

Builds a search → evaluate → refine loop that keeps looking until it
finds a good answer or runs out of turns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
from kairix.agents.research.state import DEFAULT_MAX_TURNS, ResearcherState

logger = logging.getLogger(__name__)


def _default_classify() -> Callable[..., Any]:
    """Lazy production import for intent classification."""
    from kairix.core.search.intent import classify

    return classify


def _default_search() -> Callable[..., Any]:
    """Lazy production import for the search pipeline.

    Wraps ``build_search_pipeline().search`` in a closure so the pipeline
    is constructed lazily (and cached implicitly through Python's import
    machinery for the call shape we expose to the graph).
    """

    def _search(**kwargs: Any) -> Any:
        from kairix.core.factory import build_search_pipeline

        return build_search_pipeline().search(**kwargs)

    return _search


@dataclass
class ResearchGraphDeps:
    """Injectable dependencies for the research graph.

    All fields are typed as concrete callables/objects (no ``Optional``) so
    mypy sees a real type at every call site. Production callers leave deps
    None — the dataclass wires real implementations via ``default_factory``.
    Tests construct ``ResearchGraphDeps(search_fn=fake, ...)``.

    Attributes:
        search_fn:         Search callable. Signature: (query=, budget=) -> SearchResult.
        classify_fn:       Intent classifier callable. Signature: (query) -> QueryIntent.
        llm_backend:       LLM backend object exposing ``.chat(messages, max_tokens=)``.
                           ``None`` means the nodes lazy-load the default backend.
        confidence_parser: ConfidenceParser instance for evaluate_sufficiency.
                           ``None`` means the node uses the default parser chain.
    """

    search_fn: Callable[..., Any] = field(default_factory=_default_search)
    classify_fn: Callable[..., Any] = field(default_factory=_default_classify)
    # llm_backend and confidence_parser are not test-only-callable kwargs —
    # they are stateful objects (or absent) and live as plain object slots.
    llm_backend: Any = None
    confidence_parser: Any = None
    # F1-clean test seam for the graph-build failure path: tests inject a
    # graph_builder that raises instead of @patch'ing build_researcher_graph.
    # Production callers leave default, which lazy-binds the real builder.
    graph_builder: Callable[..., Any] = field(
        default_factory=lambda: build_researcher_graph,
    )


def build_researcher_graph(
    *,
    deps: ResearchGraphDeps | None = None,
) -> Any:
    """Build the LangGraph state machine for iterative research.

    Args:
        deps: Injectable dependencies (search, classify, llm_backend,
              confidence_parser). Production callers leave None; tests
              pass a ``ResearchGraphDeps`` with fakes.

    Returns a compiled graph ready to invoke with an initial state.
    """
    from functools import partial

    from langgraph.graph import END, StateGraph

    d = deps or ResearchGraphDeps()

    graph = StateGraph(ResearcherState)

    # Add nodes — inject dependencies via partial against typed Deps shapes.
    _classify = partial(
        classify_intent,
        deps=ClassifyIntentDeps(classify_fn=d.classify_fn),
    )
    _retrieve = partial(
        retrieve,
        deps=RetrieveDeps(search_fn=d.search_fn),
    )

    # evaluate gets both llm_backend and confidence_parser if either is set
    _eval_kwargs: dict[str, Any] = {}
    if d.llm_backend is not None:
        _eval_kwargs["llm_backend"] = d.llm_backend
    if d.confidence_parser is not None:
        _eval_kwargs["confidence_parser"] = d.confidence_parser
    _eval = partial(evaluate_sufficiency, **_eval_kwargs) if _eval_kwargs else evaluate_sufficiency

    _synth = partial(synthesise, llm_backend=d.llm_backend) if d.llm_backend else synthesise

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
    deps: ResearchGraphDeps | None = None,
) -> dict[str, Any]:
    """Run a research query through the full iterative search pipeline.

    Searches your knowledge base, evaluates whether the results answer the
    question, and refines the search if needed — up to max_turns rounds.

    Args:
        query:     The question to research.
        max_turns: Maximum search rounds before giving up (default 4).
        deps:      Injectable dependencies; production callers leave None.

    Returns:
        dict with: query, synthesis, retrieved_chunks, entities_found,
        gaps, confidence, turns, error.
    """
    try:
        # Use deps.graph_builder (defaults to build_researcher_graph) so
        # tests can inject a failing builder for the exception-path contract
        # without @patch'ing the module.
        d = deps if deps is not None else ResearchGraphDeps()
        compiled = d.graph_builder(deps=d)

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
