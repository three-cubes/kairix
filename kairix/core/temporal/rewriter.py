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


def extract_time_window(
    query: str,
    reference_date: date | None = None,
) -> tuple[date | None, date | None]:
    """
    Extract a (start, end) date window from a temporal query string.

    Patterns recognised (first match wins):
      "on YYYY-MM-DD"        → (YYYY-MM-DD, YYYY-MM-DD)
      "YYYY-MM-DD"           → (YYYY-MM-DD, YYYY-MM-DD)
      "in March 2026"        → (2026-03-01, 2026-03-31)
      "in March"             → (ref_year-03-01, ref_year-03-31)
      "last N days"          → (today-N, today)
      "last week"            → (today-7d, today)
      "last month"           → (today-30d, today)
      "last year"            → (today-365d, today)
      "last quarter"         → (today-90d, today)
      "this week"            → (Monday, today)
      "this month"           → (1st of current month, today)
      "yesterday"            → (today-1d, today-1d)
      "today"                → (today, today)
      "recently" / "lately"  → (today-14d, today)

    Args:
        query:          Natural-language query string.
        reference_date: Date to use as "today". Defaults to date.today().

    Returns:
        (start, end) tuple, or (None, None) if no temporal expression found.
        Never raises.
    """
    try:
        today = reference_date or date.today()
        q = query.strip()

        # 1. "on YYYY-MM-DD" — explicit single day with "on" prefix
        m = _ON_DATE_RE.search(q)
        if m:
            d = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
            return d, d

        # 2. "in Month YYYY" or "in Month"
        m = _IN_MONTH_YEAR_RE.search(q)
        if m:
            month_num = _MONTH_NAMES[m.group("month").lower()]
            year = int(m.group("year")) if m.group("year") else today.year
            # Compute last day of month
            if month_num == 12:
                last_day = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = date(year, month_num + 1, 1) - timedelta(days=1)
            start = date(year, month_num, 1)
            return start, last_day

        # 3. "last N days"
        m = _LAST_N_DAYS_RE.search(q)
        if m:
            n = int(m.group("n"))
            return today - timedelta(days=n), today

        # 4. "last week/month/year/quarter"
        m = _LAST_PERIOD_RE.search(q)
        if m:
            unit = m.group("unit").lower()
            days_map = {"week": 7, "month": 30, "year": 365, "quarter": 90}
            n = days_map[unit]
            return today - timedelta(days=n), today

        # 5. "this week"
        m = _THIS_PERIOD_RE.search(q)
        if m:
            unit = m.group("unit").lower()
            if unit == "week":
                monday = today - timedelta(days=today.weekday())
                return monday, today
            elif unit == "month":
                return date(today.year, today.month, 1), today

        # 6. "yesterday"
        if _YESTERDAY_RE.search(q):
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday

        # 7. "today"
        if _TODAY_RE.search(q):
            return today, today

        # 8. "recently" / "lately"
        if _RECENTLY_RE.search(q):
            return today - timedelta(days=14), today

        # 9. Bare ISO date anywhere in query
        m = _ISO_DATE_RE.search(q)
        if m:
            d = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
            return d, d

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
