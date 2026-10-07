"""Step implementations for connector_per_tick_budget.feature (ADR-020 / F66).

The scenarios drive a real :class:`kairix.core.connectors.pipeline.ConnectorPipeline`
through ``kairix.core.factory.build_connector_pipeline`` (F46-sanctioned
entry point) and assert:

* The per-tick budget cap fires at ``per_tick_max_items`` and the
  pipeline commits the partial cursor before yielding.
* The watermark gate skips the tick when the injected
  ``disk_free_resolver`` reports less free space than the connector's
  ``disk_watermark_min_free_bytes``.

F1-clean: no @patch / monkeypatch. F2-clean: no ``KAIRIX_*`` env reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core import factory
from kairix.core.connectors.pipeline import BatchResult, ConnectorPipeline
from kairix.core.db.schema import create_schema
from kairix.core.protocols import ChangeEvent
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

# Per-tick cursor tokens; each tick uses a distinct token so the test
# can prove that the cursor row advances exactly once per tick. The
# value strings mimic the SharePoint deltaLink shape so the contract
# stays close to production cursor semantics.
_BUDGET_TICK_TOKENS = (
    "deltaLink-after-tick-1",
    "deltaLink-after-tick-2",
    "deltaLink-after-tick-3",
)


@dataclass
class _BudgetState:
    db: sqlite3.Connection
    pipeline: ConnectorPipeline
    source_name: str = ""
    events: list[ChangeEvent] = field(default_factory=list)
    body: bytes = b""
    per_tick_max_items: int = 500
    watermark_min_free_bytes: int | None = None
    disk_free_bytes: int = 10 * 1024**3  # 10 GiB default — well above any sensible watermark
    tick_results: list[BatchResult] = field(default_factory=list)
    cursor_tokens_after_each_tick: list[Any] = field(default_factory=list)


@pytest.fixture
def budget_state(tmp_path: Path) -> _BudgetState:
    db_path = tmp_path / "per_tick_budget.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    pipeline = factory.build_connector_pipeline(
        db=db,
        collection="per-tick-budget-test",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
    )
    return _BudgetState(db=db, pipeline=pipeline)


def _make_events(count: int) -> list[ChangeEvent]:
    """Build ``count`` change events with deterministic item_ids + timestamps."""
    return [
        ChangeEvent(
            op="modified",
            item_id=f"item-{i:05d}.md",
            modified_at=f"2026-05-28T00:{(i // 60) % 60:02d}:{i % 60:02d}Z",
        )
        for i in range(count)
    ]


@given(parsers.parse('a connector "{name}" with {count:d} change events queued'))
def given_connector_with_events(budget_state: _BudgetState, name: str, count: int) -> None:
    budget_state.source_name = name
    budget_state.events = _make_events(count)
    budget_state.body = ("body. " * 30).encode("utf-8")


@given(parsers.parse('a connector "{name}" with five change events queued'))
def given_watermark_connector(budget_state: _BudgetState, name: str) -> None:
    budget_state.source_name = name
    budget_state.events = _make_events(5)
    budget_state.body = ("body. " * 30).encode("utf-8")


@given(parsers.parse("the connector declares a per_tick_max_items of {budget:d}"))
def given_per_tick_max_items(budget_state: _BudgetState, budget: int) -> None:
    budget_state.per_tick_max_items = budget


@given("the connector declares a disk_watermark_min_free_bytes of five gibibytes")
def given_watermark(budget_state: _BudgetState) -> None:
    budget_state.watermark_min_free_bytes = 5 * 1024**3


@given("the disk_free_resolver reports only one gibibyte free")
def given_low_disk(budget_state: _BudgetState) -> None:
    budget_state.disk_free_bytes = 1 * 1024**3
    # Rebuild the pipeline through the factory with the injected
    # resolver — F46 requires the factory entry point. The previous
    # pipeline from the fixture has the production default; we swap
    # it here because the watermark scenario needs deterministic free
    # bytes.
    budget_state.pipeline = factory.build_connector_pipeline(
        db=budget_state.db,
        collection="per-tick-budget-test",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
        deps=factory.FactoryDeps(disk_free_override=lambda: budget_state.disk_free_bytes),
    )


@when(parsers.parse('the operator runs three consecutive pipeline ticks for "{name}"'))
def when_three_ticks(budget_state: _BudgetState, name: str) -> None:
    assert name == budget_state.source_name
    for i in range(3):
        connector = FakeSourceConnector(
            name=budget_state.source_name,
            events=budget_state.events,
            content={ev.item_id: budget_state.body for ev in budget_state.events},
            cursor_token=_BUDGET_TICK_TOKENS[i],
            per_tick_max_items=budget_state.per_tick_max_items,
            disk_watermark_min_free_bytes=budget_state.watermark_min_free_bytes,
        )
        result = budget_state.pipeline.run_batch(connector, FakeExtractor())
        budget_state.tick_results.append(result)
        stored = budget_state.db.execute(
            "SELECT cursor_token FROM connector_cursors WHERE source_name = ?",
            (budget_state.source_name,),
        ).fetchone()
        budget_state.cursor_tokens_after_each_tick.append(stored[0] if stored else None)


@when(parsers.parse('the operator runs one pipeline tick for "{name}"'))
def when_one_tick(budget_state: _BudgetState, name: str) -> None:
    assert name == budget_state.source_name
    connector = FakeSourceConnector(
        name=budget_state.source_name,
        events=budget_state.events,
        content={ev.item_id: budget_state.body for ev in budget_state.events},
        cursor_token=_BUDGET_TICK_TOKENS[0],
        per_tick_max_items=budget_state.per_tick_max_items,
        disk_watermark_min_free_bytes=budget_state.watermark_min_free_bytes,
    )
    result = budget_state.pipeline.run_batch(connector, FakeExtractor())
    budget_state.tick_results.append(result)


@then(parsers.parse("tick 1 processes exactly {count:d} items and yields with budget_yielded True"))
def then_tick1_budget(budget_state: _BudgetState, count: int) -> None:
    result = budget_state.tick_results[0]
    assert result.processed == count, f"tick 1 processed {result.processed!r}, expected {count}"
    assert result.budget_yielded is True, "tick 1 should have hit the per-tick budget cap"


@then(parsers.parse("tick 2 processes exactly {count:d} items and yields with budget_yielded True"))
def then_tick2_budget(budget_state: _BudgetState, count: int) -> None:
    result = budget_state.tick_results[1]
    assert result.processed == count, f"tick 2 processed {result.processed!r}, expected {count}"
    assert result.budget_yielded is True, "tick 2 should have hit the per-tick budget cap"


@then(parsers.parse("tick 3 processes exactly {count:d} items and the cursor advances each tick"))
def then_tick3_processes_and_advances(budget_state: _BudgetState, count: int) -> None:
    result = budget_state.tick_results[2]
    assert result.processed == count, f"tick 3 processed {result.processed!r}, expected {count}"


@then("the persisted cursor row has advanced three times across the three ticks")
def then_cursor_advances_three_times(budget_state: _BudgetState) -> None:
    assert budget_state.cursor_tokens_after_each_tick == list(_BUDGET_TICK_TOKENS), (
        f"cursor did not advance once per tick; got {budget_state.cursor_tokens_after_each_tick!r}"
    )


@then("zero items are processed")
def then_zero_processed(budget_state: _BudgetState) -> None:
    result = budget_state.tick_results[0]
    assert result.processed == 0, f"watermark-skipped tick processed {result.processed!r}, expected 0"


@then("the BatchResult reports skipped_low_disk True")
def then_skipped_low_disk(budget_state: _BudgetState) -> None:
    result = budget_state.tick_results[0]
    assert result.skipped_low_disk is True, "watermark-skipped tick must report skipped_low_disk=True"


@then("the persisted cursor row is unchanged")
def then_cursor_unchanged(budget_state: _BudgetState) -> None:
    stored = budget_state.db.execute(
        "SELECT cursor_token FROM connector_cursors WHERE source_name = ?",
        (budget_state.source_name,),
    ).fetchone()
    assert stored is None, f"watermark skip should not write a cursor row; got {stored!r}"
