"""
kairix.core.temporal.rewriter — Query rewriter for TEMPORAL intent.

Extracts date windows from natural-language temporal queries and rewrites
them to include explicit date context for BM25/vector retrieval.

Functions:
  extract_time_window(query, reference_date) → (start, end) | (None, None)
  rewrite_temporal_query(query, reference_date) → str

Never raises — returns (None, None) / unchanged query on any failure.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Month name table
# ---------------------------------------------------------------------------

_MONTH_NAMES: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

# ---------------------------------------------------------------------------
# Compiled patterns — in priority order
# ---------------------------------------------------------------------------

# Explicit ISO date: 2026-03-22
_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")

# "on YYYY-MM-DD" or "on 22 March 2026"
_ON_DATE_RE = re.compile(
    r"\bon\s+(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b",
    re.IGNORECASE,
)

# "in March 2026" or "in March" (without year)
_IN_MONTH_YEAR_RE = re.compile(
    r"\bin\s+(?P<month>" + "|".join(_MONTH_NAMES.keys()) + r")(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)

# "last week" / "last month" / "last year" / "last 7 days" / "last 30 days" etc.
_LAST_N_DAYS_RE = re.compile(
    r"\blast\s+(?P<n>\d+)\s*(?:days?)?\b",
    re.IGNORECASE,
)
_LAST_PERIOD_RE = re.compile(
    r"\blast\s+(?P<unit>week|month|year|quarter)\b",
    re.IGNORECASE,
)

# "this week" / "this month"
_THIS_PERIOD_RE = re.compile(
    r"\bthis\s+(?P<unit>week|month)\b",
    re.IGNORECASE,
)

# "yesterday"
_YESTERDAY_RE = re.compile(r"\byesterday\b", re.IGNORECASE)

# "today"
_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)

# "recently" / "lately"
_RECENTLY_RE = re.compile(r"\b(?:recently|lately)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Canonical temporal query patterns (shared with kairix.core.search.rrf)
# ---------------------------------------------------------------------------

# Matches YYYY-MM-DD (e.g. "2026-03-22") in a query — capturing group 1
QUERY_ISO_DATE_RE: re.Pattern[str] = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Matches YYYY-MM (e.g. "2026-03") — also captures the YYYY-MM prefix of ISO dates
QUERY_YEAR_MONTH_RE: re.Pattern[str] = re.compile(r"\b(\d{4}-\d{2})(?:-\d{2})?\b")

# Relative temporal terms that trigger a recency boost instead of a date-match boost
RELATIVE_TEMPORAL_RE: re.Pattern[str] = re.compile(
    r"\b(recent(?:ly)?|last\s+(?:week|month)|yesterday|today|this\s+(?:week|month))\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_relative_temporal(query: str) -> bool:
    """
    Return True if *query* contains a RELATIVE temporal expression.

    Relative expressions refer to time windows anchored on "today" —
    e.g. "last week", "recently", "yesterday".  For these, date-filtered
    retrieval (TMP-2) is appropriate: we expect documents *written* in that
    window to be the relevant ones.

    Absolute expressions ("March 2026", "2026-03-09") refer to a named
    period and are better served by query rewriting + BM25/vector matching
    the date tokens in document content.  Applying a date filter to absolute
    expressions would filter out gold documents that merely *mention* the
    date rather than being *written* on that date.

    Returns True for: last N days, last week/month/year/quarter,
    this week/month, yesterday, today, recently/lately.
    Returns False for: ISO dates, "in Month YYYY", "on YYYY-MM-DD".
    Never raises.
    """
    try:
        q = query.strip()
        for pattern in (
            _LAST_N_DAYS_RE,
            _LAST_PERIOD_RE,
            _THIS_PERIOD_RE,
            _YESTERDAY_RE,
            _TODAY_RE,
            _RECENTLY_RE,
        ):
            if pattern.search(q):
                return True
        return False
    except Exception:
        return False


_LAST_PERIOD_DAYS_MAP = {"week": 7, "month": 30, "year": 365, "quarter": 90}


def _try_explicit_date(q: str, _today: date) -> tuple[date, date] | None:
    """``on YYYY-MM-DD`` or bare ``YYYY-MM-DD`` → exact single-day window."""
    m = _ON_DATE_RE.search(q) or _ISO_DATE_RE.search(q)
    if not m:
        return None
    d = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
    return d, d


def _try_month_year(q: str, today: date) -> tuple[date, date] | None:
    """``in March 2026`` / ``in March`` → first-to-last-day-of-month window."""
    m = _IN_MONTH_YEAR_RE.search(q)
    if not m:
        return None
    month_num = _MONTH_NAMES[m.group("month").lower()]
    year = int(m.group("year")) if m.group("year") else today.year
    if month_num == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month_num + 1, 1) - timedelta(days=1)
    return date(year, month_num, 1), last_day


def _try_last_n_days(q: str, today: date) -> tuple[date, date] | None:
    """``last N days`` → ``today-N`` through ``today``."""
    m = _LAST_N_DAYS_RE.search(q)
    if not m:
        return None
    return today - timedelta(days=int(m.group("n"))), today


def _try_last_period(q: str, today: date) -> tuple[date, date] | None:
    """``last week|month|year|quarter`` → rolling window backward from ``today``."""
    m = _LAST_PERIOD_RE.search(q)
    if not m:
        return None
    days = _LAST_PERIOD_DAYS_MAP[m.group("unit").lower()]
    return today - timedelta(days=days), today


def _try_this_period(q: str, today: date) -> tuple[date, date] | None:
    """``this week|month`` → start-of-period through ``today``."""
    m = _THIS_PERIOD_RE.search(q)
    if not m:
        return None
    unit = m.group("unit").lower()
    if unit == "week":
        return today - timedelta(days=today.weekday()), today
    if unit == "month":
        return date(today.year, today.month, 1), today
    return None


def _try_yesterday(q: str, today: date) -> tuple[date, date] | None:
    if not _YESTERDAY_RE.search(q):
        return None
    yesterday = today - timedelta(days=1)
    return yesterday, yesterday


def _try_today(q: str, today: date) -> tuple[date, date] | None:
    return (today, today) if _TODAY_RE.search(q) else None


def _try_recently(q: str, today: date) -> tuple[date, date] | None:
    return (today - timedelta(days=14), today) if _RECENTLY_RE.search(q) else None


# Pattern handlers tried in order; first match wins (matches the historic
# pattern priority documented in extract_time_window's docstring).
_WINDOW_PATTERNS: list[Callable[[str, date], tuple[date, date] | None]] = [
    _try_explicit_date,
    _try_month_year,
    _try_last_n_days,
    _try_last_period,
    _try_this_period,
    _try_yesterday,
    _try_today,
    _try_recently,
]


def extract_time_window(
    query: str,
    reference_date: date | None = None,
) -> tuple[date | None, date | None]:
    """Extract a ``(start, end)`` date window from a temporal query string.

    Patterns recognised (first match wins):
      ``on YYYY-MM-DD`` / ``YYYY-MM-DD``  → single-day window
      ``in March 2026`` / ``in March``    → full-month window
      ``last N days`` / ``last week|month|year|quarter`` → rolling-back window
      ``this week|month``                 → start-of-period to today
      ``yesterday`` / ``today``           → single-day window
      ``recently`` / ``lately``           → last 14 days

    Args:
        query:          Natural-language query string.
        reference_date: Date to use as "today". Defaults to date.today().

    Returns:
        ``(start, end)`` tuple, or ``(None, None)`` if no temporal expression found.
        Never raises.
    """
    try:
        today = reference_date or date.today()
        q = query.strip()
        for handler in _WINDOW_PATTERNS:
            result = handler(q, today)
            if result is not None:
                return result
        return None, None
    except Exception:
        return None, None


def rewrite_temporal_query(
    query: str,
    reference_date: date | None = None,
) -> str:
    """
    Rewrite a temporal query to include explicit date context.

    Extracts the time window from the query, then appends the date range
    as explicit tokens so BM25/vector search can match dated documents.

    Example:
      "what was completed last week on kairix"
      → "what was completed last week on kairix 2026-03-16 to 2026-03-22"

    If no temporal expression is found, the query is returned unchanged.

    Args:
        query:          Natural-language query string.
        reference_date: Date to use as "today". Defaults to date.today().

    Returns:
        Rewritten query string (or original if no temporal expression).
        Never raises.
    """
    try:
        start, end = extract_time_window(query, reference_date=reference_date)
        if start is None or end is None:
            return query

        date_tokens: str
        if start == end:
            date_tokens = start.isoformat()
        else:
            date_tokens = f"{start.isoformat()} to {end.isoformat()}"

        return f"{query} {date_tokens}"

    except Exception:
        return query
