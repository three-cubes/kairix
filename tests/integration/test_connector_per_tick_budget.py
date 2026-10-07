"""ADR-020 / F66 — per-tick budget + disk-watermark gate, integration shape.

Three invariants pinned:

1. **Budget cap** — the pipeline processes at most
   ``connector.per_tick_max_items`` items per tick, commits the partial
   cursor, and reports ``budget_yielded=True``. The bronze count
   matches the budget cap.
2. **Multi-tick drain** — three sequential ticks against the same
   pipeline drain a 1500-item backlog (budget 500). Final bronze count
   = 1500. Cursor advances exactly once per tick.
3. **Watermark skip** — when the injected ``disk_free_resolver``
   reports less free than the connector's
   ``disk_watermark_min_free_bytes``, the tick yields immediately. No
   bronze writes, no cursor write — mirrors the
   ``test_quiet_tick_preserves_prior_cursor`` invariant from
   :mod:`tests.integration.test_connector_cursor_advance`.

Sabotage proofs (executed by the author with mutate → fail → restore):

  * In ``kairix/core/connectors/pipeline.py:_process_batch``, remove the
    ``if items_seen >= budget: raise _BudgetExhausted(...)`` branch.
    Re-run :func:`test_budget_caps_items_processed_per_tick` — the
    pipeline drains all 1500 events; ``result.processed == 500``
    assertion fails. Restore; test passes.
  * In ``kairix/core/connectors/pipeline.py:run_batch``, remove the
    watermark-gate early return. Re-run
    :func:`test_watermark_skip_preserves_cursor` — the pipeline writes
    bronze rows + cursor; ``result.skipped_low_disk is True`` fails.
    Restore; test passes.
  * In :func:`test_three_ticks_drain_full_corpus` — remove the
    cursor-token-per-tick rotation by reusing the same cursor token;
    the cursor-advance count drops to 1. Restore; test passes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.schema import create_schema
from kairix.core.factory import FactoryDeps, build_connector_pipeline
from kairix.core.protocols import ChangeEvent
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

pytestmark = pytest.mark.integration


_CURSOR_TOKENS_PER_TICK = (
    "deltaLink-tick-1",
    "deltaLink-tick-2",
    "deltaLink-tick-3",
)


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "per_tick_budget.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    return db


def _make_events(count: int) -> list[ChangeEvent]:
    return [
        ChangeEvent(
            op="modified",
            item_id=f"doc-{i:05d}.md",
            modified_at=f"2026-05-28T00:{(i // 60) % 60:02d}:{i % 60:02d}Z",
        )
        for i in range(count)
    ]


def _bronze_count(db: sqlite3.Connection, source_name: str) -> int:
    return int(
        db.execute(
            "SELECT COUNT(*) FROM bronze_records WHERE source_name = ?",
            (source_name,),
        ).fetchone()[0]
    )


def _read_cursor(db: sqlite3.Connection, source_name: str) -> str | None:
    row = db.execute(
        "SELECT cursor_token FROM connector_cursors WHERE source_name = ?",
        (source_name,),
    ).fetchone()
    return row[0] if row is not None else None


def test_budget_caps_items_processed_per_tick(tmp_path: Path) -> None:
    """One tick processes exactly ``per_tick_max_items`` of a larger backlog."""
    db = _open_db(tmp_path)
    pipeline = build_connector_pipeline(
        db=db,
        collection="budget-cap-test",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
    )
    events = _make_events(1500)
    body = ("body. " * 30).encode("utf-8")
    connector = FakeSourceConnector(
        name="budget-cap-test",
        events=events,
        content={ev.item_id: body for ev in events},
        cursor_token=_CURSOR_TOKENS_PER_TICK[0],
        per_tick_max_items=500,
    )

    result = pipeline.run_batch(connector, FakeExtractor())

    assert result.processed == 500, f"processed={result.processed!r}, expected 500"
    assert result.budget_yielded is True, "tick should have yielded at the budget cap"
    assert result.skipped_low_disk is False, "watermark gate should not have fired"
    assert _bronze_count(db, "budget-cap-test") == 500
    assert _read_cursor(db, "budget-cap-test") == _CURSOR_TOKENS_PER_TICK[0], (
        "cursor must advance to the connector's next_cursor() token at the budget boundary"
    )


def test_three_ticks_drain_full_corpus(tmp_path: Path) -> None:
    """A 1500-item backlog (budget 500) drains over exactly three ticks.

    Each tick's connector yields the next 500-item slice (mirrors the
    real cursor-driven shape: tick 1's deltaLink response carries
    items 0..499, tick 2's items 500..999, tick 3's items 1000..1499).
    The pipeline's budget cap fires at item 500 in each slice and
    commits the cursor token before yielding.
    """
    db = _open_db(tmp_path)
    pipeline = build_connector_pipeline(
        db=db,
        collection="drain-test",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
    )
    all_events = _make_events(1500)
    body = ("body. " * 30).encode("utf-8")
    cursor_history: list[str | None] = []
    processed_history: list[int] = []

    for i in range(3):
        # Each tick's connector simulates the deltaLink-driven slice:
        # tick i sees items [500*i .. 500*(i+1)). 500 items + budget 500
        # means the budget cap fires on the last item and the cursor
        # advances to this tick's token.
        slice_events = all_events[500 * i : 500 * (i + 1)]
        connector = FakeSourceConnector(
            name="drain-test",
            events=slice_events,
            content={ev.item_id: body for ev in slice_events},
            cursor_token=_CURSOR_TOKENS_PER_TICK[i],
            per_tick_max_items=500,
        )
        result = pipeline.run_batch(connector, FakeExtractor())
        processed_history.append(result.processed)
        cursor_history.append(_read_cursor(db, "drain-test"))

    assert processed_history == [500, 500, 500], (
        f"each tick should process exactly 500 items; got {processed_history!r}"
    )
    assert cursor_history == list(_CURSOR_TOKENS_PER_TICK), (
        f"cursor must advance to each tick's token in turn; got {cursor_history!r}"
    )
    assert _bronze_count(db, "drain-test") == 1500, "all 1500 events should land in bronze after three ticks"


def test_watermark_skip_preserves_cursor(tmp_path: Path) -> None:
    """Watermark gate yields with zero bronze writes and an untouched cursor."""
    db = _open_db(tmp_path)
    pipeline = build_connector_pipeline(
        db=db,
        collection="watermark-test",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
        deps=FactoryDeps(disk_free_override=lambda: 1 * 1024**3),  # 1 GiB free
    )
    events = _make_events(10)
    body = ("body. " * 30).encode("utf-8")
    connector = FakeSourceConnector(
        name="watermark-test",
        events=events,
        content={ev.item_id: body for ev in events},
        cursor_token=_CURSOR_TOKENS_PER_TICK[0],
        per_tick_max_items=500,
        disk_watermark_min_free_bytes=5 * 1024**3,  # 5 GiB required
    )

    result = pipeline.run_batch(connector, FakeExtractor())

    assert result.skipped_low_disk is True, "watermark gate should have skipped the tick"
    assert result.processed == 0, f"watermark-skipped tick processed {result.processed!r}, expected 0"
    assert result.budget_yielded is False, "watermark skip should NOT report budget_yielded"
    assert _bronze_count(db, "watermark-test") == 0, "watermark skip should write no bronze rows"
    assert _read_cursor(db, "watermark-test") is None, "watermark skip should not persist a cursor row"
