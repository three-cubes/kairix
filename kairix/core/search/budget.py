"""
Token budget enforcer for the kairix hybrid search pipeline.

Applies a hard token cap to search results, assigning each result a tier:
  L0 — abstract only (~100 tokens); not yet generated in Phase 1
  L1 — structural overview (~2,000 tokens); not yet generated in Phase 1
  L2 — full document snippet (current)

Until Phase 2 (L0/L1 summaries exist), all results use L2 (full snippet).
Results are returned in score order, truncated when the budget is exhausted.

Constants:
  DEFAULT_BUDGET = 3000   Hard cap on total tokens per retrieval call
  APPROX_CHARS_PER_TOKEN = 4  Conservative estimate for token counting

Thresholds for tier promotion (Phase 2+):
  L1_SCORE_THRESHOLD = 0.15  Minimum score to promote to L1
  L2_SCORE_THRESHOLD = 0.25  Minimum score to promote to L2
  L1_BUDGET_MIN = 500        Minimum remaining budget to load L1
  L2_BUDGET_MIN = 2000       Minimum remaining budget to load L2
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from kairix.core.search.rrf import FusedResult
from kairix.text import APPROX_CHARS_PER_TOKEN, strip_frontmatter
from kairix.text import estimate_tokens as _estimate_tokens_word

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BUDGET: int = 3_000

# Tier thresholds (Phase 2+ — unused until L0/L1 summaries exist)
L1_SCORE_THRESHOLD: float = 0.15
L2_SCORE_THRESHOLD: float = 0.25
L1_BUDGET_MIN: int = 500
L2_BUDGET_MIN: int = 2_000

Tier = Literal["L0", "L1", "L2"]


# ---------------------------------------------------------------------------
# SummaryLoader protocol — Phase 2 injection seam
# ---------------------------------------------------------------------------


@runtime_checkable
class SummaryLoader(Protocol):
    """Loader surface for L0 / L1 document summaries.

    Implementations own their own data source. Production
    ``_DefaultSummaryLoader`` opens the summaries SQLite DB lazily; tests
    construct ``FakeSummaryLoader`` from ``tests/fakes.py`` with in-memory
    abstracts/overviews.
    """

    def get_l0(self, path: str) -> str | None: ...

    def get_l1(self, path: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BudgetedResult:
    """A FusedResult annotated with its tier and token count."""

    result: FusedResult
    tier: Tier
    token_estimate: int
    content: str  # The actual text returned for this result at this tier


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_budget(
    results: list[FusedResult],
    budget: int = DEFAULT_BUDGET,
    l1_threshold: float = L1_SCORE_THRESHOLD,
    l2_threshold: float = L2_SCORE_THRESHOLD,
    *,
    summary_loader: SummaryLoader | None = None,
) -> list[BudgetedResult]:
    """
    Apply token budget to fused results, assigning each a tier and truncating at cap.

    Phase 1 (``summary_loader=None``): all results get tier ``L2`` and the
    snippet (frontmatter-stripped) is returned as content.

    Phase 2+ (``summary_loader=<SummaryLoader>``): tier is selected per
    score/budget; ``L0`` returns the abstract via ``loader.get_l0``, ``L1``
    returns the overview via ``loader.get_l1`` (falling back to ``L0``),
    ``L2`` returns the snippet. The snippet is the fallback whenever the
    loader returns ``None`` or raises.

    Args:
        results:        FusedResult list from rrf() / entity_boost(), score-ordered.
        budget:         Hard token cap. Default DEFAULT_BUDGET.
        l1_threshold:   Score threshold for L1 promotion (Phase 2+).
        l2_threshold:   Score threshold for L2 promotion (Phase 2+).
        summary_loader: Phase 2 loader. ``None`` (default) keeps Phase 1
                        behaviour. Tests pass ``FakeSummaryLoader``.

    Returns:
        List of BudgetedResult, truncated when budget exhausted.
        Empty list if budget is 0 or no results.
        Never raises.
    """
    if not results or budget <= 0:
        return []

    try:
        return _apply_budget_impl(results, budget, l1_threshold, l2_threshold, summary_loader)
    except Exception as e:
        logger.warning("apply_budget: unexpected error — %s", e)
        return []


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate token count using the canonical word-based estimator.

    Returns 0 for an empty / falsy text so callers don't need to guard.
    """
    if not text:
        return 0
    return _estimate_tokens_word(text)


