"""Sprint-20 follow-on tests: scope parameter parity on tool_timeline + tool_contradict.

These tools accept ``scope`` as a parameter and forward it to the underlying
search call. Tests verify the parameter is plumbed through correctly without
exercising the production search pipeline.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.agents.mcp.server import tool_contradict, tool_timeline
from kairix.core.search.scope import Scope

# tool_timeline ------------------------------------------------------


def _extract_non_temporal(query: str, reference_date: Any = None) -> tuple[Any, Any]:
    return None, None


@pytest.mark.unit
def test_tool_timeline_forwards_scope_to_search() -> None:
    """When tool_timeline runs the fallthrough search, scope is forwarded."""
    captured: dict[str, Any] = {}

    class _SR:
        def __init__(self) -> None:
            self.results: list[Any] = []

    def _fake_search(**kwargs: Any) -> _SR:
        captured.update(kwargs)
        return _SR()

    tool_timeline(
        query="anything",
        agent="shape",
        scope=Scope.ALL_AGENTS,
        extract_fn=_extract_non_temporal,
        search_fn=_fake_search,
    )

    assert captured["scope"] is Scope.ALL_AGENTS
    assert captured["agent"] == "shape"


@pytest.mark.unit
def test_tool_timeline_default_scope_is_shared_agent() -> None:
    captured: dict[str, Any] = {}

    class _SR:
        def __init__(self) -> None:
            self.results: list[Any] = []

    def _fake_search(**kwargs: Any) -> _SR:
        captured.update(kwargs)
        return _SR()

    tool_timeline(
        query="anything",
        extract_fn=_extract_non_temporal,
        search_fn=_fake_search,
    )

    assert captured["scope"] is Scope.SHARED_AGENT


# tool_contradict ----------------------------------------------------


@pytest.mark.unit
def test_tool_contradict_forwards_scope_to_check() -> None:
    """tool_contradict's scope kwarg flows through to check_contradiction."""
    captured: dict[str, Any] = {}

    def _fake_contradict(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    class _FakeLLM:
        def chat(self, messages: list[dict]) -> str:
            return "{}"

    tool_contradict(
        content="any claim",
        agent="builder",
        scope=Scope.EVERYTHING,
        llm_backend=_FakeLLM(),
        contradict_fn=_fake_contradict,
    )

    assert captured["scope"] is Scope.EVERYTHING
    assert captured["agent"] == "builder"


@pytest.mark.unit
def test_tool_contradict_default_threshold_is_0_45() -> None:
    """Threshold default aligned with the WS2-B 3-category composite (0.45)."""
    captured: dict[str, Any] = {}

    def _fake_contradict(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    class _FakeLLM:
        def chat(self, messages: list[dict]) -> str:
            return "{}"

    tool_contradict(content="any claim", llm_backend=_FakeLLM(), contradict_fn=_fake_contradict)

    assert captured["threshold"] == pytest.approx(0.45)
