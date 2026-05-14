"""Integration tests for evaluate_sufficiency with ConfidenceParser chain.

Closes the 2026-05-02 dogfood-reported bug where research confidence was
always 0.0 because raw json.loads silently fell through on prose
responses. The fix wires default_confidence_parser_chain() into
evaluate_sufficiency so JSON, JSON-with-prose, and bare-prose responses
all return a real confidence value.

Tested through evaluate_sufficiency's public surface using the canonical
FakeLLMBackend from tests/fakes.py. No @patch, no monkeypatch, no
private imports.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.agents.research.nodes import evaluate_sufficiency
from tests.fakes import FakeLLMBackend


def _state_with_chunks() -> dict[str, Any]:
    """Minimal ResearcherState shape with a non-empty retrieved_chunks list."""
    return {
        "query": "what did we decide about Globex governance?",
        "refined_query": "Globex governance decisions",
        "retrieved_chunks": [{"path": "doc1.md", "snippet": "Decision: ..."}],
        "turns": 0,
    }


@pytest.mark.unit
def test_json_response_extracts_confidence() -> None:
    """When the LLM returns valid JSON, confidence comes through cleanly."""
    backend = FakeLLMBackend(
        chat_response='{"confidence": 0.85, "sufficient": true, "refined_query": null, "gaps": []}',
    )
    result = evaluate_sufficiency(_state_with_chunks(), llm_backend=backend)
    assert result["confidence"] == pytest.approx(0.85)
    assert result["gaps"] == []


@pytest.mark.unit
def test_prose_response_extracts_confidence_via_regex_fallback() -> None:
    """The dogfood failure mode: LLM returns prose instead of JSON.

    Before the fix, json.loads raised, the except block returned 0.0.
    After the fix, the regex fallback in the parser chain extracts the
    value from prose like "Confidence: 0.7 — the results cover ...".
    """
    backend = FakeLLMBackend(
        chat_response="Confidence: 0.7 — the results mostly cover the question but lack the Q1 update.",
    )
    result = evaluate_sufficiency(_state_with_chunks(), llm_backend=backend)
    assert result["confidence"] == pytest.approx(0.7)
    # gaps remain empty because the prose isn't JSON; that's expected.
    assert result["gaps"] == []


@pytest.mark.unit
def test_percent_form_in_prose_extracted() -> None:
    """Common LLM idiom: 'Confidence: 70%' should become 0.7."""
    backend = FakeLLMBackend(chat_response="My confidence is 70% based on the available evidence.")
    result = evaluate_sufficiency(_state_with_chunks(), llm_backend=backend)
    assert result["confidence"] == pytest.approx(0.7)


@pytest.mark.unit
def test_unparseable_response_falls_through_to_zero() -> None:
    """When neither JSON nor regex find a confidence, fall through cleanly to 0.0."""
    backend = FakeLLMBackend(chat_response="The results are interesting but I cannot say more.")
    result = evaluate_sufficiency(_state_with_chunks(), llm_backend=backend)
    assert result["confidence"] == pytest.approx(0.0)


@pytest.mark.unit
def test_empty_chunks_returns_zero_without_calling_llm() -> None:
    """Sanity: the early-return path when there are no chunks is preserved."""
    backend = FakeLLMBackend(chat_response='{"confidence": 0.99}')
    state = {"query": "q", "refined_query": "q", "retrieved_chunks": [], "turns": 0}
    result = evaluate_sufficiency(state, llm_backend=backend)
    assert result["confidence"] == pytest.approx(0.0)


@pytest.mark.unit
def test_explicit_parser_injection_overrides_default() -> None:
    """Explicitly injected parsers must be used in preference to the default chain."""

    class _AlwaysReturnsHalf:
        def parse(self, response: str) -> float:
            return 0.5

    backend = FakeLLMBackend(chat_response='{"confidence": 0.99}')
    result = evaluate_sufficiency(
        _state_with_chunks(),
        llm_backend=backend,
        confidence_parser=_AlwaysReturnsHalf(),
    )
    assert result["confidence"] == pytest.approx(0.5)
