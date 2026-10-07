"""Unit coverage for ``usearch_parity_check`` — the usearch stage's testable core.

F86 split the maintenance scheduler's ``_default_usearch_rebuilder`` seam
into a thin platform adapter (resolve db path + open the on-disk index)
and this pure helper, so the parity logic is executed by the suite even
though the adapter's index-present branch needs a live on-disk usearch
index. The helper takes the db path and any ``len()``-able index.

Sabotage-proof (executed): changed the helper's ``live_count`` to
``len(rows) + 1`` — ``test_parity_check_logs_live_and_indexed_counts``
failed (``live=4`` logged). Restored.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kairix.core.maintenance import usearch_parity_check

pytestmark = pytest.mark.unit


class _UnsizedIndex:
    """An index whose size can't be read (closed / corrupt handle)."""

    def __len__(self) -> int:
        raise RuntimeError("index handle closed")


def _db_with_vectors(path: Path, n: int) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER)")
        db.executemany("INSERT INTO content_vectors VALUES (?, ?)", [(f"h{i}", 0) for i in range(n)])
        db.commit()
    return path


def test_parity_check_logs_live_and_indexed_counts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db = _db_with_vectors(tmp_path / "index.sqlite", 3)

    with caplog.at_level(logging.INFO, logger="kairix.maintenance"):
        assert usearch_parity_check(db, index=[0, 1]) is True

    assert "usearch parity check — live=3 indexed=2" in caplog.text


def test_parity_check_reports_unsized_index_as_minus_one(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db = _db_with_vectors(tmp_path / "index.sqlite", 1)

    with caplog.at_level(logging.INFO, logger="kairix.maintenance"):
        assert usearch_parity_check(db, index=_UnsizedIndex()) is True

    assert "live=1 indexed=-1" in caplog.text


def test_parity_check_on_fresh_db_is_a_no_op_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """No ``content_vectors`` table yet — nothing to rebuild against."""
    with caplog.at_level(logging.INFO, logger="kairix.maintenance"):
        assert usearch_parity_check(tmp_path / "fresh.sqlite", index=[]) is True

    assert "parity check" not in caplog.text
