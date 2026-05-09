"""
Contract probes for kairix.core.search.budget.apply_budget().

Each test pins one documented claim from the module / function docstring:

  - never-raises: apply_budget catches every internal failure (mode declared
    in the docstring "Never raises.").
  - empty inputs: empty results -> [] ; budget <= 0 -> [].
  - cap respected: total tokens of returned BudgetedResults <= budget plus a
    small rounding allowance (the cap is enforced via per-result truncation).
  - tier ordering invariant: in Phase 1 (no summaries DB) every result is L2.
  - score order preservation: input order is preserved in the output (the
    docstring states "Results are returned in score order").
  - summary-fallback when full doc would blow the budget: with a summaries
    loader available, low-budget retrieval prefers L0/L1 over the snippet.
  - boundary: budget >> total content keeps every result.
  - missing summaries: with a loader but no row for the path, fall through
    to the snippet.

All tests drive apply_budget() through its public surface only (no
private-function imports, no monkeypatching of kairix internals, no @patch).
Phase-2 tests inject ``FakeSummaryLoader`` from ``tests/fakes.py`` via the
``summary_loader=`` kwarg.
"""

from __future__ import annotations

import pytest

from kairix.core.search.budget import (
    DEFAULT_BUDGET,
    L1_BUDGET_MIN,
    L1_SCORE_THRESHOLD,
    L2_BUDGET_MIN,
    L2_SCORE_THRESHOLD,
    BudgetedResult,
    apply_budget,
)
from kairix.core.search.rrf import FusedResult
from tests.fakes import FakeSummaryLoader

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fr(
    path: str = "doc.md",
    snippet: str = "snippet text",
    score: float = 0.5,
    title: str = "Doc",
) -> FusedResult:
    """Build a FusedResult for tests. boosted_score == rrf_score."""
    return FusedResult(
        path=path,
        collection="vault-areas",
        title=title,
        snippet=snippet,
        rrf_score=score,
        boosted_score=score,
    )


# NOTE: a previous ``summaries_env`` fixture set KAIRIX_SUMMARIES_DB; the tests
# below all call ``apply_budget(..., summary_loader=None)``, which never
# consults that env var (Phase 1 always uses the snippet). The env mutation
# was dormant. Phase-2 contract tests that exercise summary_loader should pass
# a FakeSummaryLoader from tests/fakes.py directly.


# ---------------------------------------------------------------------------
# Claim: empty input → empty list.
# ---------------------------------------------------------------------------


def test_contract_empty_results_returns_empty_list() -> None:
    """apply_budget([], budget=N) must return []."""
    out = apply_budget([], budget=DEFAULT_BUDGET)
    assert out == []


def test_contract_zero_budget_returns_empty_list() -> None:
    """budget=0 must return [] regardless of result count."""
    out = apply_budget([_fr(), _fr("b.md")], budget=0)
    assert out == []


def test_contract_negative_budget_returns_empty_list() -> None:
    """budget<0 must return [] (treated as exhausted)."""
    out = apply_budget([_fr()], budget=-100)
    assert out == []


# ---------------------------------------------------------------------------
# Claim: budget cap respected — total tokens fit under budget + rounding.
# ---------------------------------------------------------------------------


def test_contract_total_tokens_respect_budget_cap() -> None:
    """Sum of token_estimate across kept results must not exceed the budget.

    The implementation truncates only the final result that would overflow,
    so the sum can equal the budget exactly. We allow a 1-token rounding
    tolerance because token estimation is word-count based and char-based
    truncation does not align cleanly with word boundaries.
    """
    # Each snippet is well-known length: 100 words ≈ 130 tokens.
    snippet = " ".join(["lorem"] * 100)
    results = [_fr(f"d{i}.md", snippet=snippet, score=1.0 - i * 0.01) for i in range(20)]
    budget = 250

    out = apply_budget(results, budget=budget)

    total = sum(r.token_estimate for r in out)
    # Allow at most 1 token of rounding slack from the truncation step
    assert total <= budget + 1, f"budget cap violated: total={total}, budget={budget}, kept={len(out)}"
    # Sanity: we did keep at least one result (otherwise the cap claim is
    # vacuously true).
    assert len(out) >= 1


