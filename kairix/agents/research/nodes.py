"""Research agent node functions.

Each function takes the current state and returns updates to it.
The graph (graph.py) wires these together with conditional edges.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from kairix.agents.research.state import (
    DEFAULT_MAX_TURNS,
    INITIAL_BUDGET,
    REFINEMENT_BUDGET,
    SUFFICIENCY_THRESHOLD,
    ResearcherState,
)

logger = logging.getLogger(__name__)


def classify_intent(state: ResearcherState, *, classify_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Work out what kind of question this is (entity lookup, date-based, etc.).

    Args:
        classify_fn: Injectable intent classifier for testing.
                     Defaults to ``kairix.core.search.intent.classify``.
    """
    try:
        if classify_fn is None:
            from kairix.core.search.intent import classify

            classify_fn = classify

        intent = classify_fn(state["query"])
        return {"intent": intent.value}
    except Exception as exc:
        logger.warning("research: classify_intent failed — %s", exc)
        return {"intent": "semantic"}


def retrieve(state: ResearcherState, *, search_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Search the knowledge base for answers to the current query."""
    if search_fn is None:
        from kairix.core.factory import build_search_pipeline

        _pipeline = build_search_pipeline()
        search_fn = _pipeline.search

    query = state.get("refined_query") or state["query"]
    turns = state.get("turns", 0)

    # Use a bigger budget on refinement turns — we need more context
    budget = INITIAL_BUDGET if turns == 0 else REFINEMENT_BUDGET

    sr = search_fn(query=query, budget=budget)

    # Convert SearchResult to list-of-dicts for accumulation
    new_results = [{"path": b.result.path, "snippet": b.content[:500]} for b in sr.results]

    # Accumulate results across turns (don't replace previous finds)
    existing = list(state.get("retrieved_chunks") or [])

    # Deduplicate by path
    seen_paths = {r.get("path", "") for r in existing}
    for r in new_results:
        if r.get("path", "") not in seen_paths:
            existing.append(r)
            seen_paths.add(r.get("path", ""))

    logger.info(
        "research: retrieve turn=%d new=%d accumulated=%d",
        turns,
        len(new_results),
        len(existing),
    )
    return {"retrieved_chunks": existing}


def evaluate_sufficiency(
    state: ResearcherState,
    *,
    llm_backend: Any = None,
    confidence_parser: Any = None,
) -> dict[str, Any]:
    """Ask the LLM whether the search results answer the question well enough.

    Uses a ConfidenceParser chain (kairix.agents.research.confidence) to
    extract the confidence value from the LLM response. The chain tries
    JSON-mode first and falls back to regex extraction on prose, so the
    function returns a real confidence even when the LLM produces
    unstructured output. Closes the dogfood-reported bug where confidence
    was always 0.0 because raw json.loads failed silently.
    """
    query = state["query"]
    chunks = state.get("retrieved_chunks", [])
    turns = state.get("turns", 0)

    if not chunks:
        return {"confidence": 0.0, "refined_query": query}

    # Build a summary of what we found
    found_summary = "\n".join(f"- {r.get('path', '?')}: {r.get('snippet', '')[:200]}" for r in chunks[:10])

    messages = [
        {
            "role": "system",
            "content": (
                "You are evaluating whether search results answer a question. "
                "Rate your confidence from 0.0 (results are irrelevant) to 1.0 "
                "(results fully answer the question). If confidence is below 0.7, "
                "suggest a better search query that might find what's missing. "
                "List any gaps — specific pieces of information that are missing "
                "or incomplete in the results.\n\n"
                "Respond as JSON: "
                '{"confidence": 0.8, "sufficient": true, "refined_query": null, '
                '"gaps": ["missing detail 1", "missing detail 2"], '
                '"reasoning": "The results cover..."}'
            ),
        },
        {
            "role": "user",
            "content": f"Question: {query}\n\nSearch results:\n{found_summary}",
        },
    ]

    try:
        if llm_backend is None:
            from kairix.platform.llm import get_default_backend

            llm_backend = get_default_backend()
        response = llm_backend.chat(messages, max_tokens=300)
    except Exception as exc:
        logger.warning("research: evaluate_sufficiency LLM call failed — %s", exc)
        # If LLM fails, treat as insufficient so we try again (up to max_turns)
        return {"confidence": 0.0, "gaps": [], "refined_query": query}

    # Confidence via the parser chain — works on both JSON and prose
    if confidence_parser is None:
        from kairix.agents.research.confidence import default_confidence_parser_chain

        confidence_parser = default_confidence_parser_chain()

    from kairix.agents.research.protocols import ConfidenceParseError

    try:
        confidence = confidence_parser.parse(response)
    except ConfidenceParseError as exc:
        logger.warning("research: confidence parse fell through — %s", exc)
        confidence = 0.0

    # Gaps and refined_query — best-effort JSON parse, independent of confidence
    refined: str | None = None
    gaps: list[str] = []
    try:
        parsed = json.loads(response)
        refined = parsed.get("refined_query")
        raw_gaps = parsed.get("gaps") or []
        gaps = raw_gaps if isinstance(raw_gaps, list) else [str(raw_gaps)]
    except (json.JSONDecodeError, TypeError):
        # LLM returned prose; the confidence parser already handled it. Fall
        # through with empty gaps and the prior refined query.
        pass

    logger.info(
        "research: evaluate turn=%d confidence=%.2f sufficient=%s gaps=%d",
        turns,
        confidence,
        confidence >= SUFFICIENCY_THRESHOLD,
        len(gaps),
    )
    return {
        "confidence": confidence,
        "gaps": gaps,
        "refined_query": refined or state.get("refined_query") or query,
    }


def refine_query(state: ResearcherState) -> dict[str, Any]:
    """Move to the next search round with the refined query."""
    turns = state.get("turns", 0)
    return {"turns": turns + 1}


def synthesise(state: ResearcherState, *, llm_backend: Any = None) -> dict[str, Any]:
    """Build a clear answer from the search results, citing sources."""
    query = state["query"]
    chunks = state.get("retrieved_chunks", [])

    found_summary = "\n".join(f"Source: {r.get('path', '?')}\n{r.get('snippet', '')[:300]}\n" for r in chunks[:8])

    messages = [
        {
            "role": "system",
            "content": (
                "Synthesise a clear, structured answer from the search results below. "
                "Cite sources by file path. If information is incomplete, say what's "
                "missing. Be direct and concise."
            ),
        },
        {
            "role": "user",
            "content": f"Question: {query}\n\nSources:\n{found_summary}",
        },
    ]

    try:
        if llm_backend is None:
            from kairix.platform.llm import get_default_backend

            llm_backend = get_default_backend()
        synthesis = llm_backend.chat(messages, max_tokens=500)
        return {"synthesis": synthesis, "confidence": state.get("confidence", 0.0)}
    except Exception as exc:
        logger.warning("research: synthesise LLM call failed — %s", exc)
        return {
            "synthesis": f"Found {len(chunks)} relevant documents but synthesis failed.",
            "confidence": state.get("confidence", 0.0),
            "error": "Synthesis failed — check server logs for details.",
        }


def route_after_evaluation(state: ResearcherState) -> str:
    """Decide what to do after evaluating search results.

    Returns the name of the next node: 'synthesise' or 'refine_query'.
    Synthesis always runs eventually — even at low confidence we produce
    a best-effort answer so callers get usable output with a confidence
    score they can inspect.
    """
    confidence = state.get("confidence", 0.0)
    turns = state.get("turns", 0)
    max_turns = state.get("max_turns", DEFAULT_MAX_TURNS)

    if confidence >= SUFFICIENCY_THRESHOLD:
        return "synthesise"
    elif turns < max_turns - 1:
        return "refine_query"
    else:
        # Turns exhausted but confidence still low — synthesise anyway (best effort)
        return "synthesise"
