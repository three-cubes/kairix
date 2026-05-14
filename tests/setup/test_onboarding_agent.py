"""Tests for kairix.platform.setup.agent — optional LLM onboarding assistant."""

from __future__ import annotations

import pytest

from kairix.platform.setup.agent import OnboardingAgentDeps, recommend_from_profile

pytestmark = pytest.mark.unit


class TestRecommendFromProfile:
    def test_returns_dict_without_api_key(self) -> None:
        result = recommend_from_profile(
            total_docs=500,
            format_counts={"md": 400, "pdf": 100},
            date_file_pct=0.20,
            procedural_pct=0.08,
            entity_pct=0.05,
        )
        assert result is not None
        assert isinstance(result, dict)
        assert "fusion_strategy" in result
        assert "reasoning" in result

    def test_enables_temporal_boost_for_date_heavy_corpus(self) -> None:
        result = recommend_from_profile(
            total_docs=100,
            format_counts={"md": 100},
            date_file_pct=0.40,
            procedural_pct=0.0,
            entity_pct=0.0,
        )
        assert result["temporal_boost"] is True

    def test_disables_temporal_boost_for_low_date_corpus(self) -> None:
        result = recommend_from_profile(
            total_docs=100,
            format_counts={"md": 100},
            date_file_pct=0.05,
            procedural_pct=0.0,
            entity_pct=0.0,
        )
        assert result["temporal_boost"] is False

    def test_switches_to_rrf_for_pdf_heavy_corpus(self) -> None:
        result = recommend_from_profile(
            total_docs=100,
            format_counts={"md": 30, "pdf": 70},
            date_file_pct=0.0,
            procedural_pct=0.0,
            entity_pct=0.0,
        )
        assert result["fusion_strategy"] == "rrf"

    def test_keeps_bm25_for_markdown_corpus(self) -> None:
        result = recommend_from_profile(
            total_docs=100,
            format_counts={"md": 95, "pdf": 5},
            date_file_pct=0.0,
            procedural_pct=0.0,
            entity_pct=0.0,
        )
        assert result["fusion_strategy"] == "bm25_primary"

    def test_llm_failure_returns_rule_based(self) -> None:
        """When the LLM call fails, the rule-based recommendations still apply.

        F1-clean: tests inject a failing chat callable via OnboardingAgentDeps
        rather than @patch'ing the internal _call_llm helper. The fail-path
        contract is unchanged.
        """

        def _boom(prompt: str, api_key: str, endpoint: str) -> str:
            raise RuntimeError("LLM unavailable")

        result = recommend_from_profile(
            total_docs=100,
            format_counts={"md": 100},
            date_file_pct=0.20,
            procedural_pct=0.10,
            entity_pct=0.05,
            api_key="test-key",
            endpoint="https://test.example.com",
            deps=OnboardingAgentDeps(chat=_boom),
        )
        assert result is not None
        assert "llm_advice" not in result  # LLM failed, no advice added
        assert result["temporal_boost"] is True  # rule-based still works
