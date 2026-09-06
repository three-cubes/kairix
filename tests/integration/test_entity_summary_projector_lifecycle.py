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
    # Only the poll happened; no mark-indexed.
    assert len(neo4j.cypher_calls) == 1


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
