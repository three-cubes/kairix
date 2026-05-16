"""End-to-end integration tests for one-turn research synthesis.

Wires the LangGraph research orchestrator
(``kairix.agents.research.graph.run_research``) through real graph
construction, real node functions (classify → retrieve → evaluate →
route → synthesise), and the use-case envelope projection
(``run_research_use_case``).

Single turn only — the evaluator returns confidence ≥ 0.5 on the first
pass so ``route_after_evaluation`` routes straight to ``synthesise``
and the graph terminates. Keeps the test fast and deterministic; the
multi-turn refinement branch has its own BDD coverage.

What's covered here that unit + BDD don't catch:
  - The full graph compiles and invokes end-to-end with injected deps.
  - The retrieve node ↔ evaluate-sufficiency node ↔ synthesise node
    cooperate — `state["retrieved_chunks"]` flows from one to the next.
  - The use-case envelope (``research_output_to_envelope``) projects
    every documented field after a real graph run.
  - LLM-only mocking lives at the system boundary (FakeLLMBackend);
    every other component is the production code path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairix.agents.research.graph import ResearchGraphDeps, run_research
from kairix.core.search.budget import BudgetedResult
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.core.search.rrf import FusedResult
from kairix.use_cases.research import (
    ResearchDeps,
    research_output_to_envelope,
    run_research_use_case,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


def _make_search_result(query: str, paths: list[str]) -> SearchResult:
    """Build a SearchResult carrying a small set of canned hits."""
    results: list[BudgetedResult] = []
    for i, p in enumerate(paths):
        fr = FusedResult(
            path=p,
            collection="default",
            title=p.rsplit("/", 1)[-1].replace(".md", "").title(),
            snippet=f"snippet about {p}",
            rrf_score=0.9 - i * 0.05,
            boosted_score=0.9 - i * 0.05,
            in_bm25=True,
        )
        results.append(
            BudgetedResult(
                result=fr,
                tier="L2",
                token_estimate=80,
                content=f"Detailed content from {p} explaining the topic.",
            )
        )
    return SearchResult(query=query, intent=QueryIntent.SEMANTIC, results=results, latency_ms=4.0)


class _FakeSearch:
    """Records each search call; returns the canned result."""

    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SearchResult:
        self.calls.append(dict(kwargs))
        return self._result


class _FakeClassify:
    """Returns a fixed intent."""

    def __init__(self, intent: QueryIntent = QueryIntent.SEMANTIC) -> None:
        self._intent = intent
        self.calls: int = 0

    def __call__(self, query: str) -> QueryIntent:
        del query
        self.calls += 1
        return self._intent


class _SequencedLLM:
    """LLM backend whose chat() returns the next response in a queue.

    The research graph calls chat() at least twice on a one-turn happy
    path: once for evaluate_sufficiency (must include a confidence
    JSON object) and once for synthesise (returns the answer string).
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if not self._responses:
            return ""
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _one_turn_deps(
    synthesis_text: str = "Found the answer in two sources.",
) -> tuple[
    ResearchGraphDeps,
    _FakeSearch,
    _SequencedLLM,
    _FakeClassify,
]:
    """Construct a one-turn-happy-path deps bag.

    Sufficiency response = JSON with confidence 0.9 so the router goes
    straight to synthesise; synthesise response = the canned answer.
    """
    search = _FakeSearch(_make_search_result("kairix architecture", ["notes/arch.md", "notes/perf.md"]))
    classifier = _FakeClassify(QueryIntent.SEMANTIC)
    llm = _SequencedLLM(
        responses=[
            # 1st call — evaluate_sufficiency: high-confidence sufficient.
            json.dumps(
                {
                    "confidence": 0.9,
                    "sufficient": True,
                    "refined_query": None,
                    "gaps": [],
                    "reasoning": "Sources cover the question.",
                }
            ),
            # 2nd call — synthesise: final answer.
            synthesis_text,
        ]
    )
    deps = ResearchGraphDeps(
        search_fn=search,
        classify_fn=classifier,
        llm_backend=llm,
    )
    return deps, search, llm, classifier


