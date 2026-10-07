"""Unit tests for the single-runner behaviours of
:class:`~kairix.core.observability.stage.BatchTransactionStageRunner`.

Moved from ``tests/contracts/test_stage_protocol.py`` (F43 paydown): these
pin ONE runner method's own failure semantics, not a contract shared across
runners — the shared contracts stay parametrized over every runner entry
point in the contract file.

* ``run_per_item`` — catches, records to dead_letter, returns the outcome
  with ``dead_lettered=True``; raises eagerly when no dead_letter is wired.
* ``run_batch_critical`` — emits then re-raises so the caller can roll back
  the per-batch transaction.
"""

from __future__ import annotations

import sqlite3

import pytest

from kairix.core.connectors.dead_letter import DeadLetterStore
from kairix.core.db.schema import create_schema
from kairix.core.observability.stage import BatchTransactionStageRunner
from kairix.core.observability.stage_contexts import FetchContext
from kairix.core.observability.status_codes import StatusCode
from tests.fakes import FakeStage

pytestmark = pytest.mark.unit


def _raising_stage(name: str = "fetch") -> FakeStage:
    return FakeStage(
        name=name, process_raises=RuntimeError("synthetic-failure"), classify_code=StatusCode.FETCH_TIMEOUT
    )


def test_batch_runner_per_item_records_dead_letter_on_exception() -> None:
    """On exception, run_per_item records to dead_letter, returns outcome,
    and does NOT raise.

    Sabotage proof: comment out the ``self._dead_letter.record(...)``
    call in ``BatchTransactionStageRunner.run_per_item``; the dead_letter
    row count stays zero and the assertion fails.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    dead_letter = DeadLetterStore(db)
    runner = BatchTransactionStageRunner(_raising_stage(), db=db, dead_letter=dead_letter)
    ctx = FetchContext(source_name="src", item_id="item-001")
    outcome = runner.run_per_item(ctx)
    # No raise — outcome carries the classified code.
    assert outcome.code == StatusCode.FETCH_TIMEOUT
    assert outcome.detail["dead_lettered"] is True
    rows = db.execute(
        "SELECT item_id, last_error FROM connector_deadletter WHERE source_name=?",
        ("src",),
    ).fetchall()
    assert len(rows) == 1, f"expected one dead_letter row; got {rows!r}"
    assert rows[0][0] == "item-001"
    assert "fetch" in rows[0][1].lower()
    db.close()


def test_batch_runner_per_item_without_dead_letter_raises_value_error() -> None:
    """run_per_item without dead_letter wired raises eagerly — fails
    loud at the call site rather than silently dropping records.

    Sabotage proof: remove the ``if self._dead_letter is None: raise``
    guard; the test fails because no ValueError is raised and the
    sentinel-flag detection downstream gets bypassed.
    """
    runner = BatchTransactionStageRunner(_raising_stage(), db=None, dead_letter=None)
    ctx = FetchContext(source_name="src", item_id="item-001")
    with pytest.raises(ValueError, match="dead_letter"):
        runner.run_per_item(ctx)


def test_batch_runner_critical_propagates_exception_after_emit() -> None:
    """On exception, run_batch_critical emits the classified code AND
    re-raises so the caller can roll back.

    Sabotage proof: change the ``raise`` in
    ``BatchTransactionStageRunner.run_batch_critical``'s except branch
    to ``return``; the test fails because ``pytest.raises`` sees no
    exception, and the batch-rollback contract is broken.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    runner = BatchTransactionStageRunner(_raising_stage(name="silver"), db=db)
    ctx = FetchContext(source_name="src", item_id="item-001")
    with pytest.raises(RuntimeError, match="synthetic-failure"):
        runner.run_batch_critical(ctx)
    # Emit DID happen before the re-raise — timeline row exists.
    rows = db.execute(
        "SELECT status_code, severity FROM pipeline_item_status WHERE source_name=? AND stage=?",
        ("src", "silver"),
    ).fetchall()
    assert len(rows) == 1, f"expected one timeline row; got {rows!r}"
    assert rows[0][0] == "FETCH_TIMEOUT"  # the raising stage classifies to FETCH_TIMEOUT
    assert rows[0][1] == "error"
    db.close()
