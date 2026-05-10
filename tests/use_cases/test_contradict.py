"""Unit tests for ``kairix.use_cases.contradict.run_contradict``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.search.scope import Scope
from kairix.use_cases.contradict import (
    ContradictDeps,
    ContradictionHit,
    ContradictOutput,
    contradict_output_to_envelope,
    run_contradict,
)


@dataclass
class _FakeContradictionResult:
    doc_path: str = ""
    score: float = 0.0
    reason: str = ""
    snippet: str = ""
    category: str = "direct"
    claim: str = ""


class _FakeLLM:
    def chat(self, messages: list[dict]) -> str:
        return "{}"


def _build_deps(
    *,
    results: list[_FakeContradictionResult] | None = None,
    raises: bool = False,
) -> tuple[ContradictDeps, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_check(**kwargs: Any) -> list[_FakeContradictionResult]:
        captured.update(kwargs)
        if raises:
            raise RuntimeError("boom")
        return list(results or [])

    return ContradictDeps(check_fn=fake_check, llm_backend=_FakeLLM()), captured


# ---------------------------------------------------------------------------
# Defaults / shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contradiction_hit_default_optionals() -> None:
    h = ContradictionHit(path="p", score=0.5, reason="r", snippet="s")
    assert h.category == ""
    assert h.claim == ""


@pytest.mark.unit
def test_contradict_output_default_results_is_empty_list() -> None:
    out = ContradictOutput(content="c")
    assert out.contradictions == []
    assert out.has_contradictions is False
    assert out.error == ""


# ---------------------------------------------------------------------------
# Happy path projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_results_projected_into_contradiction_hits() -> None:
    fake = _FakeContradictionResult(
        doc_path="docs/old.md",
        score=0.78,
        reason="contradicts X",
        snippet="The system uses option A.",
        category="status_mismatch",
        claim="The system now uses option B.",
    )
    deps, _ = _build_deps(results=[fake])
    out = run_contradict("System now uses B", deps=deps)

    assert out.has_contradictions is True
    assert len(out.contradictions) == 1
    h = out.contradictions[0]
    assert h.path == "docs/old.md"
    assert h.score == pytest.approx(0.78)
    assert h.reason == "contradicts X"
    assert h.category == "status_mismatch"
    assert h.claim == "The system now uses option B."


@pytest.mark.unit
def test_no_results_yields_no_contradictions() -> None:
    deps, _ = _build_deps(results=[])
    out = run_contradict("benign content", deps=deps)
    assert out.has_contradictions is False
    assert out.contradictions == []


# ---------------------------------------------------------------------------
# Param pass-through
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_top_k_threshold_top_claims_pass_through() -> None:
    deps, captured = _build_deps()
    run_contradict("c", top_k=8, threshold=0.6, top_claims=4, deps=deps)
    assert captured["top_k"] == 8
    assert captured["threshold"] == pytest.approx(0.6)
    assert captured["top_claims"] == 4


@pytest.mark.unit
def test_scope_passed_through_unconditionally() -> None:
    deps, captured = _build_deps()
    run_contradict("c", scope=Scope.ALL_AGENTS, deps=deps)
    assert captured["scope"] is Scope.ALL_AGENTS


@pytest.mark.unit
def test_agent_only_passed_when_explicitly_set() -> None:
    deps, captured = _build_deps()
    run_contradict("c", agent="builder", deps=deps)
    assert captured["agent"] == "builder"


@pytest.mark.unit
def test_agent_omitted_from_check_call_when_none() -> None:
    deps, captured = _build_deps()
    run_contradict("c", agent=None, deps=deps)
    assert "agent" not in captured  # legacy WS2-B contract: omit, don't pass None


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(raises=True)
    out = run_contradict("c", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.contradictions == []
    assert out.has_contradictions is False


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_envelope_includes_category_and_claim() -> None:
    out = ContradictOutput(
        content="c",
        contradictions=[
            ContradictionHit(path="p", score=0.5, reason="r", snippet="s", category="overstatement", claim="C")
        ],
        has_contradictions=True,
    )
    env = contradict_output_to_envelope(out)
    assert env["content"] == "c"
    assert env["has_contradictions"] is True
    assert env["error"] == ""
    assert env["contradictions"] == [
        {"path": "p", "score": 0.5, "reason": "r", "snippet": "s", "category": "overstatement", "claim": "C"}
    ]


@pytest.mark.unit
def test_envelope_carries_error_when_present() -> None:
    out = ContradictOutput(content="c", error="ConnectionError: Neo4j down")
    env = contradict_output_to_envelope(out)
    assert env["error"].startswith("ConnectionError")
    assert env["has_contradictions"] is False
    assert env["contradictions"] == []
