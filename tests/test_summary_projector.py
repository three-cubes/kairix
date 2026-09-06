"""Same-module outcome pins for the entity-summary projector runtime."""

from __future__ import annotations

import sqlite3

import pytest

from kairix.knowledge.entities.summary_projector import (
    DefaultProjectorBuilderDeps,
    EntitySummaryProjectorImpl,
    default_projector_builder,
)

pytestmark = pytest.mark.unit


def test_projector_closes_owned_actions_exactly_once() -> None:
    """Repeated shutdown cannot leak or double-close builder-owned resources."""
    closed: list[str] = []
    projector = EntitySummaryProjectorImpl(
        neo4j=object(),
        chunk_writer=object(),
        close_actions=(lambda: closed.append("neo4j"), lambda: closed.append("sqlite")),
    )

    projector.close()
    projector.close()

    assert closed == ["neo4j", "sqlite"]


def test_default_builder_composes_schema_writer_and_owned_database() -> None:
    """The production builder creates the schema and owns its SQLite handle."""
    db = sqlite3.connect(":memory:")
    projector = default_projector_builder(
        DefaultProjectorBuilderDeps(
            db_factory=lambda: db,
            neo4j_factory=object,
        )
    )

    assert isinstance(projector, EntitySummaryProjectorImpl)
    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'documents'").fetchone() == ("documents",)

    projector.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")
