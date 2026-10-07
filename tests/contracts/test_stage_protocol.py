"""Contract + unit tests for the Stage Protocol + StageRunner variants (ADR-026 Track A main).

Three contracts to prove:

1. **Stage Protocol shape** — a class with ``name``, ``process``, and
   ``classify_exception`` satisfies :class:`Stage` via the
   ``isinstance`` runtime check.
2. **IsolatedStageRunner** — absorbs every exception into an outcome,
   never raises, threads the classified code through.
3. **BatchTransactionStageRunner.run_per_item** — catches, records to
   dead_letter, returns the outcome with ``dead_lettered=True``.
4. **BatchTransactionStageRunner.run_batch_critical** — emits then
   re-raises so the caller can roll back the per-batch transaction.

The single-runner behaviours (``run_per_item`` dead-lettering + its
``ValueError`` guard, ``run_batch_critical`` re-raise) live in
``tests/unit/test_stage_runner_units.py``.

Contracts the runner entry points share (OK pass-through, timeline
emit, WARN routing, exception absorb) run as ONE body over every
production entry point that carries them (F43 parity) via the
``runner`` / ``absorbing_runner`` fixtures.

Each test has a "Sabotage proof:" comment describing the mutation
that proves it has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

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
from kairix.core.observability.status_codes import Severity, StatusCode
from tests.fakes import FakeStage

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Test stages — minimal Stage Protocol implementations.
# ---------------------------------------------------------------------------


@dataclass
class _OkStage:
    """Stage that always returns FETCH_OK."""

    name: str = "fetch"

    def process(self, ctx: StageContext) -> StageOutcome:
        return StageOutcome(code=StatusCode.FETCH_OK, output=b"body-bytes")

    def classify_exception(self, exc: BaseException) -> StatusCode:
        return StatusCode.PIPELINE_STAGE_NO_EMIT


@dataclass
class _RaisingStage:
    """Stage that always raises; classifies to FETCH_TIMEOUT."""

    name: str = "fetch"
    exc_class: type[BaseException] = RuntimeError

    def process(self, ctx: StageContext) -> StageOutcome:
        raise self.exc_class("synthetic-failure")

    def classify_exception(self, exc: BaseException) -> StatusCode:
        return StatusCode.FETCH_TIMEOUT


@dataclass
class _WarnStage:
    """Stage that returns a WARN-severity code."""

    name: str = "fetch"

    def process(self, ctx: StageContext) -> StageOutcome:
        return StageOutcome(code=StatusCode.FETCH_THROTTLED, output=None, detail={"retry_after": 30})

    def classify_exception(self, exc: BaseException) -> StatusCode:
        return StatusCode.PIPELINE_STAGE_NO_EMIT


# ---------------------------------------------------------------------------
# Stage Protocol — runtime shape check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("impl", [_OkStage, FakeStage], ids=["minimal_probe", "fake"])
def test_stage_protocol_runtime_check_passes_for_minimal_impl(impl: Callable[[], Stage]) -> None:
    """Any class with ``name``, ``process``, ``classify_exception`` is a Stage
    (no production Stage exists yet — the minimal probe and the canonical
    fake are the two impls), and ``process`` returns a :class:`StageOutcome`
    while ``classify_exception`` returns a :class:`StatusCode`.

    Sabotage proof: rename ``process`` to ``do_work`` on ``_OkStage``;
    the ``minimal_probe`` leg's isinstance check fails. Restored.
    """
    stage = impl()
    assert isinstance(stage, Stage)
    assert isinstance(stage.name, str)
    assert isinstance(stage.process(FetchContext(source_name="src", item_id="item-001")), StageOutcome)
    assert isinstance(stage.classify_exception(RuntimeError("x")), StatusCode)


# ---------------------------------------------------------------------------
# Runner parity — every production runner entry point shares the OK / WARN
# routing contract; the two absorbing entry points share the
# exception-absorb contract. One body per contract, run over each runner.
# ---------------------------------------------------------------------------

_Run = Callable[[Stage, "sqlite3.Connection | None"], StageOutcome]


def _run_isolated(stage: Stage, db: sqlite3.Connection | None) -> StageOutcome:
    return IsolatedStageRunner(stage, db=db).run(FetchContext(source_name="src", item_id="item-001"))


def _run_per_item(stage: Stage, db: sqlite3.Connection | None) -> StageOutcome:
    dead_letter = DeadLetterStore(db if db is not None else _schema_db())
    runner = BatchTransactionStageRunner(stage, db=db, dead_letter=dead_letter)
    return runner.run_per_item(FetchContext(source_name="src", item_id="item-001"))


def _run_batch_critical(stage: Stage, db: sqlite3.Connection | None) -> StageOutcome:
    runner = BatchTransactionStageRunner(stage, db=db)
    return runner.run_batch_critical(FetchContext(source_name="src", item_id="item-001"))


def _schema_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    create_schema(db)
    return db


@pytest.fixture(
    params=[_run_isolated, _run_per_item, _run_batch_critical],
    ids=["isolated", "per_item", "batch_critical"],
)
def runner(request: pytest.FixtureRequest) -> _Run:
    """Every production StageRunner entry point."""
    run: _Run = request.param
    return run


@pytest.fixture(params=[_run_isolated, _run_per_item], ids=["isolated", "per_item"])
def absorbing_runner(request: pytest.FixtureRequest) -> _Run:
    """The runner entry points whose contract is absorb-and-return (never raise)."""
    run: _Run = request.param
    return run


def _timeline_rows(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute(
        "SELECT status_code, severity FROM pipeline_item_status WHERE source_name=? AND item_id=?",
        ("src", "item-001"),
    ).fetchall()


def test_runner_passes_ok_outcome_through_without_db(runner: _Run) -> None:
    """When the stage returns OK, the runner returns the same outcome —
    also in the flag-OFF ``db=None`` mode.

    Sabotage proof: change :meth:`IsolatedStageRunner.run`'s success
    branch to ``return StageOutcome(code=StatusCode.FETCH_TIMEOUT)``; the
    ``isolated`` leg fails on ``outcome.code == FETCH_OK``. Restored.
    """
    outcome = runner(_OkStage(), None)
    assert outcome.code == StatusCode.FETCH_OK
    assert outcome.output == b"body-bytes"


def test_runner_ok_outcome_writes_one_timeline_row_and_no_dead_letter(runner: _Run) -> None:
    """With db wired, the OK outcome lands as ONE ``pipeline_item_status``
    row and never touches ``connector_deadletter``.

    Sabotage proof: comment out the ``_emit_outcome(emit, outcome.code, ...)``
    call in :meth:`IsolatedStageRunner.run`'s success branch; the
    ``isolated`` leg's timeline row count drops to zero. Restored.
    """
    db = _schema_db()
    outcome = runner(_OkStage(), db)
    assert outcome.code == StatusCode.FETCH_OK
    rows = _timeline_rows(db)
    assert len(rows) == 1, f"expected one timeline row; got {rows!r}"
    assert tuple(rows[0]) == ("FETCH_OK", "ok")
    dead = db.execute("SELECT COUNT(*) FROM connector_deadletter WHERE source_name=?", ("src",)).fetchone()
    assert dead[0] == 0, "OK outcome must not write to dead_letter"
    db.close()


def test_runner_routes_warn_outcome_through_emit_warn(runner: _Run) -> None:
    """WARN-severity outcomes go through ``emit.warn`` — the timeline
    row records ``severity='warn'``.

    Sabotage proof: in ``_emit_outcome`` replace the ``WARN`` branch's
    ``emit.warn(...)`` with ``pass``; the emit fail-safe records
    ``PIPELINE_STAGE_NO_EMIT`` instead and every leg fails. (Swapping to
    ``emit.ok`` is NOT a valid sabotage — ``ok``/``warn``/``error`` all
    persist the code's own severity.) Restored.
    """
    db = _schema_db()
    outcome = runner(_WarnStage(), db)
    assert outcome.code == StatusCode.FETCH_THROTTLED
    rows = _timeline_rows(db)
    assert len(rows) == 1
    assert tuple(rows[0]) == ("FETCH_THROTTLED", "warn")
    db.close()


def test_runner_absorbs_exception_into_classified_outcome(absorbing_runner: _Run) -> None:
    """When the stage raises, the absorbing runners return an outcome with
    the classified code + exception detail and emit an ERROR timeline row;
    no exception escapes.

    Sabotage proof: in :meth:`IsolatedStageRunner.run` change
    ``return StageOutcome(...)`` in the except branch to ``raise``; the
    ``isolated`` leg fails with the escaped RuntimeError. Restored.
    """
    db = _schema_db()
    outcome = absorbing_runner(_RaisingStage(), db)
    # Did NOT raise — outcome carries the classified code.
    assert outcome.code == StatusCode.FETCH_TIMEOUT
    assert outcome.code.severity == Severity.ERROR
    assert outcome.detail["exception_class"] == "RuntimeError"
    assert "synthetic-failure" in outcome.detail["exception_message"]
    assert [tuple(r) for r in _timeline_rows(db)] == [("FETCH_TIMEOUT", "error")]
    db.close()
