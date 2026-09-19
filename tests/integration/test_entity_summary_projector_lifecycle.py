"""F47 lifecycle integration for :class:`EntitySummaryProjectorImpl`
(ADR-036 §Mechanics, #460 Slice B).

Composes the projector through the canonical seams:

  * Neo4j client → :class:`FakeGraphRepository` driven via ``cypher_rows``
  * ChunkWriter → real ``_SqliteChunkWriter`` resolved via the public
    :func:`legacy_chunk_writer` entry point (F5/F61 clean)

Walks the production happy path end-to-end: seed Neo4j with a
pending entity → ``projector.tick()`` → assert SQLite chunk row + FTS5
row + Neo4j mark-indexed Cypher fired. Then runs a second tick
asserting idempotency (no new chunks, no new writer calls).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kairix.core.connectors.collection_router import legacy_chunk_writer
from kairix.core.db.schema import create_schema
from kairix.knowledge.entities.summary_projector import (
    DefaultProjectorBuilderDeps,
    EntitySummaryProjectorImpl,
    default_projector_builder,
    hash_summary,
)
from tests.fakes import FakeGraphRepository

pytestmark = pytest.mark.integration


_FIXED_TICK = "2026-06-09T00:00:00Z"


def _seed_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    create_schema(db)
    db.commit()
    return db


def _row(
    *,
    name: str,
    qid: str,
    summary: str,
    prior_hash: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "qid": qid,
        "summary": summary,
        "prior_hash": prior_hash,
        "summary_source": "wikidata",
    }


def test_production_default_builder_wires_live_graph_and_sqlite_writer(tmp_path: Path) -> None:
    """The worker's non-injected builder projects through real SQLite wiring.

    Only process boundaries are supplied: the production builder still owns
    schema creation, ``entity-summaries`` writer composition, transaction
    commit, and connection closure.  This catches the former placeholder
    graph/no-op writer default that made every live tick silently idle.
    """
    db_path = tmp_path / "kairix.db"
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary="Systems research leader")],
    )

    projector = default_projector_builder(
        DefaultProjectorBuilderDeps(
            db_factory=lambda: sqlite3.connect(str(db_path)),
            neo4j_factory=lambda: neo4j,
        )
    )
    try:
        result = projector.tick(per_tick_max_items=10)
    finally:
        projector.close()

    assert result.projected == 1
    db = sqlite3.connect(str(db_path))
    try:
        row = db.execute(
            "SELECT collection, source_uri FROM documents WHERE source_uri = ?",
            ("entity://Q42",),
        ).fetchone()
    finally:
        db.close()
    assert row == ("entity-summaries", "entity://Q42")
    assert any("SET n.summary_indexed_at" in query for query, _params in neo4j.cypher_calls)


def test_lifecycle_seed_tick_assert_chunk_and_fts(tmp_path: Path) -> None:
    # F69-small-scale-only: scale variant in tests/soak/test_entity_summary_projector_at_scale.py (ADR-024)
    """Full happy path: pending entity → tick → SQLite + FTS5 + Neo4j mark.

    Sabotage-proof: drop ``self._mark_indexed(...)`` in the projector
    and ``mark_calls`` is empty — re-projection would loop forever in
    a real worker tick chain.
    """
    db = _seed_db(tmp_path / "kairix.db")
    writer = legacy_chunk_writer(db, collection="entity-summaries")
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary="AI policy research institute")],
    )
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=writer,
        clock=lambda: _FIXED_TICK,
    )

    result = projector.tick(per_tick_max_items=10)
    db.commit()

    assert result.projected == 1
    assert result.failed == 0

    # SQLite row + content present.
    rows = db.execute(
        "SELECT collection, source_uri FROM documents WHERE source_uri = ?",
        ("entity://Q42",),
    ).fetchall()
    assert rows == [("entity-summaries", "entity://Q42")]
    content = db.execute(
        "SELECT c.doc FROM content c JOIN documents d ON d.hash = c.hash WHERE d.source_uri = ?",
        ("entity://Q42",),
    ).fetchall()
    assert content == [("AI policy research institute",)]
    # FTS5 row carries the searchable text.
    fts_rows = db.execute(
        "SELECT rowid FROM documents_fts WHERE doc MATCH ?",  # F63-bounded: lifecycle scope = one entity
        ("policy",),
    ).fetchall()
    assert len(fts_rows) == 1

    # Neo4j received: 1 poll + 1 mark-indexed call.
    assert len(neo4j.cypher_calls) == 2
    mark_calls = [call for call in neo4j.cypher_calls if "SET n.summary_indexed_at" in call[0]]
    assert len(mark_calls) == 1
    assert mark_calls[0][1]["name"] == "Ada"
    assert mark_calls[0][1]["hash"] == hash_summary("AI policy research institute")


def test_lifecycle_second_tick_is_idempotent_when_neo4j_reports_indexed_hash(
    tmp_path: Path,
) -> None:
    """Second tick where Neo4j reports the entity already indexed
    under the current hash → skipped=1, no new chunk row, no new
    cypher mark-indexed call. Locks ADR-036 idempotency contract."""
    db = _seed_db(tmp_path / "kairix.db")
    writer = legacy_chunk_writer(db, collection="entity-summaries")
    summary = "stable description"
    digest = hash_summary(summary)
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary=summary, prior_hash=digest)],
    )
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=writer,
        clock=lambda: _FIXED_TICK,
    )

    result = projector.tick(per_tick_max_items=10)
    db.commit()

    assert result.skipped == 1
    assert result.projected == 0
    rows = db.execute(
        "SELECT COUNT(*) FROM documents WHERE source_uri = ?",
        ("entity://Q42",),
    ).fetchone()
    assert rows[0] == 0
    # Legacy rows without the indexed-summary comparison value are backfilled
    # once, without rewriting SQLite, so future polls filter them server-side.
    assert len(neo4j.cypher_calls) == 2


def test_poll_filters_indexed_rows_before_limit_so_backlog_progresses(tmp_path: Path) -> None:
    """A full indexed prefix cannot starve a pending entity behind the cap."""

    class _BacklogGraph:
        def __init__(self) -> None:
            self.rows = [
                {
                    **_row(name="Already A", qid="Q1", summary="stable a", prior_hash=hash_summary("stable a")),
                    "prior_summary": "stable a",
                },
                {
                    **_row(name="Already B", qid="Q2", summary="stable b", prior_hash=hash_summary("stable b")),
                    "prior_summary": "stable b",
                },
                {**_row(name="Pending C", qid="Q3", summary="new c"), "prior_summary": ""},
            ]

        def cypher(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "SET n.summary_indexed_at" in query:
                for row in self.rows:
                    if row["name"] == params["name"]:
                        row["prior_hash"] = params["hash"]
                        row["prior_summary"] = params["summary"]
                        return [{"name": row["name"]}]
                return []
            # This small semantic fake mirrors the graph predicate: pending
            # selection occurs before the bound is applied.
            pending = [row for row in self.rows if not row["prior_summary"] or row["prior_summary"] != row["summary"]]
            return pending[: int(params["per_tick_max_items"])]

    db = _seed_db(tmp_path / "backlog.sqlite")
    graph = _BacklogGraph()
    projector = EntitySummaryProjectorImpl(
        neo4j=graph,
        chunk_writer=legacy_chunk_writer(db, collection="entity-summaries"),
        clock=lambda: _FIXED_TICK,
        commit=db.commit,
    )

    result = projector.tick(per_tick_max_items=2)

    assert result.projected == 1
    assert result.skipped == 0
    assert db.execute(
        "SELECT source_uri FROM documents WHERE source_uri = ?",
        ("entity://Q3",),
    ).fetchone() == ("entity://Q3",)


def test_sqlite_commit_precedes_graph_mark_and_failed_mark_retries_safely(tmp_path: Path) -> None:
    """Neo4j never claims a chunk that SQLite has not committed.

    A swallowed Neo4j write failure returns no rows. The first tick reports a
    failed projection but leaves the idempotent SQLite chunk committed. A
    later tick retries the pending graph row and marks it without duplicating
    the chunk.
    """

    class _RecoveringGraph:
        def __init__(self) -> None:
            self.mark_attempts = 0
            self.marked = False

        def cypher(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "SET n.summary_indexed_at" not in query:
                if self.marked:
                    return []
                return [{**_row(name="Ada", qid="Q42", summary="durable summary"), "prior_summary": ""}]
            self.mark_attempts += 1
            if self.mark_attempts == 1:
                return []
            self.marked = True
            return [{"name": params["name"]}]

    db_path = tmp_path / "ordering.sqlite"
    db = _seed_db(db_path)
    graph = _RecoveringGraph()
    projector = EntitySummaryProjectorImpl(
        neo4j=graph,
        chunk_writer=legacy_chunk_writer(db, collection="entity-summaries"),
        clock=lambda: _FIXED_TICK,
        commit=db.commit,
    )

    first = projector.tick(per_tick_max_items=1)
    visible_from_other_connection = sqlite3.connect(str(db_path))
    try:
        persisted_after_failed_mark = visible_from_other_connection.execute(
            "SELECT COUNT(*) FROM documents WHERE source_uri = ?",
            ("entity://Q42",),
        ).fetchone()[0]
    finally:
        visible_from_other_connection.close()
    second = projector.tick(per_tick_max_items=1)

    assert first.failed == 1
    assert first.projected == 0
    assert persisted_after_failed_mark == 1
    assert second.projected == 1
    assert graph.marked is True
    assert (
        db.execute(
            "SELECT COUNT(*) FROM documents WHERE source_uri = ?",
            ("entity://Q42",),
        ).fetchone()[0]
        == 1
    )


def test_failed_sqlite_commit_never_marks_graph_indexed(tmp_path: Path) -> None:
    """A storage commit failure stops before the Neo4j marker write."""

    class _Graph:
        def __init__(self) -> None:
            self.mark_calls = 0

        def cypher(self, query: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
            if "SET n.summary_indexed_at" in query:
                self.mark_calls += 1
                return [{"name": "Ada"}]
            return [{**_row(name="Ada", qid="Q42", summary="must commit first"), "prior_summary": ""}]

    db = _seed_db(tmp_path / "commit-failure.sqlite")
    graph = _Graph()
    projector = EntitySummaryProjectorImpl(
        neo4j=graph,
        chunk_writer=legacy_chunk_writer(db, collection="entity-summaries"),
        clock=lambda: _FIXED_TICK,
        commit=lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )

    result = projector.tick(per_tick_max_items=1)

    assert result.failed == 1
    assert result.projected == 0
    assert graph.mark_calls == 0


def test_lifecycle_re_projection_swaps_old_chunk_for_new(tmp_path: Path) -> None:
    # F69-small-scale-only: scale variant in tests/soak/test_entity_summary_projector_at_scale.py (ADR-024)
    """Re-projection: prior_hash is stale → tick deletes prior chunk
    then upserts new. The old FTS5 hit no longer matches the old text;
    the new text is the only searchable row.

    Sabotage-proof: drop the ``delete_by_source_uri`` call in
    ``_process_one`` and the old FTS5 row would survive — the
    'doc MATCH old text' assertion below would still return a hit.
    """
    db = _seed_db(tmp_path / "kairix.db")
    writer = legacy_chunk_writer(db, collection="entity-summaries")

    # First tick — seed the prior chunk via the normal projection path.
    initial_summary = "outdated description"
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary=initial_summary)],
    )
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=writer,
        clock=lambda: _FIXED_TICK,
    )
    projector.tick(per_tick_max_items=10)
    db.commit()

    # Re-tick with the changed summary + the prior hash.
    new_summary = "refreshed description"
    prior_digest = hash_summary(initial_summary)
    neo4j_rerun = FakeGraphRepository(
        cypher_rows=[
            _row(name="Ada", qid="Q42", summary=new_summary, prior_hash=prior_digest),
        ],
    )
    projector_rerun = EntitySummaryProjectorImpl(
        neo4j=neo4j_rerun,
        chunk_writer=writer,
        clock=lambda: "2026-06-09T01:00:00Z",
    )
    result = projector_rerun.tick(per_tick_max_items=10)
    db.commit()

    assert result.updated == 1
    # The FTS5 view of the world is the refreshed text only.
    rows_old = db.execute(
        "SELECT rowid FROM documents_fts WHERE doc MATCH ?",  # F63-bounded: lifecycle scope = one entity
        ("outdated",),
    ).fetchall()
    assert rows_old == []
    rows_new = db.execute(
        "SELECT rowid FROM documents_fts WHERE doc MATCH ?",  # F63-bounded: lifecycle scope = one entity
        ("refreshed",),
    ).fetchall()
    assert len(rows_new) == 1