def _apply_budget_impl(
    results: list[FusedResult],
    budget: int,
    l1_threshold: float,
    l2_threshold: float,
    summary_loader: SummaryLoader | None,
) -> list[BudgetedResult]:
    """Internal budget implementation."""
    budgeted: list[BudgetedResult] = []
    remaining = budget

    for result in results:
        if remaining <= 0:
            break

        tier = _select_tier(result, remaining, l1_threshold, l2_threshold, summary_loader)
        content = _get_content_for_tier(result, tier, summary_loader)
        tokens = _estimate_tokens(content)

        # Hard cap: if even this result exceeds remaining, truncate content
        # using the same estimator we recount with — char-based truncation
        # plus word-based recount disagree on degenerate inputs (e.g. 1-char
        # words), causing the cap to drift to a soft cap. The loop below
        # truncates progressively until the recount fits.
        if tokens > remaining:
            max_chars = remaining * APPROX_CHARS_PER_TOKEN
            content = content[:max_chars]
            tokens = _estimate_tokens(content)
            # If the word-based recount still exceeds, shave content until it fits.
            while tokens > remaining and content:
                # Drop ~10% of the remaining content per pass; converges fast.
                cut = max(1, len(content) // 10)
                content = content[:-cut]
                tokens = _estimate_tokens(content)

        budgeted.append(
            BudgetedResult(
                result=result,
                tier=tier,
                token_estimate=tokens,
                content=content,
            )
        )
        remaining -= tokens

    return budgeted


def _select_tier(
    result: FusedResult,
    remaining_budget: int,
    l1_threshold: float,
    l2_threshold: float,
    summary_loader: SummaryLoader | None,
) -> Tier:
    """
    Select the appropriate tier for a result given the current budget.

    Phase 1 (no loader): always return L2 (full snippet).
    Phase 2+: L0 by default; promote to L1 or L2 based on score and budget.
    """
    if summary_loader is None:
        return "L2"

    score = result.boosted_score

    if remaining_budget >= L2_BUDGET_MIN and score >= l2_threshold:
        return "L2"
    elif remaining_budget >= L1_BUDGET_MIN and score >= l1_threshold:
        return "L1"
    else:
        return "L0"


def _lookup_tier_summary(
    summary_loader: SummaryLoader,
    path: str,
    tier: Tier,
) -> str | None:
    """Try the loader for the tier's preferred summary; on L1, fall back to L0.

    Returns ``None`` when no suitable summary exists OR on loader exception.
    """
    try:
        if tier == "L0":
            return summary_loader.get_l0(path) or None
        if tier == "L1":
            return (summary_loader.get_l1(path) or summary_loader.get_l0(path)) or None
    except Exception as exc:
        logger.debug("_get_content_for_tier: summary lookup failed — %s", exc)
    return None


def _get_content_for_tier(
    result: FusedResult,
    tier: Tier,
    summary_loader: SummaryLoader | None,
) -> str:
    """
    Return the content string for ``tier``.

    Phase 2+ (loader present): ``L0`` queries the abstract; ``L1`` queries
    the overview and falls back to the abstract. ``L2`` always returns the
    snippet. Loader exceptions surface as the snippet fallback.

    Phase 1 (no loader) and any miss: returns the frontmatter-stripped
    snippet, or "" when the snippet is empty.
    """
    if summary_loader is not None:
        summary = _lookup_tier_summary(summary_loader, result.path, tier)
        if summary:
            return summary

    # Strip YAML frontmatter — raw frontmatter wastes context budget and is
    # noise for agents consuming search results.
    return strip_frontmatter(result.snippet) if result.snippet else ""


# ---------------------------------------------------------------------------
# Phase 2 production loader — pragma'd until summary generation is enabled.
# Tests inject FakeSummaryLoader from tests/fakes.py through ``apply_budget``'s
# ``summary_loader=`` kwarg, exercising every Phase-2 branch through the public
# surface. The default loader is constructed only by callers that opt in to
# Phase 2 once it ships.
# ---------------------------------------------------------------------------


class _DefaultSummaryLoader:  # pragma: no cover — Phase-2 prod loader; tests inject FakeSummaryLoader
    """Production ``SummaryLoader`` — opens the summaries DB lazily on first use."""

    def __init__(self) -> None:
        self._db: sqlite3.Connection | None = None
        self._db_attempted = False

    def _ensure_db(self) -> sqlite3.Connection | None:
        if not self._db_attempted:
            self._db_attempted = True
            self._db = _open_summaries_db()
        return self._db

    def get_l0(self, path: str) -> str | None:
        db = self._ensure_db()
        if db is None:
            return None
        from kairix.knowledge.summaries.loader import get_l0

        return get_l0(path, db)

    def get_l1(self, path: str) -> str | None:
        db = self._ensure_db()
        if db is None:
            return None
        from kairix.knowledge.summaries.loader import get_l1

        return get_l1(path, db)


_summaries_warned = False


def _open_summaries_db() -> sqlite3.Connection | None:  # pragma: no cover — real SQLite; tests use FakeSummaryLoader
    """Open the summaries DB at the configured path, or return None."""
    global _summaries_warned
    from kairix.paths import summaries_db_path

    db_path = summaries_db_path()
    if not db_path.exists():
        if not _summaries_warned:
            logger.info("budget: summaries DB not found — run 'kairix summarise --all' to generate L0 summaries")
            _summaries_warned = True
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        if not _summaries_warned:
            count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            if count < 100:
                logger.info(
                    "budget: only %d summaries in DB — run 'kairix summarise --all' for better token budgeting",
                    count,
                )
            _summaries_warned = True
        return conn
    except Exception:
        return None
