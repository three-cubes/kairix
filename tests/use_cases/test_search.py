"""Unit tests for ``kairix.use_cases.search.run_search``.

Drives the use case through SearchDeps injection — no @patch, no
monkeypatch. Pinning the contract that closes #168 Phase 2 drift:
same use case drives both the CLI's ``kairix search`` and the MCP's
``tool_search``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.core.search.intent import QueryIntent
from kairix.core.search.scope import Scope
from kairix.use_cases.search import (
    SearchDeps,
    SearchHit,
    SearchOutput,
    run_search,
)

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


def _build_deps(
    *,
    sr: _FakeSearchResult | None = None,
    classify: Any = None,
    card: dict[str, Any] | None = None,
    search_raises: bool = False,
    classify_raises: bool = False,
    card_raises: bool = False,
) -> tuple[SearchDeps, dict[str, list[Any]]]:
    captured: dict[str, list[Any]] = {"search": [], "classify": [], "card": []}

    def fake_search(**kwargs: Any) -> _FakeSearchResult:
        captured["search"].append(kwargs)
        if search_raises:
            raise RuntimeError("search boom")
        return sr or _FakeSearchResult()

    def fake_classify(query: str) -> QueryIntent:
        captured["classify"].append(query)
        if classify_raises:
            raise RuntimeError("classify boom")
        return classify if classify is not None else QueryIntent.SEMANTIC

    def fake_card(name: str) -> dict[str, Any] | None:
        captured["card"].append(name)
        if card_raises:
            raise RuntimeError("card boom")
        return card

    return SearchDeps(search_fn=fake_search, classify_fn=fake_classify, entity_card_fn=fake_card), captured


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_hit_default_optional_fields() -> None:
    h = SearchHit(path="p", title="t", snippet="s", score=0.5)
    assert h.tier == ""
    assert h.tokens == 0
    assert h.collection == ""
    assert h.source == ""
    assert h.entity == {}


@pytest.mark.unit
def test_search_output_default_results_is_empty_list() -> None:
    out = SearchOutput(query="q", intent="semantic")
    assert out.results == []
    assert out.error == ""
    assert out.bm25_count == 0
    assert out.vec_failed is False


# ---------------------------------------------------------------------------
# Happy path: pipeline produces hits, projection lifts every field.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_results_projected_into_search_hits() -> None:
    inner = _FakeInner(
        path="docs/note.md", title="Note", snippet="raw snippet", boosted_score=0.85, collection="shared"
    )
    budgeted = _FakeBudgeted(result=inner, content="boundary-trimmed snippet", tier="L1", token_estimate=42)
    sr = _FakeSearchResult(
        query="q",
        intent=QueryIntent.SEMANTIC,
        results=[budgeted],
        bm25_count=8,
        vec_count=12,
        fused_count=15,
        total_tokens=42,
        latency_ms=125.5,
    )
    deps, _ = _build_deps(sr=sr)

    out = run_search("q", deps=deps)

    assert out.query == "q"
    assert out.intent == "semantic"
    assert out.bm25_count == 8
    assert out.vec_count == 12
    assert out.fused_count == 15
    assert out.total_tokens == 42
    assert out.latency_ms == pytest.approx(125.5)
    assert out.error == ""
    assert len(out.results) == 1
    h = out.results[0]
    assert h.path == "docs/note.md"
    assert h.title == "Note"
    # boundary-trimmed content takes precedence over inner.snippet
    assert h.snippet == "boundary-trimmed snippet"
    assert h.score == pytest.approx(0.85)
    assert h.tier == "L1"
    assert h.tokens == 42
    assert h.collection == "shared"
    assert h.source == ""  # not an entity-graph card


@pytest.mark.unit
def test_results_truncated_to_limit() -> None:
    big = [_FakeBudgeted(result=_FakeInner(path=f"/p{i}"), content="") for i in range(25)]
    deps, _ = _build_deps(sr=_FakeSearchResult(results=big))
    out = run_search("q", limit=7, deps=deps)
    assert len(out.results) == 7


# ---------------------------------------------------------------------------
# Budget inference: explicit non-default wins; entity/keyword shrinks; "research" expands.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_explicit_non_default_passed_through_unchanged() -> None:
    deps, captured = _build_deps()
    run_search("anything", budget=999, deps=deps)
    assert captured["search"][0]["budget"] == 999


@pytest.mark.unit
def test_budget_default_3000_with_entity_intent_drops_to_1500() -> None:
    deps, captured = _build_deps(classify=QueryIntent.ENTITY)
    run_search("who is Acme", deps=deps)
    assert captured["search"][0]["budget"] == 1500


@pytest.mark.unit
def test_budget_default_3000_with_keyword_intent_drops_to_1500() -> None:
    deps, captured = _build_deps(classify=QueryIntent.KEYWORD)
    run_search("token", deps=deps)
    assert captured["search"][0]["budget"] == 1500


@pytest.mark.unit
def test_budget_default_3000_with_research_keyword_expands_to_5000() -> None:
    deps, captured = _build_deps(classify=QueryIntent.SEMANTIC)
    run_search("research the topic", deps=deps)
    assert captured["search"][0]["budget"] == 5000


@pytest.mark.unit
def test_budget_default_3000_no_special_signals_stays_3000() -> None:
    deps, captured = _build_deps(classify=QueryIntent.SEMANTIC)
    run_search("ordinary query", deps=deps)
    assert captured["search"][0]["budget"] == 3000


@pytest.mark.unit
def test_classify_failure_falls_through_to_heuristic() -> None:
    """A classify exception must not crash; non-research queries stay at 3000."""
    deps, captured = _build_deps(classify_raises=True)
    run_search("ordinary query", deps=deps)
    assert captured["search"][0]["budget"] == 3000


# ---------------------------------------------------------------------------
# Entity-graph augmentation: ENTITY intent + card present → prepended.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_entity_card_prepended_when_entity_intent_and_card_found() -> None:
    sr = _FakeSearchResult(
        intent=QueryIntent.ENTITY,
        results=[_FakeBudgeted(result=_FakeInner(path="other.md"), content="other")],
    )
    card = {
        "id": "acme",
        "name": "Acme",
        "type": "Organisation",
        "summary": "Acme is a client engagement.",
        "vault_path": "02-Areas/00-Clients/Acme/Acme.md",
    }
    deps, captured = _build_deps(sr=sr, classify=QueryIntent.ENTITY, card=card)

    out = run_search("who is Acme", deps=deps)

    assert len(out.results) == 2
    # First hit is the entity card.
    first = out.results[0]
    assert first.source == "entity_graph"
    assert first.score == pytest.approx(1.0)
    assert first.path == "02-Areas/00-Clients/Acme/Acme.md"
    assert first.title == "Acme"
    assert first.entity == {"id": "acme", "name": "Acme", "type": "Organisation"}
    # Lookup happened against the de-prefixed name.
    assert captured["card"] == ["Acme"]


@pytest.mark.unit
def test_entity_card_skipped_when_include_entity_card_false() -> None:
    sr = _FakeSearchResult(intent=QueryIntent.ENTITY, results=[])
    deps, captured = _build_deps(sr=sr, classify=QueryIntent.ENTITY, card={"id": "x"})
    run_search("who is Acme", include_entity_card=False, deps=deps)
    assert captured["card"] == []


@pytest.mark.unit
def test_entity_card_skipped_for_non_entity_intent() -> None:
    sr = _FakeSearchResult(intent=QueryIntent.SEMANTIC, results=[])
    deps, captured = _build_deps(sr=sr, classify=QueryIntent.SEMANTIC, card={"id": "x"})
    run_search("a question", deps=deps)
    assert captured["card"] == []


@pytest.mark.unit
def test_entity_card_lookup_failure_does_not_break_search() -> None:
    sr = _FakeSearchResult(intent=QueryIntent.ENTITY, results=[])
    deps, _ = _build_deps(sr=sr, classify=QueryIntent.ENTITY, card_raises=True)
    out = run_search("who is Acme", deps=deps)
    assert out.error == ""
    assert out.results == []


@pytest.mark.unit
def test_entity_card_missing_query_name_skips_lookup() -> None:
    sr = _FakeSearchResult(intent=QueryIntent.ENTITY, results=[])
    deps, captured = _build_deps(sr=sr, classify=QueryIntent.ENTITY, card={"id": "x"})
    # Empty query → empty extracted name → no lookup
    run_search("", deps=deps)
    assert captured["card"] == []


# ---------------------------------------------------------------------------
# Result projection edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_budgeted_with_none_inner_yields_empty_hit() -> None:
    bad = _FakeBudgeted(result=None, content="x")  # type: ignore[arg-type]  # exercising malformed-shape tolerance
    deps, _ = _build_deps(sr=_FakeSearchResult(results=[bad]))
    out = run_search("q", deps=deps)
    assert out.results[0].path == ""
    assert out.results[0].score == pytest.approx(0.0)


@pytest.mark.unit
def test_inner_snippet_used_when_content_empty() -> None:
    inner = _FakeInner(path="/p", snippet="from inner", boosted_score=0.5)
    bad = _FakeBudgeted(result=inner, content="")  # adapter falls back to inner.snippet
    deps, _ = _build_deps(sr=_FakeSearchResult(results=[bad]))
    out = run_search("q", deps=deps)
    assert out.results[0].snippet == "from inner"


# ---------------------------------------------------------------------------
# Error path: top-level failure populates envelope.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_pipeline_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(search_raises=True)
    out = run_search("q", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.results == []
    assert out.intent == ""


# ---------------------------------------------------------------------------
# Pass-through: every adapter param reaches the underlying search call.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_and_scope_pass_through() -> None:
    deps, captured = _build_deps()
    run_search("q", agent="builder", scope=Scope.AGENT, deps=deps)
    call = captured["search"][0]
    assert call["agent"] == "builder"
    assert call["scope"] is Scope.AGENT


@pytest.mark.unit
def test_default_scope_is_shared_agent() -> None:
    deps, captured = _build_deps()
    run_search("q", deps=deps)
    assert captured["search"][0]["scope"] is Scope.SHARED_AGENT
