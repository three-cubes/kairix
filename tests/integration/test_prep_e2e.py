"""End-to-end integration tests for the prep tiered-summary surface.

Wires the prep use case (``kairix.use_cases.prep.run_prep``) through real
``_format_context`` + the envelope projection. Two real kairix components
collaborate per test:

  - The use-case orchestrator (run_prep) drives the L0/L1 tier branching
    and the no-content short-circuit;
  - The envelope projection (prep_output_to_envelope) emits the documented
    JSON shape — `query`, `tier`, `summary`, `tokens`, `sources`, `error`.

The search and chat callables are the system-boundary fakes — they return
canned ``SearchResult`` (real dataclass) and a captured chat string. No
``@patch`` of kairix internals; everything flows through ``PrepDeps``.

What's covered here that unit + BDD don't catch:
  - The L0/L1 token-budget contract: L0 budgets 1500, L1 budgets 3000,
    and the *recorded* call into the search seam carries that budget.
  - The `_MIN_USEFUL_SNIPPET_CHARS` filter cooperates with the source
    projection — short-snippet hits never reach the LLM context.
  - The full envelope shape (every documented key present, types correct)
    after a successful run, and the empty/no-content branch's text.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.search.budget import BudgetedResult
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.core.search.rrf import FusedResult
from kairix.use_cases.prep import PrepDeps, prep_output_to_envelope, run_prep

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test fixtures — boundary fakes
# ---------------------------------------------------------------------------


def _make_budgeted(path: str, title: str, content: str, score: float) -> BudgetedResult:
    """Build a BudgetedResult with a real FusedResult inside."""
    fused = FusedResult(
        path=path,
        collection="default",
        title=title,
        snippet=content[:200],
        rrf_score=score,
        boosted_score=score,
        in_bm25=True,
    )
    return BudgetedResult(result=fused, tier="L2", token_estimate=50, content=content)


def _make_search_result(query: str, hits: list[BudgetedResult]) -> SearchResult:
    return SearchResult(
        query=query,
        intent=QueryIntent.SEMANTIC,
        results=hits,
        total_tokens=sum(h.token_estimate for h in hits),
        latency_ms=5.0,
    )


class _RecordingSearch:
    """Fake search seam — records every call and returns the canned result.

    Lives at the system boundary (replaces the real
    ``build_search_pipeline().search`` callable) so the integration test
    can assert the budget value the use case threads through.
    """

    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SearchResult:
        self.calls.append(dict(kwargs))
        return self._result


class _RecordingChat:
    """Fake chat seam — records messages + max_tokens, returns canned summary."""

    def __init__(self, summary: str) -> None:
        self._summary = summary
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return self._summary


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prep_l0_runs_pipeline_and_emits_documented_envelope() -> None:
    """Full happy path: L0 prep runs classify → search → context → chat →
    envelope. The envelope carries every key the MCP/CLI contract documents
    and the source list is drawn from the search hits' titles.

    Sabotage: if ``run_prep`` stopped projecting ``sources`` from the
    search hits' titles, the asserted list ``["Architecture Overview", ...]``
    would not match.
    """
    hits = [
        _make_budgeted(
            "notes/architecture.md",
            "Architecture Overview",
            "Kairix uses hybrid BM25 + vector search with RRF fusion for ranking. "
            "Long-enough snippet that exceeds the 40-char minimum for context.",
            score=0.9,
        ),
        _make_budgeted(
            "notes/performance.md",
            "Performance Notes",
            "NDCG@10 target is 0.78 overall. Temporal category requires temporal_boost. "
            "Snippet must also exceed the minimum useful length floor.",
            score=0.7,
        ),
    ]
    search = _RecordingSearch(_make_search_result("architecture overview", hits))
    chat = _RecordingChat("Kairix uses hybrid retrieval with RRF fusion.")

    out = run_prep("architecture overview", tier="l0", deps=PrepDeps(search_fn=search, chat_fn=chat))

    envelope = prep_output_to_envelope(out)
    # Documented envelope shape — every key present.
    assert set(envelope.keys()) == {"query", "tier", "summary", "tokens", "sources", "error"}
    assert envelope["query"] == "architecture overview"
    assert envelope["tier"] == "l0"
    assert envelope["error"] == ""
    assert envelope["summary"] == "Kairix uses hybrid retrieval with RRF fusion."
    # Sources projected from the FusedResult.title of each hit.
    assert envelope["sources"] == ["Architecture Overview", "Performance Notes"]
    # Tokens estimated from the summary string itself.
    assert envelope["tokens"] > 0


def test_prep_l0_threads_budget_1500_into_search_seam() -> None:
    """The L0 path's token budget contract: prep must call ``search`` with
    budget=1500. L1 uses 3000 (see next test). This is the load-bearing
    knob that keeps L0 cheap.

    Sabotage: if ``_L0_BUDGET`` regressed to e.g. 3000, the recorded
    search call's ``budget=1500`` assertion would fail.
    """
    hits = [
        _make_budgeted(
            "notes/x.md",
            "X",
            "Long-enough snippet to exceed the 40-char minimum useful threshold for context.",
            score=0.8,
        ),
    ]
    search = _RecordingSearch(_make_search_result("topic", hits))
    chat = _RecordingChat("summary")

    run_prep("topic", tier="l0", deps=PrepDeps(search_fn=search, chat_fn=chat))

    assert len(search.calls) == 1
    assert search.calls[0]["budget"] == 1500
    assert chat.calls[0]["max_tokens"] == 150  # _L0_MAX_TOKENS


def test_prep_l1_threads_budget_3000_into_search_seam() -> None:
    """L1 path uses the wider 3000-token retrieval budget and the 600-token
    LLM cap. Symmetric with the L0 contract above.

    Sabotage: dropping the L1 branch from ``run_prep`` would make this
    test see budget=1500 (the L0 default) and fail.
    """
    hits = [
        _make_budgeted(
            "notes/x.md",
            "X",
            "Long-enough snippet to exceed the 40-char minimum useful threshold for context.",
            score=0.8,
        ),
    ]
    search = _RecordingSearch(_make_search_result("topic", hits))
    chat = _RecordingChat("structured overview")

    out = run_prep("topic", tier="l1", deps=PrepDeps(search_fn=search, chat_fn=chat))

    assert out.tier == "l1"
    assert search.calls[0]["budget"] == 3000
    assert chat.calls[0]["max_tokens"] == 600  # _L1_MAX_TOKENS


def test_prep_filters_short_snippets_and_skips_llm_when_nothing_useful() -> None:
    """When every hit's content is shorter than ``_MIN_USEFUL_SNIPPET_CHARS``
    (40), the use case returns the canned "no relevant documents" summary
    WITHOUT calling the chat backend. This is the #254 guardrail against
    grounding the LLM on title-only fragments.

    Sabotage: if the 40-char filter were removed (or set to 0), the chat
    seam would be called and ``chat.calls`` would be non-empty — and the
    envelope summary would equal the chat-fake string instead of the
    "no relevant documents" message.
    """
    # Every hit's content is too short to pass the 40-char usefulness filter.
    hits = [
        _make_budgeted("a.md", "A", "tiny", score=0.9),
        _make_budgeted("b.md", "B", "also short", score=0.8),
    ]
    search = _RecordingSearch(_make_search_result("topic", hits))
    chat = _RecordingChat("THIS SHOULD NOT APPEAR")

    out = run_prep("topic", tier="l0", deps=PrepDeps(search_fn=search, chat_fn=chat))

    # LLM was never called — the boundary was protected.
    assert chat.calls == []
    assert "No relevant documents found" in out.summary
    assert out.error == ""
    assert out.sources == []
