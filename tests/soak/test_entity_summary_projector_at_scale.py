"""F69 scale-bound soak for :class:`EntitySummaryProjectorImpl` (ADR-036).

Seeds 10K pending entities with summaries → asserts the projector
clears the backlog in ≤50 ticks at ``per_tick_max_items=200`` and
ends with exactly 10K chunks in the ``entity-summaries`` collection.

Per `docs/architecture/ADR-024-test-pyramid-redesign.md`, this lives
in `tests/soak/` and runs under `pytest -m soak` nightly via
`.github/workflows/soak-suite.yml` — excluded from per-commit CI.

The test uses a scripted Neo4j fake (no live Neo4j required) and the
real :func:`legacy_chunk_writer` so SQLite + FTS5 lifecycle exercises
the production code path at scale.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kairix.core.connectors.collection_router import legacy_chunk_writer
from kairix.core.db.schema import create_schema
from kairix.knowledge.entities.summary_projector import EntitySummaryProjectorImpl

pytestmark = pytest.mark.soak


_PER_TICK_MAX_ITEMS = 200
_TOTAL_ENTITIES = 10_000


class _ScriptedNeo4jForSoak:
    """Soak-only Neo4j fake that paginates the backlog across ticks.

    Each ``cypher`` call returns up to ``per_tick_max_items`` rows from
    the remaining pool; the projector's poll naturally drains. Mark-
    indexed writes drop the entity from the pool so re-poll behaviour
    matches production (next tick sees the remainder).
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._pool: dict[str, dict[str, Any]] = {r["name"]: r for r in rows}
        self.cypher_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.cypher_calls.append((query, params))
        if "SET n.summary_indexed_at" in query:
            assert params is not None
            removed = self._pool.pop(params["name"], None)
            return [{"name": params["name"]}] if removed is not None else []
        # Poll branch — return up to per_tick_max_items entries.
        per_tick = int((params or {}).get("per_tick_max_items", _PER_TICK_MAX_ITEMS))
        slice_keys = list(self._pool.keys())[:per_tick]
        return [self._pool[k] for k in slice_keys]


def _seed_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    create_schema(db)
    db.commit()
    return db


def test_projector_clears_10k_entity_backlog(tmp_path: Path) -> None:
    """F69 — 10K-entity backlog clears in ≤50 ticks; ends with exactly
    10K chunks in the entity-summaries collection.

    Sabotage-proof: drop the ``self._mark_indexed(...)`` call in the
    projector and the pool would never drain — the test would loop
    until the safety cap fires and the chunk count would mismatch.
    """
    db = _seed_db(tmp_path / "kairix-soak.db")
    writer = legacy_chunk_writer(db, collection="entity-summaries")
    pending = [
        {
            "name": f"entity-{i}",
            "qid": f"Q{1000 + i}",
            "summary": f"description payload {i} for soak scale-bound proof",
            "prior_hash": "",
            "summary_source": "wikidata",
        }
        for i in range(_TOTAL_ENTITIES)
    ]
    neo4j = _ScriptedNeo4jForSoak(pending)
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=writer,
        clock=lambda: "2026-06-09T00:00:00Z",
        commit=db.commit,
    )

    ticks = 0
    cap = 80  # safety cap; expected ~50 ticks (10K / 200 per tick)
    total_projected = 0
    while ticks < cap:
        ticks += 1
        result = projector.tick(per_tick_max_items=_PER_TICK_MAX_ITEMS)
        db.commit()
        total_projected += result.projected
        if result.projected == 0 and result.updated == 0:
            break
    # 10K / 200 = 50 productive ticks, plus one empty drain tick = 51
    assert ticks <= 51, f"backlog should clear in ≤51 ticks; took {ticks}"
    assert total_projected == _TOTAL_ENTITIES

    # F63-bounded: soak verifies the full 10K chunks landed
    rows = db.execute("SELECT COUNT(*) FROM documents WHERE collection = 'entity-summaries'").fetchone()
    assert rows[0] == _TOTAL_ENTITIES