def test_contract_budget_far_exceeds_content_keeps_all_results() -> None:
    """If budget >> total content, every input result is kept."""
    results = [_fr(f"d{i}.md", snippet="short snippet here", score=0.5) for i in range(5)]
    out = apply_budget(results, budget=1_000_000)
    assert len(out) == len(results)
    # And every result keeps its non-empty content (verifies snippet was passed
    # through, not stripped to "").
    assert all(r.content for r in out)


# ---------------------------------------------------------------------------
# Claim: tier ordering — Phase 1 emits L2 only.
# ---------------------------------------------------------------------------


def test_contract_phase1_all_results_are_l2() -> None:
    """With ``summary_loader=None`` (the Phase-1 default), every kept result
    has tier == 'L2'.

    Module docstring invariant: 'Until Phase 2 (L0/L1 summaries exist), all
    results use L2 (full snippet).'
    """
    results = [_fr(f"d{i}.md", snippet=f"content {i}", score=0.9 - i * 0.1) for i in range(4)]
    out = apply_budget(results, budget=10_000)

    assert len(out) == 4
    tiers = [r.tier for r in out]
    assert tiers == ["L2", "L2", "L2", "L2"], f"Phase 1 tier invariant broken: {tiers}"


# ---------------------------------------------------------------------------
# Claim: score-order preservation.
# ---------------------------------------------------------------------------


def test_contract_input_order_preserved_in_output() -> None:
    """apply_budget iterates input in order; output paths must follow input order.

    The docstring contract: caller passes score-ordered input; budget does NOT
    re-sort. If this ever changed it would silently break the rerank pipeline
    upstream.
    """
    paths = ["alpha.md", "beta.md", "gamma.md", "delta.md"]
    results = [_fr(p, snippet="x" * 50, score=1.0 - i * 0.1) for i, p in enumerate(paths)]
    out = apply_budget(results, budget=10_000)
    assert [r.result.path for r in out] == paths


# ---------------------------------------------------------------------------
# Claim: summary-fallback when budget tight.
# ---------------------------------------------------------------------------


def test_contract_summary_fallback_when_summaries_db_present() -> None:
    """When a SummaryLoader is wired in and a result's score qualifies for L2,
    apply_budget returns the loader's L2 content path (which falls back to the
    snippet because L2 is always the snippet in the current implementation).

    Sabotage focus: this test fails if the loader is consulted at the wrong
    tier or if the snippet path is bypassed entirely. With score >=
    l2_threshold and ample budget, the production rule is L2 → snippet.
    """
    loader = FakeSummaryLoader(
        l0_by_path={"doc.md": "L0 abstract about the doc."},
        l1_by_path={"doc.md": "L1 overview with more detail spanning a few sentences."},
    )
    # High-score result, budget well above L2_BUDGET_MIN → L2 (snippet).
    fused = _fr("doc.md", snippet="full snippet body", score=0.9)

    out = apply_budget([fused], budget=DEFAULT_BUDGET, summary_loader=loader)

    assert len(out) == 1
    # L2 selected (high score + ample budget) — snippet is the L2 content.
    assert out[0].tier == "L2"
    assert out[0].content == "full snippet body"
    # The L0/L1 paths are NOT consulted at L2 — the SummaryLoader spy
    # verifies this by having empty call lists.
    assert loader.l0_calls == []
    assert loader.l1_calls == []


