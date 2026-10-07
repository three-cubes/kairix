"""IM-2 unit tests for :class:`ConnectorPipeline` corner cases.

The end-to-end happy-path / dead-letter / poison / rollback paths are
proven in ``tests/integration/test_connector_pipeline.py`` (marked
``@pytest.mark.integration``). This unit file covers the
per-method seams that don't require the full pipeline composition:

* :class:`BatchResult` is a frozen dataclass.
* :class:`ChunkWriter` Protocol is :func:`runtime_checkable`.
* Extract failure (extractor raises) is absorbed into dead_letter,
  same as fetch failure.
* Empty change stream is a no-op (cursor unchanged, zero counts).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kairix.core.connectors import StreamingBronzeStore
from kairix.core.connectors.cursor_store import CursorStore
from kairix.core.connectors.dead_letter import DeadLetterStore
from kairix.core.connectors.pipeline import BatchResult, ChunkWriter, ConnectorPipeline
from kairix.core.connectors.silver import DefaultSilverProcessor
from kairix.core.db.schema import create_schema
from kairix.core.protocols import ChangeEvent
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

pytestmark = pytest.mark.unit


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "kairix.db"))
    create_schema(db)
    return db


def _build_pipeline(db: sqlite3.Connection, tmp_path: Path) -> ConnectorPipeline:
    del tmp_path  # Phase 7: streaming bronze writes no files
    return ConnectorPipeline(
        db=db,
        bronze=StreamingBronzeStore(db),
        silver=DefaultSilverProcessor(),
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
        cursor_store=CursorStore(db),
        dead_letter=DeadLetterStore(db),
    )


def test_batch_result_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(BatchResult)
    params = BatchResult.__dataclass_params__
    assert params.frozen is True


def test_chunk_writer_protocol_is_runtime_checkable() -> None:
    """A class implementing the full Protocol surface satisfies the
    ``runtime_checkable`` ``isinstance`` check.

    ADR-036 (#459 Slice A) extended :class:`ChunkWriter` with
    :meth:`delete_by_source_uri`; the minimal stub here mirrors both
    methods so the Protocol-shape contract stays mechanically enforced.
    """

    class _Writer:
        def upsert(self, _chunks: object) -> int:
            return 0

        def delete_by_source_uri(self, _source_uri: str) -> int:
            return 0

    assert isinstance(_Writer(), ChunkWriter)


def test_extract_failure_is_absorbed_into_dead_letter(tmp_path: Path) -> None:
    """When ``extractor.extract`` raises, the item goes to dead-letter; siblings pass."""

    class _BrokenExtractor(FakeExtractor):
        def __init__(self, fail_on: str) -> None:
            super().__init__()
            self._fail_on = fail_on

        def extract(self, raw: bytes, mime: str) -> object:
            text = raw.decode("utf-8", errors="replace")
            if self._fail_on in text:
                raise RuntimeError(f"extract: simulated failure on {self._fail_on!r}")
            return super().extract(raw, mime)

    db = _open_db(tmp_path)
    try:
        pipeline = _build_pipeline(db, tmp_path)
        events = [
            ChangeEvent(op="modified", item_id=f"note-{i}.md", modified_at=f"2026-05-22T10:0{i}:00Z") for i in (1, 2, 3)
        ]
        content = {
            "note-1.md": b"alpha body content",
            "note-2.md": b"trigger-extract-failure body",
            "note-3.md": b"gamma body content",
        }
        connector = FakeSourceConnector(name="fake-source", events=events, content=content)
        extractor = _BrokenExtractor(fail_on="trigger-extract-failure")

        result = pipeline.run_batch(connector, extractor)

        assert result.processed == 2
        assert result.dead_lettered == 1
        # Item 2 sits in dead_letter with failure_count = 1 and the
        # 'extract: ...' prefix the pipeline records for extract failures.
        row = db.execute(
            "SELECT failure_count, last_error FROM connector_deadletter WHERE source_name = ? AND item_id = ?",
            ("fake-source", "note-2.md"),
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1].startswith("extract:")
    finally:
        db.close()


def test_empty_change_stream_is_a_no_op(tmp_path: Path) -> None:
    """No changes → no chunks written, cursor unchanged, zero counts."""
    db = _open_db(tmp_path)
    try:
        pipeline = _build_pipeline(db, tmp_path)
        connector = FakeSourceConnector(name="fake-source", events=[])
        result = pipeline.run_batch(connector, FakeExtractor())

        assert result.processed == 0
        assert result.dead_lettered == 0
        assert result.poisoned_skipped == 0

        # No cursor was written — fresh connection sees None.
        fresh = sqlite3.connect(str(tmp_path / "kairix.db"))
        try:
            assert CursorStore(fresh).read("fake-source") is None
        finally:
            fresh.close()
    finally:
        db.close()


# ----------------------------------------------------------------------
# Delete-dispatch (connector-architecture-refactor §3.3 primitive #1).
#
# Sabotage proof (executed by the agent, recorded for the reader):
#   In ``kairix/core/connectors/pipeline.py`` ``_process_item``, delete
#   the ``if op in _DELETE_OPS:`` branch. Re-run
#   ``test_delete_op_calls_delete_by_source_uri_and_skips_fetch``: the
#   item falls through to the fetch→upsert path, so ``chunk_writer.deletes``
#   stays empty and ``connector.fetch_calls`` records the item — both
#   assertions fail. Restore the branch; the test passes again.
# ----------------------------------------------------------------------


def _build_pipeline_with_writer(
    db: sqlite3.Connection,
) -> tuple[ConnectorPipeline, FakeChunkWriter]:
    """Pipeline whose chunk writer is returned so deletes can be asserted."""
    writer = FakeChunkWriter()
    pipeline = ConnectorPipeline(
        db=db,
        bronze=StreamingBronzeStore(db),
        silver=DefaultSilverProcessor(),
        chunk_writer=writer,
        entity_graph_sink=FakeEntityGraphSink(),
        cursor_store=CursorStore(db),
        dead_letter=DeadLetterStore(db),
    )
    return pipeline, writer


@pytest.mark.parametrize("delete_op", ["deleted", "archived", "access_lost"])
def test_delete_op_calls_delete_by_source_uri_and_skips_fetch(tmp_path: Path, delete_op: str) -> None:
    """Every delete-op routes to delete_by_source_uri(source_link(item_id)) and skips fetch."""
    db = _open_db(tmp_path)
    try:
        pipeline, writer = _build_pipeline_with_writer(db)
        item_id = "gone.md"
        connector = FakeSourceConnector(
            name="fake-source",
            events=[ChangeEvent(op=delete_op, item_id=item_id, modified_at="2026-06-21T10:00:00Z")],  # type: ignore[arg-type]  # F3-rationale: delete_op is a ChangeEvent.op literal member, parametrized for table coverage.
            content={item_id: b"would-be-reindexed"},
        )

        result = pipeline.run_batch(connector, FakeExtractor())

        # The delete branch fired: writer received exactly the item URI;
        # fetch was skipped; nothing was upserted.
        assert writer.deletes == [connector.source_link(item_id)]
        assert connector.fetch_calls == []
        assert writer.writes == []
        # Outcome accounting: delete is not a processed upsert.
        assert result.processed == 0
        assert result.dead_lettered == 0
        assert result.deleted == 1
    finally:
        db.close()


def test_created_then_deleted_removes_chunk_and_does_not_reindex(tmp_path: Path) -> None:
    """A created tick indexes the chunk; a deleted tick removes it (via the same writer).

    Asserted through the writer's public surface: the create tick upserts
    chunks for the URI; the delete tick calls ``delete_by_source_uri`` for
    that same URI and skips fetch (no re-index).
    """
    db = _open_db(tmp_path)
    try:
        pipeline, writer = _build_pipeline_with_writer(db)
        item_id = "note.md"

        created = FakeSourceConnector(
            name="fake-source",
            events=[ChangeEvent(op="created", item_id=item_id, modified_at="2026-06-21T10:00:00Z")],
            content={item_id: b"body content that becomes a chunk"},
        )
        create_result = pipeline.run_batch(created, FakeExtractor())
        assert create_result.processed == 1
        source_uri = created.source_link(item_id)
        # The create tick upserted at least one chunk under this URI.
        upserted_uris = {getattr(c, "source_uri", "") for batch in writer.writes for c in batch}
        assert source_uri in upserted_uris

        deleted = FakeSourceConnector(
            name="fake-source",
            events=[ChangeEvent(op="deleted", item_id=item_id, modified_at="2026-06-21T11:00:00Z")],
            content={item_id: b"body content that becomes a chunk"},
        )
        delete_result = pipeline.run_batch(deleted, FakeExtractor())

        assert delete_result.deleted == 1
        assert delete_result.processed == 0
        # The delete tick targeted the same URI the create tick wrote.
        assert writer.deletes == [source_uri]
        # The delete tick did not re-fetch / re-index.
        assert deleted.fetch_calls == []
    finally:
        db.close()


def test_created_and_deleted_in_one_batch_nets_to_removed(tmp_path: Path) -> None:
    """Within a single batch a delete-op for a sibling item removes only that item.

    Two created items + one deleted item: both created items index; the
    deleted item routes to delete_by_source_uri without disturbing the
    siblings (per-item dispatch, not batch-wide).
    """
    db = _open_db(tmp_path)
    try:
        pipeline, writer = _build_pipeline_with_writer(db)
        events = [
            ChangeEvent(op="created", item_id="a.md", modified_at="2026-06-21T10:00:00Z"),
            ChangeEvent(op="deleted", item_id="b.md", modified_at="2026-06-21T10:01:00Z"),
            ChangeEvent(op="created", item_id="c.md", modified_at="2026-06-21T10:02:00Z"),
        ]
        connector = FakeSourceConnector(
            name="fake-source",
            events=events,
            content={"a.md": b"alpha body", "c.md": b"gamma body"},
        )

        result = pipeline.run_batch(connector, FakeExtractor())

        assert result.processed == 2
        assert result.deleted == 1
        # Only b.md hit the delete path; a.md/c.md were fetched.
        assert writer.deletes == [connector.source_link("b.md")]
        assert sorted(connector.fetch_calls) == ["a.md", "c.md"]
    finally:
        db.close()


def test_delete_op_source_link_failure_propagates_as_batch_rollback(tmp_path: Path) -> None:
    """If source_link raises on a delete-op, the failure propagates (batch rollback).

    F68-adjacent failure injection: the delete branch reads
    ``connector.source_link(item_id)``. When that raises (the
    ``raise_on_source_link`` seam) the exception is NOT swallowed into
    dead-letter — it propagates to ``run_batch``'s except clause which
    rolls back, matching the silver/writer/sink batch-level discipline.
    """
    db = _open_db(tmp_path)
    try:
        pipeline, writer = _build_pipeline_with_writer(db)
        item_id = "boom.md"
        connector = FakeSourceConnector(
            name="fake-source",
            events=[ChangeEvent(op="deleted", item_id=item_id, modified_at="2026-06-21T10:00:00Z")],
            raise_on_source_link={item_id},
        )

        with pytest.raises(RuntimeError, match="source_link"):
            pipeline.run_batch(connector, FakeExtractor())

        # No delete was recorded (the failure happened before the call
        # could land in a committed chunk).
        assert writer.deletes == []
    finally:
        db.close()


def test_fetch_failure_records_dead_letter(tmp_path: Path) -> None:
    """When ``connector.fetch`` raises, the item lands in dead_letter (fetch path)."""
    db = _open_db(tmp_path)
    try:
        pipeline = _build_pipeline(db, tmp_path)
        events = [ChangeEvent(op="modified", item_id="boom.md", modified_at="2026-05-22T10:00:00Z")]
        connector = FakeSourceConnector(
            name="fake-source",
            events=events,
            content={},
            fail_on_fetch={"boom.md"},
        )

        result = pipeline.run_batch(connector, FakeExtractor())

        assert result.processed == 0
        assert result.dead_lettered == 1
        row = db.execute(
            "SELECT failure_count, last_error FROM connector_deadletter WHERE source_name = ? AND item_id = ?",
            ("fake-source", "boom.md"),
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1].startswith("fetch:")
    finally:
        db.close()


def test_poisoned_item_is_skipped_before_fetch(tmp_path: Path) -> None:
    """Pre-seeded poison → pipeline skips the item without calling fetch."""
    db = _open_db(tmp_path)
    try:
        pipeline = _build_pipeline(db, tmp_path)
        # Seed three pre-existing failures so the item is already poisoned.
        dead_letter = DeadLetterStore(db)
        for _ in range(3):
            dead_letter.record("fake-source", "poisoned.md", "earlier failure")
        db.commit()

        events = [
            ChangeEvent(op="modified", item_id="poisoned.md", modified_at="2026-05-22T10:00:00Z"),
        ]
        connector = FakeSourceConnector(
            name="fake-source",
            events=events,
            content={"poisoned.md": b"body that won't be fetched"},
        )

        result = pipeline.run_batch(connector, FakeExtractor())

        assert result.processed == 0
        assert result.dead_lettered == 0
        assert result.poisoned_skipped == 1
        # The pipeline skipped fetch entirely.
        assert connector.fetch_calls == []
    finally:
        db.close()


def test_batch_level_failure_rolls_back(tmp_path: Path) -> None:
    """Silver raises → run_batch rolls back and re-raises."""

    class _ExplodingSilver:
        def process(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("silver: unit-level rollback proof")

    db = _open_db(tmp_path)
    try:
        # Seed a pre-existing cursor so we can prove it doesn't advance.
        CursorStore(db).write("fake-source", "PRE-EXISTING")
        db.commit()

        pipeline = ConnectorPipeline(
            db=db,
            bronze=StreamingBronzeStore(db),
            silver=_ExplodingSilver(),  # type: ignore[arg-type]  # F3-rationale: synthetic Protocol-compliant stand-in for negative-path test.
            chunk_writer=FakeChunkWriter(),
            entity_graph_sink=FakeEntityGraphSink(),
            cursor_store=CursorStore(db),
            dead_letter=DeadLetterStore(db),
        )
        events = [ChangeEvent(op="modified", item_id="x.md", modified_at="2026-05-22T11:00:00Z")]
        connector = FakeSourceConnector(name="fake-source", events=events, content={"x.md": b"body"})

        with pytest.raises(RuntimeError, match="silver: unit-level rollback proof"):
            pipeline.run_batch(connector, FakeExtractor())

        # Cursor unchanged on a fresh connection — rollback took effect.
        fresh = sqlite3.connect(str(tmp_path / "kairix.db"))
        try:
            assert CursorStore(fresh).read("fake-source") == "PRE-EXISTING"
        finally:
            fresh.close()
    finally:
        db.close()


# ----------------------------------------------------------------------
# GH #336 (ADR-024 Bundle B) — _safe_quality_ok + _safe_outcome_write
# defensive fallback branches are exercised through the public pipeline
# surface via tests/integration/test_documents_media_writer.py. Per F5
# we don't import the underscore-prefixed helpers directly — instead
# the tests below drive ConnectorPipeline.run_batch with stand-in
# extractors / silvers that hit each fallback path.
# ----------------------------------------------------------------------


class _ExtractorMissingQuality:
    """Extractor stand-in with no ``quality_ok`` attribute — exercises the public fallback."""

    name = "no-quality"
    version = "v0"

    def can_extract(self, _mime: str, _magic: bytes) -> bool:
        return True

    def extract(self, raw: bytes, _mime: str) -> Any:
        from kairix.core.protocols import DocMetadata, ExtractedDocument

        return ExtractedDocument(
            markdown=raw.decode("utf-8", errors="replace") or "body",
            pages=(),
            images=(),
            metadata=DocMetadata(title=None, author=None, created_date=None, language=None, page_count=None),
            confidence=1.0,
        )

    # No ``quality_ok`` -> _safe_quality_ok's "method missing" branch
    # fires when the pipeline calls it. The expected status the
    # orchestrator surfaces is ``ok`` (the helper defaults to True).

    def metadata_for(self, _raw: bytes, _mime: str) -> Any:
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class _ExtractorRaisingQuality:
    """Extractor stand-in whose ``quality_ok`` raises — exercises the public swallow fallback."""

    name = "raise-quality"
    version = "v0"

    def can_extract(self, _mime: str, _magic: bytes) -> bool:
        return True

    def extract(self, raw: bytes, _mime: str) -> Any:
        from kairix.core.protocols import DocMetadata, ExtractedDocument

        return ExtractedDocument(
            markdown=raw.decode("utf-8", errors="replace") or "body",
            pages=(),
            images=(),
            metadata=DocMetadata(title=None, author=None, created_date=None, language=None, page_count=None),
            confidence=1.0,
        )

    def quality_ok(self, _doc: object) -> bool:
        raise RuntimeError("scripted quality_ok failure")

    def metadata_for(self, _raw: bytes, _mime: str) -> Any:
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


def _run_one_item_through_factory_pipeline(tmp_path: Path, extractor: object) -> str | None:
    """Drive one happy-path item through the production factory pipeline.

    Returns the resulting ``documents_media.extraction_status`` so the
    caller can assert the safe-quality-ok fallback produces ``ok``
    (the documented default when the helper's exception/missing-method
    fallback fires).
    """
    from kairix.core import factory
    from kairix.core.db.schema import create_schema

    db = sqlite3.connect(str(tmp_path / "fallback.sqlite"))
    create_schema(db)
    pipeline = factory.build_connector_pipeline(
        db=db,
        collection="safe-quality-fallback",
        chunk_writer=FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
    )
    connector = FakeSourceConnector(
        name="fallback-source",
        events=[ChangeEvent(op="modified", item_id="doc.md", modified_at="2026-05-28T10:00:00Z")],
        content={"doc.md": b"body text"},
        cursor_token="fallback-cursor",
    )
    pipeline.run_batch(connector, extractor)  # type: ignore[arg-type]  # F3-rationale: synthetic stand-in satisfies the runtime-checkable Extractor Protocol
    row = db.execute("SELECT extraction_status FROM documents_media LIMIT 1").fetchone()
    db.close()
    return None if row is None else str(row[0])


def test_extractor_missing_quality_method_yields_ok_status_via_public_pipeline(tmp_path: Path) -> None:
    """Extractor with no quality_ok method -> safe-quality-ok fallback returns True -> status='ok'."""
    status = _run_one_item_through_factory_pipeline(tmp_path, _ExtractorMissingQuality())
    assert status == "ok"


def test_extractor_quality_method_raising_yields_ok_status_via_public_pipeline(tmp_path: Path) -> None:
    """Extractor whose quality_ok raises -> safe-quality-ok swallows -> status='ok'."""
    status = _run_one_item_through_factory_pipeline(tmp_path, _ExtractorRaisingQuality())
    assert status == "ok"


# ---------------------------------------------------------------------------
# Pipeline handling of SourceConnector methods that raise mid-item
# (moved from tests/contracts/test_source_connector_failure_modes.py —
# these pin ConnectorPipeline behaviour; no shipped connector raises from
# source_link / sensitivity_for / next_cursor / metadata_for, so the
# failure is injected via FakeSourceConnector's knobs).
# ---------------------------------------------------------------------------


def _factory_pipeline(
    db: sqlite3.Connection,
    *,
    chunk_writer: FakeChunkWriter | None = None,
) -> Any:
    """ConnectorPipeline composed via the factory entry point (F47 shape)."""
    from kairix.core.factory import build_connector_pipeline

    return build_connector_pipeline(
        db=db,
        collection="default",
        chunk_writer=chunk_writer if chunk_writer is not None else FakeChunkWriter(),
        entity_graph_sink=FakeEntityGraphSink(),
    )


def _factory_event(item_id: str, modified_at: str = "2026-01-01T00:00:00Z") -> ChangeEvent:
    return ChangeEvent(op="created", item_id=item_id, modified_at=modified_at)


def _factory_dead_letter_rows(db: sqlite3.Connection, source_name: str) -> list[tuple[str, str]]:
    return list(
        db.execute(
            "SELECT item_id, last_error FROM connector_deadletter WHERE source_name = ? ORDER BY item_id",
            (source_name,),
        ).fetchall()  # F63-bounded: one-source fixture DB, at most a handful of rows
    )


def _factory_cursor_token(db: sqlite3.Connection, source_name: str) -> str | None:
    row = db.execute(
        "SELECT cursor_token FROM connector_cursors WHERE source_name = ?",
        (source_name,),
    ).fetchone()
    return None if row is None else row[0]


def test_source_link_raises_propagates_and_rolls_back_chunk(tmp_path: Path) -> None:
    """``source_link`` is called by ``_process_item`` AFTER bronze write
    + extract — a raise propagates and rolls back that chunk's
    bronze rows. The pipeline does NOT wrap ``source_link`` in a
    try/except (only ``fetch`` and ``extract`` are absorbed) — so the
    expected behaviour is "exception escapes ``run_batch``".

    Sabotage proof: in ``FakeSourceConnector.source_link`` remove the
    ``if item_id in self._raise_on_source_link: raise ...`` block.
    Re-run: the test fails because ``pytest.raises`` sees nothing and
    the items index successfully. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    source = FakeSourceConnector(
        name="source-link-raises",
        events=[_factory_event("item-001")],
        content={"item-001": b"body"},
        raise_on_source_link={"item-001"},
    )
    pipeline = _factory_pipeline(db)
    with pytest.raises(RuntimeError, match="simulated source_link failure"):
        pipeline.run_batch(source, FakeExtractor())
    db.close()


def test_sensitivity_for_raises_propagates_and_rolls_back_chunk(tmp_path: Path) -> None:
    """``sensitivity_for`` is called by ``_process_item`` for the silver
    pass. A raise here propagates — the chunk rolls back and no
    content / entity_signals row is written for that item.

    Sabotage proof: comment out the raise branch in
    ``FakeSourceConnector.sensitivity_for``. Re-run: the test fails
    because ``pytest.raises`` sees no exception. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    writer = FakeChunkWriter()
    source = FakeSourceConnector(
        name="sensitivity-raises",
        events=[_factory_event("item-001")],
        content={"item-001": b"body"},
        raise_on_sensitivity_for={"item-001"},
    )
    pipeline = _factory_pipeline(db, chunk_writer=writer)
    with pytest.raises(RuntimeError, match="simulated sensitivity_for failure"):
        pipeline.run_batch(source, FakeExtractor())
    # Failing chunk rolled back — no chunk batch reached the writer
    # (the silver pass raised before reaching ``chunk_writer.upsert``).
    assert writer.writes == [], f"writer must not have received chunks; got {writer.writes!r}"
    db.close()


def test_next_cursor_raises_propagates_and_aborts_commit(tmp_path: Path) -> None:
    """``next_cursor`` is called by ``_commit_and_flush`` at end-of-chunk
    AND end-of-batch. A raise during ``next_cursor`` propagates;
    the cursor never advances and the pipeline rolls back.

    Sabotage proof: comment out the raise branch in
    ``FakeSourceConnector.next_cursor``. Re-run: the test fails
    because ``pytest.raises`` sees no exception and the cursor
    advance attempt succeeds. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    source = FakeSourceConnector(
        name="next-cursor-raises",
        events=[_factory_event("item-001")],
        content={"item-001": b"body"},
        raise_on_next_cursor=RuntimeError("F68-next-cursor-raises"),
    )
    pipeline = _factory_pipeline(db)
    with pytest.raises(RuntimeError, match="F68-next-cursor-raises"):
        pipeline.run_batch(source, FakeExtractor())
    # Cursor row never created — the commit didn't survive the raise.
    assert _factory_cursor_token(db, "next-cursor-raises") is None
    db.close()


def test_metadata_for_raises_returns_empty_metadata_chunk_still_indexed(tmp_path: Path) -> None:
    """ADR-021 Wave E.5 contract — ``metadata_for`` failing is NEVER
    fatal. The pipeline falls back to empty :class:`SourceMetadata`
    via :func:`_safe_connector_metadata` and the chunk still flows to
    the writer.

    Note this is intentionally the only Protocol method on
    SourceConnector whose ``raises`` failure class observably looks
    like ``returns_empty`` — the wrapper absorbs the exception by
    design (see ``kairix/core/connectors/pipeline.py:464``).

    Sabotage proof: in ``kairix/core/connectors/pipeline.py``
    :func:`_safe_connector_metadata`, change the
    ``except Exception: return SourceMetadata()`` to ``except Exception: raise``.
    Re-run: the test fails because the pipeline now propagates the
    RuntimeError instead of falling back. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    writer = FakeChunkWriter()
    source = FakeSourceConnector(
        name="metadata-raises",
        events=[_factory_event("item-001")],
        content={"item-001": b"body-content"},
        raise_on_metadata_for={"item-001"},
    )
    pipeline = _factory_pipeline(db, chunk_writer=writer)
    result = pipeline.run_batch(source, FakeExtractor())

    # The chunk IS written — metadata_for failure is absorbed.
    assert result.processed == 1
    assert result.dead_lettered == 0
    assert len(writer.writes) == 1, f"writer should have received exactly one chunk batch; got {writer.writes!r}"
    # And no dead-letter entry — metadata failures do not dead-letter.
    assert _factory_dead_letter_rows(db, "metadata-raises") == []
    db.close()
