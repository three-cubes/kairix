"""
Tests for kairix.core.temporal.rewriter.

Covers:
  - extract_time_window(): all supported temporal patterns
  - rewrite_temporal_query(): appends date range to query
  - is_relative_temporal(): relative-vs-absolute temporal classification
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kairix.core.temporal.rewriter import (
    extract_time_window,
    is_relative_temporal,
    rewrite_temporal_query,
)

# Fixed reference date for deterministic tests
REFERENCE = date(2026, 3, 22)  # A Sunday


@pytest.mark.unit
class TestExtractTimeWindow:
    """Tests for extract_time_window()."""

    # -----------------------------------------------------------------------
    # Relative periods
    # -----------------------------------------------------------------------

    @pytest.mark.unit
    def test_last_week(self) -> None:
        start, end = extract_time_window("what happened last week", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=7)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_last_month(self) -> None:
        start, end = extract_time_window("last month's progress", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=30)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_last_year(self) -> None:
        start, end = extract_time_window("what was done last year", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=365)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_last_quarter(self) -> None:
        start, end = extract_time_window("last quarter results", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=90)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_last_n_days_7(self) -> None:
        start, end = extract_time_window("last 7 days", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=7)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_last_n_days_30(self) -> None:
        start, end = extract_time_window("last 30 days", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=30)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_this_week(self) -> None:
        # REFERENCE is Sunday 2026-03-22; Monday is 2026-03-16
        start, end = extract_time_window("this week's tasks", reference_date=REFERENCE)
        monday = REFERENCE - timedelta(days=REFERENCE.weekday())
        assert start == monday
        assert end == REFERENCE

    @pytest.mark.unit
    def test_this_month(self) -> None:
        start, end = extract_time_window("this month's work", reference_date=REFERENCE)
        assert start == date(2026, 3, 1)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_yesterday(self) -> None:
        start, end = extract_time_window("what happened yesterday", reference_date=REFERENCE)
        yesterday = REFERENCE - timedelta(days=1)
        assert start == yesterday
        assert end == yesterday

    @pytest.mark.unit
    def test_today(self) -> None:
        start, end = extract_time_window("what was done today", reference_date=REFERENCE)
        assert start == REFERENCE
        assert end == REFERENCE

    @pytest.mark.unit
    def test_recently(self) -> None:
        start, end = extract_time_window("recently completed tasks", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=14)
        assert end == REFERENCE

    @pytest.mark.unit
    def test_lately(self) -> None:
        start, end = extract_time_window("what has been done lately", reference_date=REFERENCE)
        assert start == REFERENCE - timedelta(days=14)
        assert end == REFERENCE

    # -----------------------------------------------------------------------
    # Month/year expressions
    # -----------------------------------------------------------------------

    @pytest.mark.unit
    def test_in_march_2026(self) -> None:
        start, end = extract_time_window("what happened in March 2026", reference_date=REFERENCE)
        assert start == date(2026, 3, 1)
        assert end == date(2026, 3, 31)

    @pytest.mark.unit
    def test_in_february_2026(self) -> None:
        start, end = extract_time_window("in February 2026", reference_date=REFERENCE)
        assert start == date(2026, 2, 1)
        assert end == date(2026, 2, 28)  # 2026 is not a leap year

    @pytest.mark.unit
    def test_in_december_2025(self) -> None:
        start, end = extract_time_window("in December 2025", reference_date=REFERENCE)
        assert start == date(2025, 12, 1)
        assert end == date(2025, 12, 31)

    @pytest.mark.unit
    def test_in_month_without_year_uses_reference_year(self) -> None:
        start, end = extract_time_window("in January", reference_date=REFERENCE)
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 31)

    @pytest.mark.unit
    def test_month_abbreviation(self) -> None:
        start, end = extract_time_window("in Mar 2026", reference_date=REFERENCE)
        assert start == date(2026, 3, 1)
        assert end == date(2026, 3, 31)

    # -----------------------------------------------------------------------
    # ISO date expressions
    # -----------------------------------------------------------------------

    @pytest.mark.unit
    def test_on_iso_date(self) -> None:
        start, end = extract_time_window("on 2026-03-22", reference_date=REFERENCE)
        assert start == date(2026, 3, 22)
        assert end == date(2026, 3, 22)

    @pytest.mark.unit
    def test_bare_iso_date(self) -> None:
        start, end = extract_time_window("tasks from 2026-03-22", reference_date=REFERENCE)
        assert start == date(2026, 3, 22)
        assert end == date(2026, 3, 22)

    @pytest.mark.unit
    def test_on_takes_priority_over_bare_iso(self) -> None:
        # "on YYYY-MM-DD" should be handled by _ON_DATE_RE (same result here)
        start, end = extract_time_window("on 2026-03-15", reference_date=REFERENCE)
        assert start == date(2026, 3, 15)
        assert end == date(2026, 3, 15)

    # -----------------------------------------------------------------------
    # No temporal expression
    # -----------------------------------------------------------------------

    @pytest.mark.unit
    def test_no_dates_returns_none_none(self) -> None:
        start, end = extract_time_window("no dates here", reference_date=REFERENCE)
        assert start is None
        assert end is None

    @pytest.mark.unit
    def test_empty_string_returns_none_none(self) -> None:
        start, end = extract_time_window("", reference_date=REFERENCE)
        assert start is None
        assert end is None

    @pytest.mark.unit
    def test_unrelated_query_returns_none_none(self) -> None:
        start, end = extract_time_window("what is the best way to deploy an API", reference_date=REFERENCE)
        assert start is None
        assert end is None

    @pytest.mark.unit
    def test_uses_date_today_when_no_reference(self) -> None:
        # Should not raise — defaults to date.today()
        start, end = extract_time_window("last week")
        today = date.today()
        assert start == today - timedelta(days=7)
        assert end == today

    # -----------------------------------------------------------------------
    # Priority ordering
    # -----------------------------------------------------------------------

    @pytest.mark.unit
    def test_on_date_takes_priority_over_month_name(self) -> None:
        # Query has both "in March 2026" and "on 2026-03-22" — "on" should win
        # because _ON_DATE_RE is checked before _IN_MONTH_YEAR_RE
        start, end = extract_time_window("on 2026-03-22 in March 2026", reference_date=REFERENCE)
        assert start == date(2026, 3, 22)
        assert end == date(2026, 3, 22)


# ---------------------------------------------------------------------------
# TestRewriteTemporalQuery
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRewriteTemporalQuery:
    @pytest.mark.unit
    def test_appends_date_range_for_last_week(self) -> None:
        result = rewrite_temporal_query(
            "what was completed last week on Kairix",
            reference_date=REFERENCE,
        )
        expected_start = (REFERENCE - timedelta(days=7)).isoformat()
        expected_end = REFERENCE.isoformat()
        assert expected_start in result
        assert expected_end in result
        assert "what was completed last week on Kairix" in result

    @pytest.mark.unit
    def test_appends_single_date_for_exact_date(self) -> None:
        result = rewrite_temporal_query("on 2026-03-22", reference_date=REFERENCE)
        assert "2026-03-22" in result
        assert " to " not in result  # single date, no range

    @pytest.mark.unit
    def test_appends_range_for_month(self) -> None:
        result = rewrite_temporal_query("what happened in March 2026", reference_date=REFERENCE)
        assert "2026-03-01" in result
        assert "2026-03-31" in result
        assert " to " in result

    @pytest.mark.unit
    def test_returns_unchanged_when_no_temporal_expression(self) -> None:
        query = "tell me about Bower Bird architecture"
        result = rewrite_temporal_query(query, reference_date=REFERENCE)
        assert result == query

    @pytest.mark.unit
    def test_returns_unchanged_for_empty_string(self) -> None:
        result = rewrite_temporal_query("", reference_date=REFERENCE)
        assert result == ""

    @pytest.mark.unit
    def test_original_query_preserved_in_output(self) -> None:
        query = "Kairix tasks recently"
        result = rewrite_temporal_query(query, reference_date=REFERENCE)
        assert result.startswith(query)

    @pytest.mark.unit
    def test_does_not_raise_on_unusual_input(self) -> None:
        # Should never raise
        result = rewrite_temporal_query("!@#$%^&*()", reference_date=REFERENCE)
        assert isinstance(result, str)

    @pytest.mark.unit
    def test_uses_today_when_no_reference(self) -> None:
        result = rewrite_temporal_query("last week summary")
        today = date.today()
        assert today.isoformat() in result

    @pytest.mark.unit
    def test_non_string_input_returns_unchanged_via_exception_path(self) -> None:
        # rewrite_temporal_query catches any exception and returns the original
        # query unchanged. Passing None hits the .strip() path inside
        # extract_time_window, which itself returns (None, None); the outer
        # rewrite then exits via the no-window branch. To hit the OUTER
        # try/except (lines 294-295), we need an exception AFTER
        # extract_time_window returns — e.g. a non-string whose isoformat()
        # call would never run because we never reach isoformat for None
        # results. Instead, sabotage by passing an object whose __str__ raises
        # when formatted via f-string.
        class _Boom:
            def __str__(self) -> str:
                raise RuntimeError("sabotage str()")

        # extract_time_window swallows the exception and returns (None, None),
        # so rewrite returns the same object back without raising.
        result = rewrite_temporal_query(_Boom())  # type: ignore[arg-type]  # contract: never raises
        # Identity preserved when no temporal window resolved.
        assert isinstance(result, _Boom)


# ---------------------------------------------------------------------------
# TestExtractTimeWindowExceptionPath — defensive try/except (lines 253-254)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractTimeWindowExceptionPath:
    @pytest.mark.unit
    def test_none_input_returns_none_none(self) -> None:
        # None.strip() raises AttributeError; the catch-all returns (None, None).
        start, end = extract_time_window(None)  # type: ignore[arg-type]  # contract: never raises
        assert start is None
        assert end is None

    @pytest.mark.unit
    def test_object_without_strip_returns_none_none(self) -> None:
        # An int has no .strip(); exception path returns (None, None).
        start, end = extract_time_window(42)  # type: ignore[arg-type]  # contract: never raises
        assert start is None
        assert end is None


# ---------------------------------------------------------------------------
# TestRewriteTemporalQueryExceptionPath — defensive try/except (lines 294-295)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRewriteTemporalQueryExceptionPath:
    @pytest.mark.unit
    def test_outer_except_returns_original_when_format_raises(self) -> None:
        # Sabotage proof for the outer try/except (lines 294-295):
        # construct an input where extract_time_window resolves a valid
        # window (so we get past the early return) but the final f-string
        # formatting raises. A custom object whose .strip() returns a
        # string containing "last week" makes the inner extraction succeed;
        # its __str__ then raises when the f-string interpolates it.
        class _Sketchy:
            def strip(self) -> str:
                return "last week"

            def __format__(self, spec: str) -> str:
                raise RuntimeError("sabotage __format__")

        sketchy = _Sketchy()
        result = rewrite_temporal_query(sketchy, reference_date=date(2026, 3, 22))  # type: ignore[arg-type]  # contract: never raises
        # When formatting blows up, the outer except returns the ORIGINAL query.
        assert result is sketchy


# ---------------------------------------------------------------------------
# TestIsRelativeTemporal — relative-vs-absolute classification (lines 137-151)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsRelativeTemporal:
    """Tests for is_relative_temporal()."""

    @pytest.mark.unit
    def test_last_week_is_relative(self) -> None:
        assert is_relative_temporal("what happened last week") is True

    @pytest.mark.unit
    def test_last_month_is_relative(self) -> None:
        assert is_relative_temporal("last month progress") is True

    @pytest.mark.unit
    def test_last_n_days_is_relative(self) -> None:
        assert is_relative_temporal("tasks in the last 7 days") is True

    @pytest.mark.unit
    def test_this_week_is_relative(self) -> None:
        assert is_relative_temporal("this week's standups") is True

    @pytest.mark.unit
    def test_this_month_is_relative(self) -> None:
        assert is_relative_temporal("this month review") is True

    @pytest.mark.unit
    def test_yesterday_is_relative(self) -> None:
        assert is_relative_temporal("yesterday's meeting notes") is True

    @pytest.mark.unit
    def test_today_is_relative(self) -> None:
        assert is_relative_temporal("today's standup") is True

    @pytest.mark.unit
    def test_recently_is_relative(self) -> None:
        assert is_relative_temporal("recently completed tickets") is True

    @pytest.mark.unit
    def test_lately_is_relative(self) -> None:
        assert is_relative_temporal("what's been done lately") is True

    @pytest.mark.unit
    def test_iso_date_is_not_relative(self) -> None:
        # Absolute date — date-filter would suppress mentions, so this is NOT
        # treated as relative.
        assert is_relative_temporal("changes on 2026-03-22") is False

    @pytest.mark.unit
    def test_in_month_year_is_not_relative(self) -> None:
        # "in March 2026" is an absolute period, not relative-to-today.
        assert is_relative_temporal("what shipped in March 2026") is False

    @pytest.mark.unit
    def test_empty_query_is_not_relative(self) -> None:
        assert is_relative_temporal("") is False

    @pytest.mark.unit
    def test_unrelated_query_is_not_relative(self) -> None:
        assert is_relative_temporal("how do I deploy the api") is False

    @pytest.mark.unit
    def test_non_string_input_returns_false(self) -> None:
        # .strip() on None raises AttributeError; the catch-all returns False.
        assert is_relative_temporal(None) is False  # type: ignore[arg-type]  # contract: never raises

    @pytest.mark.unit
    def test_int_input_returns_false(self) -> None:
        # An int has no .strip(); exception path returns False.
        assert is_relative_temporal(123) is False  # type: ignore[arg-type]  # contract: never raises
