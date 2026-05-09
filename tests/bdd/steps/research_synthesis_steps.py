"""Step definitions for research_synthesis.feature."""

from __future__ import annotations

from pytest_bdd import given, then, when

from kairix.agents.research.nodes import evaluate_sufficiency, synthesise
from kairix.agents.research.state import ResearcherState
from tests.fakes import FakeLLMBackend

# Module-level state (simple, test-scoped)
_state: dict = {}


def _base_state(**overrides) -> ResearcherState:
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


@given("the research finds documents but confidence is low")
def given_low_confidence_results():
    import json

    chunks = [
        {"path": "docs/overview.md", "snippet": "A general overview of the system."},
        {
            "path": "docs/faq.md",
            "snippet": "Frequently asked questions about the project.",
        },
    ]

    # LLM returns low confidence during evaluation
    eval_response = json.dumps(
        {
            "confidence": 0.25,
            "sufficient": False,
            "refined_query": "test question detailed",
            "reasoning": "Results are tangentially related but do not directly answer.",
        }
    )

    fake_llm = FakeLLMBackend(chat_response=eval_response)

    _state["research_state"] = _base_state(
        query="test question",
        retrieved_chunks=chunks,
        turns=3,
        max_turns=4,
    )

    # Run evaluate_sufficiency with injected LLM backend
    updates = evaluate_sufficiency(_state["research_state"], llm_backend=fake_llm)
    _state["research_state"].update(updates)

    # Store the synthesis-stage LLM (a different scripted response) separately
    _state["synth_llm"] = FakeLLMBackend(
        chat_response=(
            "Based on the available documents, the system provides a general overview "
            "and FAQ. Sources: docs/overview.md, docs/faq.md."
        )
    )


@when("the agent completes research")
def agent_completes_research():
    updates = synthesise(_state["research_state"], llm_backend=_state["synth_llm"])
    _state["research_state"].update(updates)


@then("the research state has a non-empty synthesis")
def synthesis_is_non_empty():
    synthesis = _state["research_state"].get("synthesis", "")
    assert synthesis, f"Expected non-empty synthesis, got {synthesis!r}"


@then("the research state confidence is greater than zero")
def confidence_greater_than_zero():
    confidence = _state["research_state"].get("confidence", 0.0)
    assert confidence > 0.0, f"Expected confidence > 0.0, got {confidence}"
