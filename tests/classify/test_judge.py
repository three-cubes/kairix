"""
Tests for the LLM judge (kairix/classify/judge.py).

Uses canonical FakeLLMBackend from tests/fakes.py — no MagicMock.
"""

from __future__ import annotations

import json

import pytest

from kairix.core.classify.judge import classify_with_llm
from kairix.core.classify.rules import ClassificationResult
from tests.fakes import FakeLLMBackend


def _make_backend(return_value: str | None = None, side_effect: BaseException | None = None) -> FakeLLMBackend:
    """Build a FakeLLMBackend with the given chat behaviour."""
    if side_effect is not None:
        return FakeLLMBackend(chat_raises=side_effect)
    return FakeLLMBackend(chat_response=return_value or "")


@pytest.mark.unit
class TestClassifyWithLLM:
    @pytest.mark.unit
    def test_successful_classification(self):
        mock_response = json.dumps(
            {
                "type": "semantic-decision",
                "confidence": 0.85,
                "reason": "Contains 'we decided' and rationale",
            }
        )
        result = classify_with_llm(
            "some ambiguous content",
            agent="builder",
            llm_backend=_make_backend(return_value=mock_response),
        )
        assert isinstance(result, ClassificationResult)
        assert result.type == "semantic-decision"
        assert result.confidence == pytest.approx(0.85)
        assert not result.needs_confirmation

    @pytest.mark.unit
    def test_low_confidence_sets_needs_confirmation(self):
        mock_response = json.dumps(
            {
                "type": "episodic",
                "confidence": 0.55,
                "reason": "Could be episodic or procedural",
            }
        )
        result = classify_with_llm(
            "ambiguous content",
            agent="builder",
            llm_backend=_make_backend(return_value=mock_response),
        )
        assert result.needs_confirmation is True
        assert result.confidence == pytest.approx(0.55)

    @pytest.mark.unit
    def test_api_failure_returns_unknown(self):
        result = classify_with_llm("some content", agent="builder", llm_backend=_make_backend(return_value=""))
        assert result.type == "unknown"
        assert result.confidence == pytest.approx(0.0)
        assert result.needs_confirmation is True

    @pytest.mark.unit
    def test_json_parse_error_returns_unknown(self):
        result = classify_with_llm(
            "some content",
            agent="builder",
            llm_backend=_make_backend(return_value="not valid json"),
        )
        assert result.type == "unknown"
        assert result.needs_confirmation is True

    @pytest.mark.unit
    def test_empty_content_returns_unknown(self):
        result = classify_with_llm("", agent="builder")
        assert result.type == "unknown"
        assert result.needs_confirmation is True

    @pytest.mark.unit
    def test_code_fence_wrapped_json(self):
        mock_response = '```json\n{"type": "episodic", "confidence": 0.9, "reason": "timestamp"}\n```'
        result = classify_with_llm(
            "## 09:15 did stuff",
            agent="builder",
            llm_backend=_make_backend(return_value=mock_response),
        )
        assert result.type == "episodic"
        assert result.confidence == pytest.approx(0.9)

    @pytest.mark.unit
    def test_path_resolved_for_valid_type(self):
        mock_response = json.dumps(
            {
                "type": "procedural-rule",
                "confidence": 0.92,
                "reason": "contains normative constraint",
            }
        )
        result = classify_with_llm(
            "never do X",
            agent="builder",
            llm_backend=_make_backend(return_value=mock_response),
        )
        assert result.target_path != ""
        assert "rules.md" in result.target_path

    @pytest.mark.unit
    def test_invalid_agent_raises(self):
        with pytest.raises(ValueError, match="Invalid agent"):
            classify_with_llm("some content", agent="invalid")

    @pytest.mark.unit
    def test_shared_agent_is_valid(self):
        mock_response = json.dumps(
            {
                "type": "semantic-fact",
                "confidence": 0.80,
                "reason": "infrastructure fact",
            }
        )
        result = classify_with_llm(
            "endpoint: https://api.example.com",
            agent="shared",
            llm_backend=_make_backend(return_value=mock_response),
        )
        assert result.type == "semantic-fact"