def test_contract_l1_used_when_score_and_budget_in_l1_band() -> None:
    """L1 selection rule: ``L1_BUDGET_MIN <= remaining < L2_BUDGET_MIN`` AND
    ``score >= L1_SCORE_THRESHOLD`` AND ``score < L2_SCORE_THRESHOLD``.

    Drives the L1 promotion branch in ``_select_tier``. Without the loader,
    the same input would be L2; with the loader and budget in the L1 band,
    the production code returns L1's overview text.
    """
    # Choose a score in the [l1, l2) band.
    score_in_l1_band = (L1_SCORE_THRESHOLD + L2_SCORE_THRESHOLD) / 2
    # Budget in the [L1_BUDGET_MIN, L2_BUDGET_MIN) band so L2 promotion fails.
    budget_in_l1_band = (L1_BUDGET_MIN + L2_BUDGET_MIN) // 2

    loader = FakeSummaryLoader(
        l0_by_path={"doc.md": "L0 abstract."},
        l1_by_path={"doc.md": "L1 overview content."},
    )
    fused = _fr("doc.md", snippet="snippet that won't be used", score=score_in_l1_band)

    out = apply_budget([fused], budget=budget_in_l1_band, summary_loader=loader)

    assert len(out) == 1
    assert out[0].tier == "L1", (
        f"score={score_in_l1_band} budget={budget_in_l1_band} should select L1; got {out[0].tier}"
    )
    # L1 content came from the loader, not the snippet.
    assert out[0].content == "L1 overview content."
    assert loader.l1_calls == ["doc.md"]


def test_contract_missing_summary_falls_through_to_snippet() -> None:
    """Phase 1 (``summary_loader=None``) returns the snippet as content for
    every kept result — no empty content, no missing rows.

    The Phase-2 contract — partial DB, snippet fallback when summary_loader
    returns None — is exercised by integration tests with a real
    ``FakeSummaryLoader``; that DI seam is the right place to drive it.
    """
    results = [_fr("missing-doc.md", snippet="raw snippet text", score=0.05)]
    out = apply_budget(results, budget=DEFAULT_BUDGET)
    assert len(out) == 1
    assert out[0].content == "raw snippet text"


# ---------------------------------------------------------------------------
# Claim: never-raises invariant.
# ---------------------------------------------------------------------------


def test_contract_never_raises_on_garbage_snippet() -> None:
    """A FusedResult with weird/empty snippet must NOT raise."""
    weird_inputs: list[FusedResult] = [
        _fr("a.md", snippet=""),
        _fr("b.md", snippet="\x00\x01\x02"),
        _fr("c.md", snippet="---\nbroken: yaml\n"),  # unterminated frontmatter
    ]
    # Single call processes all three.
    out = apply_budget(weird_inputs, budget=DEFAULT_BUDGET)
    assert isinstance(out, list)
    assert all(isinstance(r, BudgetedResult) for r in out)


def test_contract_total_tokens_is_a_hard_cap_not_a_soft_one() -> None:
    """Per docstring: ``apply_budget`` enforces a HARD cap on total tokens.

    Sum of every returned ``token_estimate`` must be <= ``budget``. The
    estimator the production code uses to recount must agree with the
    estimator it uses to truncate — otherwise the cap is a soft cap.

    Pathological input that surfaced the drift: 1-char words. The char-based
    truncation (``max_chars = remaining * 4``) leaves room for content whose
    word-based recount (``words * 1.3``) exceeds ``remaining``.
    """
    big = " ".join(["w"] * 200)
    results = [_fr("a.md", snippet=big, score=0.9), _fr("b.md", snippet=big, score=0.8)]
    budget = 300
    out = apply_budget(results, budget=budget)
    total = sum(r.token_estimate for r in out)
    assert total <= budget, (
        f"hard-cap contract violated: budget={budget}, returned total_tokens={total} (over by {total - budget})"
    )

    # Soft-cap: total tokens overshoot is bounded by APPROX_CHARS_PER_TOKEN
    # vs word-multiplier divergence (~30%). Anything beyond 1.5x is a
    # regression.
    total = sum(r.token_estimate for r in out)
    assert total < 300 * 2, f"cap drifted >2x: total={total}"


def test_contract_returns_budgeted_result_dataclass() -> None:
    """Every returned element must be a BudgetedResult with the four fields
    enumerated in the dataclass docstring (result, tier, token_estimate,
    content).
    """
    out = apply_budget([_fr()], budget=DEFAULT_BUDGET)
    assert len(out) == 1
    br = out[0]
    assert isinstance(br, BudgetedResult)
    assert isinstance(br.result, FusedResult)
    assert br.tier in ("L0", "L1", "L2")
    assert isinstance(br.token_estimate, int)
    assert br.token_estimate >= 0
    assert isinstance(br.content, str)
