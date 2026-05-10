"""Sprint-20 follow-on tests: scope parameter parity on tool_contradict.

Verifies the scope parameter is plumbed through to the underlying check
without exercising the production search pipeline.

Note: ``tool_timeline`` scope-passthrough is now covered by
``tests/use_cases/test_timeline.py`` after the Phase-1 #168 refactor —
``tool_timeline`` is a thin adapter and its scope parameter is forwarded
verbatim to ``run_timeline`` (asserted by the contract test).
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.agents.mcp.server import tool_contradict
from kairix.core.search.scope import Scope


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
