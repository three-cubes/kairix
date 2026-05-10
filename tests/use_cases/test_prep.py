"""Unit tests for ``kairix.use_cases.prep.run_prep``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.core.search.scope import Scope
from kairix.use_cases.prep import (
    PrepDeps,
    PrepOutput,
    prep_output_to_envelope,
    run_prep,
)

pytestmark = pytest.mark.unit


@dataclass
class _FakeInner:
    title: str = ""
    path: str = ""


@dataclass
class _FakeBudgeted:
    result: _FakeInner
    content: str = ""


@dataclass
class _FakeSearchResult:
    results: list[_FakeBudgeted] = field(default_factory=list)


def _build_deps(
    *,
    sr: _FakeSearchResult | None = None,
    summary: str = "",
    search_raises: bool = False,
    chat_raises: bool = False,
) -> tuple[PrepDeps, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_search(**kwargs: Any) -> _FakeSearchResult:
        captured["search"] = kwargs
        if search_raises:
            raise RuntimeError("search down")
        return sr or _FakeSearchResult()

    def fake_chat(**kwargs: Any) -> str:
        captured["chat"] = kwargs
        if chat_raises:
            raise RuntimeError("chat down")
        return summary

    return PrepDeps(search_fn=fake_search, chat_fn=fake_chat), captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_l0_summary_calls_chat_with_concise_system_message() -> None:
    sr = _FakeSearchResult(
        results=[_FakeBudgeted(result=_FakeInner(title="doc-a", path="/a"), content="alpha snippet")]
    )
    deps, captured = _build_deps(sr=sr, summary="brief alpha summary")
    out = run_prep("topic", tier="l0", deps=deps)

    assert out.error == ""
    assert out.tier == "l0"
    assert out.summary == "brief alpha summary"
    assert out.tokens > 0
    assert out.sources == ["doc-a"]
    # The system prompt mentions "2-3 sentences" for l0 only.
    assert "2-3 sentences" in captured["chat"]["messages"][0]["content"]
    # Search budget is 1500 for l0.
    assert captured["search"]["budget"] == 1500
    # Chat max_tokens is 150 for l0.
    assert captured["chat"]["max_tokens"] == 150


def test_l1_summary_uses_structured_overview_prompt() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="doc-b"), content="bravo snippet")])
    deps, captured = _build_deps(sr=sr, summary="structured overview")
    out = run_prep("topic", tier="l1", deps=deps)

    assert out.tier == "l1"
    assert "structured overview" in captured["chat"]["messages"][0]["content"]
    assert captured["search"]["budget"] == 3000
    assert captured["chat"]["max_tokens"] == 600


def test_no_results_returns_no_documents_message_no_chat_call() -> None:
    deps, captured = _build_deps(sr=_FakeSearchResult(results=[]))
    out = run_prep("obscure topic", deps=deps)
    assert "No relevant documents" in out.summary
    assert out.error == ""
    assert "chat" not in captured  # chat must not run when there's no context


def test_sources_use_path_when_title_empty() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="", path="/notes/foo.md"), content="x")])
    deps, _ = _build_deps(sr=sr, summary="ok")
    out = run_prep("topic", deps=deps)
    assert out.sources == ["/notes/foo.md"]


def test_only_top_5_results_become_sources() -> None:
    big = [_FakeBudgeted(result=_FakeInner(title=f"d{i}"), content="x") for i in range(8)]
    sr = _FakeSearchResult(results=big)
    deps, _ = _build_deps(sr=sr, summary="ok")
    out = run_prep("topic", deps=deps)
    assert len(out.sources) == 5


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_search_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(search_raises=True)
    out = run_prep("anything", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.summary == ""


def test_chat_failure_yields_error_envelope() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="d"), content="snip")])
    deps, _ = _build_deps(sr=sr, chat_raises=True)
    out = run_prep("topic", deps=deps)
    assert out.error.startswith("RuntimeError:")


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


def test_agent_and_scope_pass_through() -> None:
    deps, captured = _build_deps()
    run_prep("q", agent="builder", scope=Scope.AGENT, deps=deps)
    assert captured["search"]["agent"] == "builder"
    assert captured["search"]["scope"] is Scope.AGENT


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_envelope_includes_all_fields() -> None:
    out = PrepOutput(query="q", tier="l1", summary="s", tokens=42, sources=["a", "b"])
    env = prep_output_to_envelope(out)
    assert env == {
        "query": "q",
        "tier": "l1",
        "summary": "s",
        "tokens": 42,
        "sources": ["a", "b"],
        "error": "",
    }