def test_research_one_turn_synthesises_and_envelope_carries_documented_fields() -> None:
    """A single happy-path turn produces a synthesis + a populated
    envelope. ``retrieved_chunks`` flow from retrieve → evaluate →
    synthesise; the projection clips at 10 chunks (here we only seed 2,
    so all survive).

    Sabotage: if ``run_research`` stopped accumulating
    ``retrieved_chunks`` (e.g. if the retrieve node returned an empty
    list instead of the search hits), the envelope would carry zero
    chunks and the assertion ``len(envelope["retrieved_chunks"]) == 2``
    would fail.
    """
    deps, search, llm, classifier = _one_turn_deps("Kairix retrieval is hybrid BM25 + vector.")

    # Drive the orchestrator directly with one turn — the use case wraps it.
    out = run_research_use_case(
        "How does kairix do retrieval?",
        max_turns=1,
        deps=ResearchDeps(research_fn=lambda **kw: run_research(**kw, deps=deps)),
    )

    envelope = research_output_to_envelope(out)

    # Documented envelope shape — every field present.
    assert set(envelope.keys()) == {
        "query",
        "synthesis",
        "retrieved_chunks",
        "gaps",
        "confidence",
        "turns",
        "error",
    }
    assert envelope["query"] == "How does kairix do retrieval?"
    assert envelope["synthesis"] == "Kairix retrieval is hybrid BM25 + vector."
    assert envelope["error"] == ""
    # Retrieve fed two hits through; evaluate + synthesise didn't drop them.
    assert len(envelope["retrieved_chunks"]) == 2
    chunk_paths = sorted(c["path"] for c in envelope["retrieved_chunks"])
    assert chunk_paths == ["notes/arch.md", "notes/perf.md"]
    # Confidence came through the parser chain.
    assert envelope["confidence"] == pytest.approx(0.9)
    # Single turn — graph went classify → retrieve → evaluate → synthesise (no refine).
    assert envelope["turns"] == 0
    # Exactly two LLM calls: evaluate_sufficiency + synthesise.
    assert len(llm.calls) == 2
    # Search and classifier each ran once.
    assert len(search.calls) == 1
    assert classifier.calls == 1


def test_research_one_turn_threads_initial_budget_into_search() -> None:
    """First retrieve turn uses ``INITIAL_BUDGET`` (3000), not the
    refinement budget (5000). This is the load-bearing budget contract
    for fresh queries — refinement turns expand it.

    Sabotage: if the retrieve node read ``REFINEMENT_BUDGET`` on the
    first turn (or read no budget at all), the recorded search call's
    ``budget=3000`` assertion would fail.
    """
    deps, search, _llm, _classifier = _one_turn_deps()

    run_research_use_case(
        "kairix architecture overview",
        max_turns=1,
        deps=ResearchDeps(research_fn=lambda **kw: run_research(**kw, deps=deps)),
    )

    assert len(search.calls) == 1
    assert search.calls[0]["budget"] == 3000


def test_research_clamps_max_turns_below_floor_to_one() -> None:
    """max_turns=0 (or negative) is clamped to 1 by the use case before
    the orchestrator sees it. Keeps the graph from hitting a divide-by-
    zero / off-by-one in route_after_evaluation.

    Sabotage: if the floor clamp (``_MAX_TURNS_FLOOR = 1``) were
    removed, the graph would receive max_turns=0 and the router's
    ``turns < max_turns - 1`` math would misbehave; even when the
    happy path still produces an answer here, the recorded ``max_turns``
    that reaches ``run_research`` is the load-bearing contract.
    """
    deps, _search, _llm, _classifier = _one_turn_deps()
    captured: dict[str, Any] = {}

    def _research_with_capture(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return run_research(**kwargs, deps=deps)

    out = run_research_use_case(
        "anything",
        max_turns=0,  # below the floor.
        deps=ResearchDeps(research_fn=_research_with_capture),
    )

    assert captured["max_turns"] == 1  # clamped up from 0.
    assert out.error == ""
    assert out.synthesis  # synth still produced output.
