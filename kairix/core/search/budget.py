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
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from kairix.core.search.rrf import FusedResult
from kairix.text import APPROX_CHARS_PER_TOKEN, strip_frontmatter
from kairix.text import estimate_tokens as _estimate_tokens_word


@runtime_checkable
class SummaryLoader(Protocol):
    """Loader surface for L0 / L1 document summaries.

    Production: ``_DefaultSummaryLoader`` lazily imports
    ``kairix.knowledge.summaries.loader`` and delegates. Tests pass a
    ``FakeSummaryLoader`` from ``tests/fakes.py`` instead of patching
    ``sys.modules`` to substitute the loader module.
    """

    def get_l0(self, path: str, db: sqlite3.Connection) -> str | None: ...

    def get_l1(self, path: str, db: sqlite3.Connection) -> str | None: ...


class _DefaultSummaryLoader:
    """Production ``SummaryLoader`` — delegates to the real loader module.

    Both methods are ``# pragma: no cover``: they need the real loader module
    and a populated summaries DB. Tests inject ``FakeSummaryLoader`` instead;
    integration coverage of the real loader is deferred to Phase 2.
    """

    def get_l0(self, path: str, db: sqlite3.Connection) -> str | None:  # pragma: no cover
        from kairix.knowledge.summaries.loader import get_l0

        return get_l0(path, db)

    def get_l1(self, path: str, db: sqlite3.Connection) -> str | None:  # pragma: no cover
        from kairix.knowledge.summaries.loader import get_l1

        return get_l1(path, db)


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
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate token count using the canonical word-based estimator.

    Returns 0 for an empty / falsy text so callers don't need to guard.
    """
    if not text:
        return 0
    return _estimate_tokens_word(text)


# ---------------------------------------------------------------------------
# Budget enforcer
# ---------------------------------------------------------------------------


def _get_summaries_db_path() -> Path:
    """Return the summaries DB path — delegates to kairix.paths."""
    from kairix.paths import summaries_db_path

    return summaries_db_path()


_summaries_warned = False


def _open_summaries_db(db_path: Path | None = None) -> sqlite3.Connection | None:
    """Open the summaries DB if it exists, else return None.

    Logs a warning once if the DB is missing or has fewer than 100 entries.

    Args:
        db_path: Explicit summaries-DB path. When ``None``, resolves via
                 ``_get_summaries_db_path()``. Tests pass an explicit path to
                 control whether the DB exists / is openable.
    """
    global _summaries_warned
    if db_path is None:
        db_path = _get_summaries_db_path()
    if not db_path.exists():
        if not _summaries_warned:
            logger.info("budget: summaries DB not found — run 'kairix summarise --all' to generate L0 summaries")
            _summaries_warned = True
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        # pragma below covers a Phase-2-only warning: the summaries DB never
        # exists in Phase 1, so the COUNT(*) probe never fires. Tests cover
        # the path-missing and not-a-database branches via explicit db_path
        # injection; the count-warning will gain real coverage when summary
        # generation is enabled.
        if not _summaries_warned:  # pragma: no cover
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


def apply_budget(
    results: list[FusedResult],
    budget: int = DEFAULT_BUDGET,
    l1_threshold: float = L1_SCORE_THRESHOLD,
    l2_threshold: float = L2_SCORE_THRESHOLD,
) -> list[BudgetedResult]:
    """
    Apply token budget to fused results, assigning each a tier and truncating at cap.

    Phase 1 behaviour: all results are L2 (full snippet). Tier widening (L0→L1→L2)
    requires Phase 2 summary generation.

    Args:
        results:        FusedResult list from rrf() / entity_boost(), score-ordered.
        budget:         Hard token cap. Default DEFAULT_BUDGET.
        l1_threshold:   Score threshold for L1 promotion (Phase 2+).
        l2_threshold:   Score threshold for L2 promotion (Phase 2+).

    Returns:
        List of BudgetedResult, truncated when budget exhausted.
        Empty list if budget is 0 or no results.
        Never raises.
    """
    if not results or budget <= 0:
        return []

    try:
        # Phase 2 tier logic is gated behind summaries DB — skip the open
        # entirely until summaries generation is enabled.  The DB never
        # exists in Phase 1, so _open_summaries_db() always returns None.
        summaries_db = _open_summaries_db()
        budgeted = _apply_budget_impl(results, budget, l1_threshold, l2_threshold, summaries_db)
        # ``_open_summaries_db()`` returns None until Phase 2 summary
        # generation is enabled, so this close() never fires today.
        if summaries_db is not None:  # pragma: no cover
            summaries_db.close()
        return budgeted
    except Exception as e:
        logger.warning("apply_budget: unexpected error — %s", e)
        return []


def _apply_budget_impl(
    results: list[FusedResult],
    budget: int,
    l1_threshold: float,
    l2_threshold: float,
    summaries_db: sqlite3.Connection | None = None,
) -> list[BudgetedResult]:
    """Internal budget implementation."""
    budgeted: list[BudgetedResult] = []
    remaining = budget

    for result in results:
        if remaining <= 0:
            break

        # Phase 2+: use L0/L1 summaries when available and budget is tight
        tier = _select_tier(result, remaining, l1_threshold, l2_threshold, summaries_db)
        content = _get_content_for_tier(result, tier, remaining, summaries_db)
        tokens = _estimate_tokens(content)

        # Hard cap: if even this result exceeds remaining, truncate content
        if tokens > remaining:
            # Truncate to fit
            max_chars = remaining * APPROX_CHARS_PER_TOKEN
            content = content[:max_chars]
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
    summaries_db: sqlite3.Connection | None = None,
) -> Tier:
    """
    Select the appropriate tier for a result given the current budget.

    Phase 1 (no summaries_db): always return L2 (full snippet).
    Phase 2+: L0 by default; promote to L1 or L2 based on score and budget.
    When budget is tight (< L2_BUDGET_MIN), prefer L0/L1 summaries to save tokens.
    """
    # Phase 2 tier logic is not yet active — summaries DB is never populated.
    # Short-circuit to avoid opening a DB that always returns None.
    if summaries_db is None:
        return "L2"

    score = result.boosted_score

    if remaining_budget >= L2_BUDGET_MIN and score >= l2_threshold:
        return "L2"
    elif remaining_budget >= L1_BUDGET_MIN and score >= l1_threshold:
        return "L1"
    else:
        return "L0"


def _get_content_for_tier(
    result: FusedResult,
    tier: Tier,
    remaining_budget: int = DEFAULT_BUDGET,
    summaries_db: sqlite3.Connection | None = None,
    loader: SummaryLoader | None = None,
) -> str:
    """
    Return content for the given tier.

    Phase 2+: when summaries_db is available and budget is tight, prefer
    L0 (abstract) or L1 (overview) over the full snippet to conserve tokens.
    Falls back to snippet if no summary is available.

    Args:
        loader: Summary loader. When ``None``, lazily constructs the
                production ``_DefaultSummaryLoader``. Tests inject a
                ``FakeSummaryLoader``.
    """
    if summaries_db is not None:
        if loader is None:  # pragma: no cover — production-only lazy default; tests inject FakeSummaryLoader
            loader = _DefaultSummaryLoader()
        try:
            if tier == "L0":
                l0 = loader.get_l0(result.path, summaries_db)
                if l0:
                    return l0
            elif tier == "L1":
                l1 = loader.get_l1(result.path, summaries_db)
                if l1:
                    return l1
                # Fall back to L0 if L1 not available
                l0 = loader.get_l0(result.path, summaries_db)
                if l0:
                    return l0
        except Exception as exc:
            logger.debug("_get_content_for_tier: summary lookup failed — %s", exc)

    # Default: return snippet (Phase 1 behaviour and fallback)
    # Strip YAML frontmatter — raw frontmatter wastes context budget and is
    # noise for agents consuming search results.
    return strip_frontmatter(result.snippet) if result.snippet else ""
