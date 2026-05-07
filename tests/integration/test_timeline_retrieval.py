"""
Integration tests: temporal index retrieval against synthetic agent memory logs.

Memory fixture dates are rolled forward to today by the integration conftest
(see ``_roll_memory_dates_to_today``), so the date range below is computed
relative to today rather than pinned to the original fixture stems.
"""

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
def test_timeline_finds_memory_logs(real_db, real_document_root):
    """Timeline retrieval finds agent memory logs within date range."""
    from kairix.core.temporal.index import query_temporal_chunks

    today = date.today()
    results = query_temporal_chunks(
        "session update",
        start=today - timedelta(days=2),
        end=today,
    )
    assert len(results) > 0


@pytest.mark.integration
def test_timeline_returns_empty_for_future_dates(real_db, real_document_root):
    """Timeline returns empty for dates with no memory logs."""
    from kairix.core.temporal.index import query_temporal_chunks

    results = query_temporal_chunks(
        "anything",
        start=date(2099, 1, 1),
        end=date(2099, 12, 31),
    )
    assert len(results) == 0
