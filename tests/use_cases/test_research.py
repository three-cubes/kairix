"""Unit tests for ``kairix.use_cases.research.run_research_use_case``."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.use_cases.research import (
    ResearchDeps,
    ResearchOutput,
    research_output_to_envelope,
    run_research_use_case,
)

pytestmark = pytest.mark.unit


def _build_deps(
    *,
    result: dict[str, Any] | None = None,
    raises: bool = False,
) -> tuple[ResearchDeps, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_research(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        if raises:
            raise RuntimeError("research crashed")
        return result or {}

    return ResearchDeps(research_fn=fake_research), captured


# ---------------------------------------------------------------------------
# Happy path projection
# ---------------------------------------------------------------------------


def test_projects_dict_into_dataclass() -> None:
    payload = {
        "query": "what's our entity coverage?",
        "synthesis": "We have 1240 entities across 17 collections.",
        "retrieved_chunks": [{"path": "/r1.md"}, {"path": "/r2.md"}],
        "gaps": ["unknown about Acme's tier"],
        "confidence": 0.82,
        "turns": 3,
    }
    deps, captured = _build_deps(result=payload)
    out = run_research_use_case("ignored — taken from payload", max_turns=4, deps=deps)

    assert out.error == ""
    assert out.query == "what's our entity coverage?"
    assert out.synthesis.startswith("We have 1240")
    assert out.confidence == pytest.approx(0.82)
    assert out.turns == 3
    assert len(out.retrieved_chunks) == 2
    assert out.gaps == ["unknown about Acme's tier"]
    # Caller's max_turns reaches the orchestrator unmodified (within bounds).
    assert captured["max_turns"] == 4


def test_retrieved_chunks_truncated_to_10() -> None:
    payload = {"retrieved_chunks": [{"path": f"/c{i}"} for i in range(25)]}
    deps, _ = _build_deps(result=payload)
    out = run_research_use_case("q", deps=deps)
    assert len(out.retrieved_chunks) == 10


def test_query_falls_back_to_caller_when_payload_omits_it() -> None:
    deps, _ = _build_deps(result={})
    out = run_research_use_case("the original query", deps=deps)
    assert out.query == "the original query"


# ---------------------------------------------------------------------------
# max_turns clamping
# ---------------------------------------------------------------------------


def test_max_turns_clamps_below_one_to_one() -> None:
    deps, captured = _build_deps()
    run_research_use_case("q", max_turns=0, deps=deps)
    assert captured["max_turns"] == 1


def test_max_turns_clamps_above_ten_to_ten() -> None:
    deps, captured = _build_deps()
    run_research_use_case("q", max_turns=99, deps=deps)
    assert captured["max_turns"] == 10


def test_max_turns_negative_clamps_to_one() -> None:
    deps, captured = _build_deps()
    run_research_use_case("q", max_turns=-3, deps=deps)
    assert captured["max_turns"] == 1


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_orchestrator_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(raises=True)
    out = run_research_use_case("q", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.synthesis == ""
    assert out.confidence == 0.0


def test_payload_error_field_propagates() -> None:
    deps, _ = _build_deps(result={"error": "no LLM credits"})
    out = run_research_use_case("q", deps=deps)
    assert out.error == "no LLM credits"


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_envelope_includes_all_fields() -> None:
    out = ResearchOutput(
        query="q",
        synthesis="s",
        retrieved_chunks=[{"path": "/c"}],
        gaps=["g"],
        confidence=0.5,
        turns=2,
    )
    env = research_output_to_envelope(out)
    assert env == {
        "query": "q",
        "synthesis": "s",
        "retrieved_chunks": [{"path": "/c"}],
        "gaps": ["g"],
        "confidence": 0.5,
        "turns": 2,
        "error": "",
    }
