"""F68 failure-mode contract for the ``Stage`` Protocol (ADR-026).

A ``Stage`` only does the work and classifies its own exceptions; the
``StageRunner`` variants own emit + dead-letter + transaction
semantics. The two Stage failure surfaces:

* ``process`` raising → the runner records the stage's CLASSIFIED code
  on the ``pipeline_item_status`` timeline (not a generic error).
* ``classify_exception`` itself raising (an unmapped exception class)
  → the classifier's error propagates, and the timeline still records
  the fail-safe ``PIPELINE_STAGE_NO_EMIT`` row so the failed pass is not
  blank — with no dead-letter written on a half-classified failure.

F43 parity: ONE body per method runs over every production runner
variant that drives a Stage, through the canonical ``FakeStage``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator

import pytest

from kairix.core.connectors.dead_letter import DeadLetterStore
from kairix.core.db.schema import create_schema
from kairix.core.observability.stage import (
    BatchTransactionStageRunner,
    IsolatedStageRunner,
    Stage,
    StageOutcome,
)
from kairix.core.observability.stage_contexts import FetchContext, StageContext
from kairix.core.observability.status_codes import StatusCode
from tests.fakes import FakeStage

pytestmark = pytest.mark.contract

RunnerFactory = Callable[[Stage, sqlite3.Connection, DeadLetterStore], Callable[[StageContext], StageOutcome]]

_ABSORBING_RUNNERS: list[tuple[str, RunnerFactory]] = [
    ("isolated", lambda stage, db, _dl: IsolatedStageRunner(stage, db=db).run),
    ("per_item", lambda stage, db, dl: BatchTransactionStageRunner(stage, db=db, dead_letter=dl).run_per_item),
]

_ALL_RUNNERS: list[tuple[str, RunnerFactory]] = [
    *_ABSORBING_RUNNERS,
    (
        "batch_critical",
        lambda stage, db, dl: BatchTransactionStageRunner(stage, db=db, dead_letter=dl).run_batch_critical,
    ),
]

_CTX = FetchContext(source_name="src-alpha", item_id="item-001")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    yield conn
    conn.close()


def _timeline(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return db.execute(
        "SELECT status_code, severity, detail_json FROM pipeline_item_status WHERE source_name=? AND item_id=?",
        (_CTX.source_name, _CTX.item_id),
    ).fetchall()


def _dead_letter_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM connector_deadletter").fetchone()[0])


@pytest.mark.parametrize("name,factory", _ABSORBING_RUNNERS)
def test_process_raises_classified_code_lands_on_timeline(
    name: str, factory: RunnerFactory, db: sqlite3.Connection
) -> None:
    """``raises``: a stage whose ``process`` raises is recorded under the
    stage's OWN classified code (``FETCH_TIMEOUT``), at error severity,
    with the exception message in the detail — and the runner returns
    that code instead of raising.

    Sabotage proof (executed): in ``IsolatedStageRunner.run`` replace
    ``code = self._stage.classify_exception(exc)`` with
    ``code = StatusCode.PIPELINE_STAGE_NO_EMIT`` → the ``isolated`` case
    fails (timeline row reads ``PIPELINE_STAGE_NO_EMIT``). Restored.
    """
    stage = FakeStage(process_raises=TimeoutError("upstream read timed out"), classify_code=StatusCode.FETCH_TIMEOUT)
    outcome = factory(stage, db, DeadLetterStore(db))(_CTX)
    assert outcome.code == StatusCode.FETCH_TIMEOUT, name
    rows = _timeline(db)
    assert [(code, severity) for code, severity, _ in rows] == [("FETCH_TIMEOUT", "error")]
    assert "upstream read timed out" in json.loads(rows[0][2])["exception_message"]


@pytest.mark.parametrize("name,factory", _ALL_RUNNERS)
def test_classify_exception_raises_propagates_and_timeline_records_no_emit(
    name: str, factory: RunnerFactory, db: sqlite3.Connection
) -> None:
    """``raises``: a classifier that itself blows up propagates its error
    out of every runner variant; the timeline still carries one
    ``PIPELINE_STAGE_NO_EMIT`` row naming the classifier's exception, and
    no dead-letter is written for the half-classified failure.

    Sabotage proof (executed): in ``emit_for`` skip the
    ``PIPELINE_STAGE_NO_EMIT`` write in the exception branch → every
    case fails (the failed pass leaves a blank timeline). Restored.
    """
    stage = FakeStage(
        process_raises=RuntimeError("extract crashed"),
        classify_raises=KeyError("no status code mapped for RuntimeError"),
    )
    with pytest.raises(KeyError, match="no status code mapped"):
        factory(stage, db, DeadLetterStore(db))(_CTX)
    rows = _timeline(db)
    assert [(code, severity) for code, severity, _ in rows] == [("PIPELINE_STAGE_NO_EMIT", "error")], name
    assert json.loads(rows[0][2])["exception_class"] == "KeyError"
    assert _dead_letter_count(db) == 0
