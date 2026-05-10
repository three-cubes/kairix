"""Tests for KFEAT-010 MCP affordance improvements (AFF-1 through AFF-3, AFF-5).

Covers:
  AFF-1  Automatic budget inference (driven through ``run_search``)
  AFF-2  Plain-language tool descriptions
  AFF-3  Entity-first hint in search results (driven through ``run_search``)

Phase 2 of #168 moved budget inference and entity-card injection into
the use case (``kairix.use_cases.search.run_search``). These tests
exercise the public surface — both via the use case directly (AFF-1,
AFF-3) and via the MCP tool docstrings (AFF-2).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.agents.mcp.server import (
    tool_entity,
    tool_prep,
    tool_search,
    tool_timeline,
    tool_usage_guide,
)
from kairix.core.search.intent import QueryIntent
from kairix.use_cases.search import SearchDeps, run_search

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeInner:
    path: str = ""
    title: str = ""
    snippet: str = ""
    boosted_score: float = 0.0
    collection: str = ""


@dataclass
class _FakeBudgeted:
    result: _FakeInner
    content: str = ""
    tier: str = ""
    token_estimate: int = 0


@dataclass
class _FakeSearchResult:
    query: str = ""
    intent: Any = QueryIntent.SEMANTIC
    results: list[_FakeBudgeted] = field(default_factory=list)
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
    vec_failed: bool = False
    total_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""


def _capturing_deps(intent: QueryIntent) -> tuple[SearchDeps, dict[str, Any]]:
    """Build a SearchDeps that captures the budget passed through and
    returns an empty SearchResult."""
    captured: dict[str, Any] = {}

    def fake_search(**kwargs: Any) -> _FakeSearchResult:
        captured.update(kwargs)
        return _FakeSearchResult(intent=intent)

    return SearchDeps(
        search_fn=fake_search,
        classify_fn=lambda q: intent,
        entity_card_fn=lambda name: None,
    ), captured


# ---------------------------------------------------------------------------
# AFF-1: Budget inference via run_search public surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBudgetInference:
    """``run_search`` selects the right token budget for the inferred intent."""

    def _budget_for(self, query: str, intent: QueryIntent, explicit_budget: int = 3000) -> int:
        deps, captured = _capturing_deps(intent)
        run_search(query, budget=explicit_budget, deps=deps)
        return captured["budget"]

    def test_entity_intent_returns_1500(self) -> None:
        assert self._budget_for("tell me about Acme", QueryIntent.ENTITY) == 1500

    def test_keyword_intent_returns_1500(self) -> None:
        assert self._budget_for("KFEAT-010", QueryIntent.KEYWORD) == 1500

    def test_research_query_returns_5000(self) -> None:
        assert self._budget_for("research the competitive landscape", QueryIntent.SEMANTIC) == 5000

    def test_compare_query_returns_5000(self) -> None:
        assert self._budget_for("compare the two frameworks", QueryIntent.SEMANTIC) == 5000

    def test_analyse_query_returns_5000(self) -> None:
        assert self._budget_for("analyse the quarterly results", QueryIntent.SEMANTIC) == 5000

    def test_comprehensive_query_returns_5000(self) -> None:
        assert self._budget_for("give me a comprehensive overview", QueryIntent.SEMANTIC) == 5000

    def test_detailed_query_returns_5000(self) -> None:
        assert self._budget_for("detailed breakdown of costs", QueryIntent.SEMANTIC) == 5000

    def test_default_returns_3000(self) -> None:
        assert self._budget_for("how does the build system work", QueryIntent.SEMANTIC) == 3000

    def test_explicit_override_preserved(self) -> None:
        """Non-default explicit budget is returned unchanged regardless of intent."""
        assert self._budget_for("tell me about Acme", QueryIntent.ENTITY, explicit_budget=2000) == 2000
        assert self._budget_for("research everything", QueryIntent.SEMANTIC, explicit_budget=1000) == 1000


# ---------------------------------------------------------------------------
# AFF-2: Plain-language tool descriptions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPlainLanguageDocstrings:
    """Verify tool docstrings are written at grade 8 reading level."""

    def test_tool_search_docstring(self) -> None:
        doc = inspect.getdoc(tool_search) or ""
        first_sentence = doc.split(".")[0]
        assert "knowledge store" in first_sentence.lower() or "search" in first_sentence.lower()

    def test_tool_entity_docstring(self) -> None:
        doc = inspect.getdoc(tool_entity) or ""
        first_sentence = doc.split(".")[0]
        assert "Look up" in first_sentence

    def test_tool_prep_docstring(self) -> None:
        doc = inspect.getdoc(tool_prep) or ""
        first_sentence = doc.split(".")[0]
        assert "summary" in first_sentence.lower()

    def test_tool_timeline_docstring(self) -> None:
        doc = inspect.getdoc(tool_timeline) or ""
        first_sentence = doc.split(".")[0]
        assert "date" in first_sentence.lower()

    def test_tool_usage_guide_docstring(self) -> None:
        doc = inspect.getdoc(tool_usage_guide) or ""
        first_sentence = doc.split(".")[0]
        assert "help" in first_sentence.lower() or "guide" in first_sentence.lower()

    def test_no_temporal_as_leading_term(self) -> None:
        """Docstring first sentences should not lead with jargon like 'temporal'."""
        tools = [tool_search, tool_entity, tool_prep, tool_timeline, tool_usage_guide]
        for fn in tools:
            doc = inspect.getdoc(fn) or ""
            first_sentence = doc.split(".")[0].lower()
            assert not first_sentence.startswith("temporal"), (
                f"{fn.__name__} docstring starts with 'temporal': {first_sentence}"
            )


# ---------------------------------------------------------------------------
# AFF-3: Entity-first hint via run_search public surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEntityFirstHint:
    """When intent is ENTITY, the entity graph result appears first."""

    def test_entity_intent_prepends_entity_graph_result(self) -> None:
        sr = _FakeSearchResult(
            intent=QueryIntent.ENTITY,
            results=[_FakeBudgeted(result=_FakeInner(path="notes/acme.md", boosted_score=0.7), content="some context")],
        )
        card = {
            "id": "acme",
            "name": "Acme",
            "type": "Organisation",
            "summary": "A health org",
            "vault_path": "02-Areas/00-Clients/Acme/Acme.md",
        }

        deps = SearchDeps(
            search_fn=lambda **_: sr,
            classify_fn=lambda q: QueryIntent.ENTITY,
            entity_card_fn=lambda name: card,
        )
        out = run_search("tell me about Acme", deps=deps)

        assert len(out.results) == 2
        first = out.results[0]
        assert first.source == "entity_graph"
        assert first.entity == {"id": "acme", "name": "Acme", "type": "Organisation"}

    def test_entity_not_found_no_prepend(self) -> None:
        sr = _FakeSearchResult(
            intent=QueryIntent.ENTITY,
            results=[_FakeBudgeted(result=_FakeInner(path="notes/unknown.md", boosted_score=0.5), content="x")],
        )
        deps = SearchDeps(
            search_fn=lambda **_: sr,
            classify_fn=lambda q: QueryIntent.ENTITY,
            entity_card_fn=lambda name: None,
        )
        out = run_search("tell me about UnknownCorp", deps=deps)
        assert len(out.results) == 1
        assert out.results[0].source == ""

    def test_non_entity_intent_no_prepend(self) -> None:
        deps = SearchDeps(
            search_fn=lambda **_: _FakeSearchResult(intent=QueryIntent.SEMANTIC),
            classify_fn=lambda q: QueryIntent.SEMANTIC,
            entity_card_fn=lambda name: {"id": "x"},  # would prepend if branch ran
        )
        out = run_search("how to deploy", deps=deps)
        assert all(h.source != "entity_graph" for h in out.results)
