"""
Tier router for the kairix search pipeline.

load_tiered_content() selects the appropriate content representation
(L0 abstract, L1 overview, or full file text) based on a token budget
and what summaries are available in the summaries DB.

Token budget thresholds:
  <= 150 tokens  → L0 preferred (1-2 sentence abstract)
  <= 600 tokens  → L1 preferred (structured overview)
  >  600 tokens  → full file content (truncated to budget)

Falls back gracefully when summaries are absent.
"""

import logging
import sqlite3
from pathlib import Path

from kairix.text import APPROX_CHARS_PER_TOKEN

logger = logging.getLogger(__name__)


def _truncate_to_tokens(text: str, budget_tokens: int) -> str:
    max_chars = budget_tokens * APPROX_CHARS_PER_TOKEN
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Low-level accessors (used by budget.py integration)
# ---------------------------------------------------------------------------


def get_l0(path: str, db: sqlite3.Connection) -> str | None:
    """Return the L0 abstract if available, else None."""
    row = db.execute(
        "SELECT l0 FROM summaries WHERE path = ? AND l0 IS NOT NULL AND l0 != ''",
        (path,),
    ).fetchone()
    return row[0] if row else None


def get_l1(path: str, db: sqlite3.Connection) -> str | None:
    """Return the L1 overview if available, else None."""
    row = db.execute(
        "SELECT l1 FROM summaries WHERE path = ? AND l1 IS NOT NULL AND l1 != ''",
        (path,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tier router
# ---------------------------------------------------------------------------


def load_tiered_content(
    path: str,
    db: sqlite3.Connection,
    budget_tokens: int = 500,
    _tier_preference: str = "l0",  # "l0" | "l1" | "full" — reserved for future explicit tier pinning
) -> tuple[str, str]:
    """
    Return (content, tier_used) based on budget and available summaries.

    Logic:
      budget <= 150: L0 if available, else first 150 tokens of file
      budget <= 600: L1 if available, else L0 if available, else first 600 tokens
      budget > 600:  full file content truncated to budget

    tier_used: "l0" | "l1" | "full" | "truncated"
    """
    if budget_tokens <= 150:
        l0 = get_l0(path, db)
        if l0:
            return l0, "l0"
        raw = _read_file(path)
        return _truncate_to_tokens(raw, 150), "truncated"

    if budget_tokens <= 600:
        l1 = get_l1(path, db)
        if l1:
            return l1, "l1"
        l0 = get_l0(path, db)
        if l0:
            return l0, "l0"
        raw = _read_file(path)
        return _truncate_to_tokens(raw, budget_tokens), "truncated"

    # budget > 600: full content truncated to budget
    raw = _read_file(path)
    if len(raw) <= budget_tokens * APPROX_CHARS_PER_TOKEN:
        return raw, "full"
    return _truncate_to_tokens(raw, budget_tokens), "truncated"


def _read_file(path: str) -> str:
    """Read file content, returning empty string on failure."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("load_tiered_content: could not read %s — %s", path, exc)
        return ""
