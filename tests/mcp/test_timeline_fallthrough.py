"""Integration tests for tool_timeline's MCP-vs-CLI parity (WS2-C).

Closes the 2026-05-02 dogfood-reported asymmetry: the MCP tool returned
empty when is_temporal=False, while the CLI fell through to search.

The fix: when search_fn is wired, tool_timeline always runs a search
using the (possibly rewritten) query, returning a results list and
fell_back: bool. No @patch, no monkeypatch — uses small in-test fakes.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.agents.mcp.server import tool_timeline


def _extract_temporal(query: str, reference_date: Any = None) -> tuple[Any, Any]:
    from datetime import date

    return date(2026, 4, 6), date(2026, 4, 13)


def _extract_non_temporal(query: str, reference_date: Any = None) -> tuple[Any, Any]:
    return None, None


def _rewrite_temporal(query: str, reference_date: Any = None) -> str:
    return f"{query} 2026-04-06..2026-04-13"


def _build_fake_search(results: list[dict[str, Any]]) -> Any:
    """Build a search_fn that records the query it was called with and returns SearchResult-shaped data."""

    class _Result:
        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    class _SearchResult:
        def __init__(self) -> None:
            self.results = [_Result(**r) for r in results]
            self.last_query: str | None = None

    sr = _SearchResult()

    def search_fn(*, query: str, budget: int = 3000, **_kwargs: Any) -> _SearchResult:
        sr.last_query = query
        return sr

    return search_fn, sr


@pytest.mark.unit
def test_non_temporal_query_falls_through_to_search() -> None:
    """The dogfood failure mode: 'tell me about LaTrobe Health Services' is not temporal,
    but the agent expected results. After WS2-C, results come back via fallthrough."""
    search_fn, sr = _build_fake_search(
        [
            {"path": "doc1.md", "title": "LaTrobe", "snippet": "engagement notes", "score": 0.9},
            {"path": "doc2.md", "title": "Delivery", "snippet": "milestones", "score": 0.8},
        ]
    )
    result = tool_timeline(
        query="tell me about LaTrobe Health Services delivery milestones",
        extract_fn=_extract_non_temporal,
        search_fn=search_fn,
    )

    assert result["is_temporal"] is False
    assert result["fell_back"] is True
    assert len(result["results"]) == 2
    assert result["results"][0]["path"] == "doc1.md"
    # Search was invoked with the original query (no rewrite happened)
    assert sr.last_query == "tell me about LaTrobe Health Services delivery milestones"


@pytest.mark.unit
def test_temporal_query_uses_rewritten_query_for_search() -> None:
    """When the query is temporal, the rewriter expands it and search runs against the rewrite."""
    search_fn, sr = _build_fake_search([{"path": "memory.md", "title": "Last week", "snippet": "...", "score": 0.7}])
    result = tool_timeline(
        query="what happened last week",
        extract_fn=_extract_temporal,
        rewrite_fn=_rewrite_temporal,
        search_fn=search_fn,
    )

    assert result["is_temporal"] is True
    assert result["fell_back"] is False
    assert "2026-04-06..2026-04-13" in result["rewritten_query"]
    assert sr.last_query == result["rewritten_query"]
    assert len(result["results"]) == 1


@pytest.mark.unit
def test_no_search_fn_returns_empty_results() -> None:
    """Backwards-compat: callers who pass no search_fn still get the rewriter-only response shape."""
    result = tool_timeline(query="what about today", extract_fn=_extract_non_temporal)
    assert result["is_temporal"] is False
    assert result["fell_back"] is True
    assert result["results"] == []


@pytest.mark.unit
def test_search_failure_does_not_break_timeline() -> None:
    """When the wired search_fn raises, timeline returns an empty results list, not an error."""

    def failing_search(*, query: str, budget: int = 3000, **_kwargs: Any) -> Any:
        raise RuntimeError("search down")

    result = tool_timeline(
        query="anything",
        extract_fn=_extract_non_temporal,
        search_fn=failing_search,
    )
    assert result["is_temporal"] is False
    assert result["results"] == []
    assert result["error"] == ""  # Search failure is a soft failure


@pytest.mark.unit
def test_response_shape_includes_required_fields() -> None:
    """Every successful response carries the keys callers depend on."""
    search_fn, _ = _build_fake_search([])
    result = tool_timeline(query="anything", extract_fn=_extract_non_temporal, search_fn=search_fn)
    for key in ("original_query", "rewritten_query", "is_temporal", "fell_back", "time_window", "results", "error"):
        assert key in result
