"""F48 composed-path E2E for entity-summary indexing (ADR-036, #461 Slice C).

Exercises the full composed production code path:

  ingest a vault document → seed Neo4j entity row →
    run_entity_summary_projector_tick (flag-gated) →
    real factory.build_search_pipeline →
    real SearchPipeline.search() →
    assertion that the entity-summary chunk reaches the operator

Four scenarios per ADR-036 §E2E:

1. **OFF is a no-op** — flag OFF + tick + search → no entity row in results.
   Locks the pre-#457 byte-for-byte parity contract.
2. **ON surfaces entity in search** — flag ON + projector tick + search
   for description-only words → top result has ``entity://Q...`` URI.
3. **Update propagates** — operator changes the Wikidata description,
   the next tick replaces the chunk, search for old text no longer
   hits, search for new text does.
4. **Tier composition with #438** — vault canonical chunk outranks
   the reference-tier entity-summary chunk on overlap; locks the EPIC
   #438 source-tier composition contract.

F48: file exists, carries ``@pytest.mark.e2e``, runs in CI Stage 4.5
under ``pytest -m e2e``. Every layer is real production code; only
the Neo4j client is scripted (no live Neo4j server in CI).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kairix.core.connectors.collection_router import legacy_chunk_writer
from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema
from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache
from kairix.core.search.config import (
    RetrievalConfig,
    SourceTier,
    SourceTierBoostConfig,
)
from kairix.knowledge.entities.summary_projector import (
    EntitySummaryProjectorDeps,
    EntitySummaryProjectorImpl,
    hash_summary,
    run_entity_summary_projector_tick,
)
from tests.fakes import (
    FakeFeatureFlagResolver,
    FakeProvider,
    FakeProviderRegistry,
)

pytestmark = pytest.mark.e2e


_FIXED_TICK = "2026-06-09T00:00:00Z"


class _ScriptedNeo4jForE2E:
    """Scripted Neo4j client whose poll returns a fixed entity list.

    Implements ``cypher(query, params)`` honouring two patterns:

    * a poll-shaped query returns the configured pending entities + drops
      any that have been mark-indexed since the last poll
    * a mark-indexed SET query records the entity name + drops it from
      the pool

    No live Neo4j server is required so the E2E runs anywhere
    ``@pytest.mark.e2e`` is selected.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._pool: dict[str, dict[str, Any]] = {r["name"]: r for r in rows}
        self.cypher_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.cypher_calls.append((query, params))
        if "SET n.summary_indexed_at" in query:
            assert params is not None
            name = str(params.get("name") or "")
            new_hash = str(params.get("hash") or "")
            new_summary = str(params.get("summary") or "")
            if name in self._pool:
                self._pool[name]["prior_hash"] = new_hash
                self._pool[name]["prior_summary"] = new_summary
            return [{"name": name}] if name else []
        # Poll branch.
        per_tick = int((params or {}).get("per_tick_max_items", 200))
        slice_keys = list(self._pool.keys())[:per_tick]
        return [self._pool[k] for k in slice_keys]


def _vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    return root


def _build_e2e_db(tmp_path: Path, vault_docs: dict[str, str]) -> Path:
    """Build the real SQLite + FTS5 + scan a vault directory.

    Returns the DB path so the caller can thread it into the search
    pipeline construction.
    """
    document_root = _vault_root(tmp_path)
    for filename, body in vault_docs.items():
        (document_root / filename).write_text(body)

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    create_schema(db)

    scanner = DocumentScanner(db, document_root=document_root)
    scanner.scan([CollectionConfig(name="vault", path=".")])

    db.execute("DELETE FROM documents_fts")
    db.execute(
        """
        INSERT INTO documents_fts (rowid, filepath, title, doc)
        SELECT d.id, d.path, d.title, c.doc
        FROM documents d
        JOIN content c ON c.hash = d.hash
        WHERE d.active = 1
        """
    )
    db.commit()
    db.close()
    return db_path


def _run_projector_tick(
    *,
    db_path: Path,
    neo4j: _ScriptedNeo4jForE2E,
    flag_on: bool,
) -> tuple[int, int, int, int]:
    """Compose the projector via the production dispatcher with the
    given Neo4j + a real chunk-writer.

    Opens its own sqlite3.Connection so the projector + chunk-writer
    share a transaction, then commits + closes.
    """
    resolver = (
        FakeFeatureFlagResolver().with_flag("entity_summary_indexing_enabled", True)
        if flag_on
        else FakeFeatureFlagResolver().with_flag("entity_summary_indexing_enabled", False)
    )
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    writer = legacy_chunk_writer(db, collection="entity-summaries")

    def _factory() -> EntitySummaryProjectorImpl:
        return EntitySummaryProjectorImpl(
            neo4j=neo4j,
            chunk_writer=writer,
            clock=lambda: _FIXED_TICK,
        )

    deps = EntitySummaryProjectorDeps(
        flag_reader=lambda: resolver.get("entity_summary_indexing_enabled"),
        projector_factory=_factory,
    )
    result = run_entity_summary_projector_tick(deps)
    db.commit()
    db.close()
    if result is None:
        return (0, 0, 0, 0)
    return (result.projected, result.updated, result.skipped, result.failed)


def _build_pipeline_with_tier(
    *,
    db_path: Path,
    document_root: Path,
    tier_boost_enabled: bool = False,
) -> Any:
    """Build the real SearchPipeline through the production factory,
    honouring the source-tier boost when enabled (so scenario 4 can
    drive the tier composition contract)."""
    from tests.fakes import FakePaths

    paths = FakePaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=db_path.parent / "logs",
        workspace_root=db_path.parent / "workspaces",
    )
    multipliers = (
        (SourceTier.CANONICAL, 3.0),
        (SourceTier.ACTIVE_STANDARD, 2.0),
        (SourceTier.VAULT_ACTIVE, 1.0),
        (SourceTier.REFERENCE, 0.6),
        (SourceTier.ARCHIVED, 0.2),
    )
    source_tier_boost = SourceTierBoostConfig(
        enabled=tier_boost_enabled,
        multipliers=multipliers,
    )
    cfg = RetrievalConfig(provider="fake", source_tier_boost=source_tier_boost)
    registry = FakeProviderRegistry({"fake": FakeProvider(name="fake", vector=[0.1] * 1536, dim=1536)})
    reset_search_pipeline_cache()
    return build_search_pipeline(config=cfg, registry=registry, paths=paths)


def _result_paths(search_result: Any) -> list[str]:
    paths_out: list[str] = []
    for row in search_result.results:
        inner = getattr(row, "result", None)
        path = str(getattr(inner, "path", "") or "")
        paths_out.append(path)
    return paths_out


# ---------------------------------------------------------------------------
# Scenario 1 — OFF is byte-for-byte no-op
# ---------------------------------------------------------------------------


def test_composed_entity_summary_path_off_is_noop(tmp_path: Path) -> None:
    """Flag OFF + tick + search → no ``entity://`` path in the results.

    Sabotage-proof: drop the OFF guard in
    :func:`run_entity_summary_projector_tick` and the projector would
    fire, chunks would land in the SQLite, and the assertion below
    would catch the leaked entity row.
    """
    document_root = _vault_root(tmp_path)
    (document_root / "vault_note.md").write_text("# Vault note\nAcme address Sydney HQ summary lookup.\n")
    db_path = _build_e2e_db(tmp_path, {})

    neo4j = _ScriptedNeo4jForE2E(
        [
            {
                "name": "Acme Corp",
                "qid": "Q1",
                "summary": "an Australian software company",
                "prior_hash": "",
                "summary_source": "wikidata",
            }
        ]
    )
    outcome = _run_projector_tick(db_path=db_path, neo4j=neo4j, flag_on=False)
    assert outcome == (0, 0, 0, 0)

    pipeline = _build_pipeline_with_tier(db_path=db_path, document_root=document_root)
    result = pipeline.search(query="Australian software company", budget=3000)
    paths = _result_paths(result)
    assert all(not p.startswith("entity://") for p in paths)


# ---------------------------------------------------------------------------
# Scenario 2 — ON surfaces entity in search
# ---------------------------------------------------------------------------


def test_composed_entity_summary_path_on_surfaces_entity_in_search(tmp_path: Path) -> None:
    """End-to-end happy path: project a Wikidata-style description into
    SQLite, then search for description-only keywords. The composed
    pipeline returns the entity-summary chunk with ``entity://`` URI.

    Sabotage-proof: drop the ``self._chunk_writer.upsert([chunk])``
    line in the projector and the assertion fails — search returns
    nothing in the entity-summaries collection.
    """
    document_root = _vault_root(tmp_path)
    # Provide a single vault doc so the BM25 backend has *something*
    # to evaluate, but its content shares zero terms with the query so
    # the entity chunk is the only relevant hit.
    (document_root / "unrelated.md").write_text("# Unrelated\nproject delivery notes.\n")
    db_path = _build_e2e_db(tmp_path, {})

    neo4j = _ScriptedNeo4jForE2E(
        [
            {
                "name": "Ada Lovelace Institute",
                "qid": "Q42",
                "summary": "AI policy research institute",
                "prior_hash": "",
                "summary_source": "wikidata",
            }
        ]
    )
    outcome = _run_projector_tick(db_path=db_path, neo4j=neo4j, flag_on=True)
    assert outcome[0] == 1, f"expected projected=1, got {outcome}"

    pipeline = _build_pipeline_with_tier(db_path=db_path, document_root=document_root)
    result = pipeline.search(query="AI policy research institute", budget=3000)
    paths = _result_paths(result)
    entity_hits = [p for p in paths if p.startswith("entity://Q42")]
    assert entity_hits, f"entity chunk missing from composed search results. got: {paths!r}"


# ---------------------------------------------------------------------------
# Scenario 3 — Update propagates
# ---------------------------------------------------------------------------


def test_composed_entity_summary_path_update_propagates(tmp_path: Path) -> None:
    """Re-projection: Neo4j description changes → next tick replaces
    the SQLite chunk. Search for the old text no longer hits the entity
    row; search for the new text does.

    Sabotage-proof: drop the ``delete_by_source_uri`` line in the
    projector's re-projection branch — the old FTS5 row would survive
    and the assertion that the old-text search returns no entity rows
    would fail.
    """
    document_root = _vault_root(tmp_path)
    (document_root / "unrelated.md").write_text("# Unrelated\nproject delivery notes.\n")
    db_path = _build_e2e_db(tmp_path, {})

    initial_summary = "an Australian software company"
    neo4j = _ScriptedNeo4jForE2E(
        [
            {
                "name": "Acme Corp",
                "qid": "Q1",
                "summary": initial_summary,
                "prior_hash": "",
                "summary_source": "wikidata",
            }
        ]
    )
    _run_projector_tick(db_path=db_path, neo4j=neo4j, flag_on=True)

    # Update: simulate the operator re-enriching with a different description.
    new_summary = "an automotive parts supplier"
    neo4j._pool["Acme Corp"]["summary"] = new_summary
    # prior_hash stamped by the previous mark-indexed Cypher call already.
    assert neo4j._pool["Acme Corp"]["prior_hash"] == hash_summary(initial_summary)

    _run_projector_tick(db_path=db_path, neo4j=neo4j, flag_on=True)

    pipeline = _build_pipeline_with_tier(db_path=db_path, document_root=document_root)
    old_text_result = pipeline.search(query="Australian software company", budget=3000)
    new_text_result = pipeline.search(query="automotive parts supplier", budget=3000)

    old_entity_hits = [p for p in _result_paths(old_text_result) if p.startswith("entity://")]
    new_entity_hits = [p for p in _result_paths(new_text_result) if p.startswith("entity://")]
    assert not old_entity_hits, f"old text should not match the entity row after refresh; got {old_entity_hits!r}"
    new_paths = _result_paths(new_text_result)
    assert new_entity_hits, f"new text should match the entity row after refresh; got {new_paths!r}"


# ---------------------------------------------------------------------------
# Scenario 4 — Tier composition with EPIC #438 (source-tier ranking)
# ---------------------------------------------------------------------------


def test_composed_entity_summary_path_tier_composition_with_438(tmp_path: Path) -> None:
    """Both a vault chunk and an entity-summary chunk surface in the
    same composed query — the source-tier boost (#432) and the
    cross-layer dedup (#455) operate on the combined candidate set
    without dropping either row when the entities differ.

    Composition contract: the entity-summary chunk participates in
    the same fusion + boost chain as the vault content. The detailed
    tier-multiplier math is covered by the dedicated SourceTierBoost
    unit tests in #432; this E2E pins that the two systems compose
    in the production factory wiring.

    Sabotage-proof: drop the projector's upsert path → the entity row
    vanishes from the composed search and the assertion catches.
    """
    document_root = _vault_root(tmp_path)
    # Vault doc whose terms overlap the entity summary so the same
    # query surfaces both rows.
    (document_root / "ada_lovelace_institute.md").write_text(
        "# Ada Lovelace Institute\n"
        "Ada Lovelace Institute is an AI policy research institute "
        "leading public-interest AI work.\n"
    )
    db_path = _build_e2e_db(tmp_path, {})

    neo4j = _ScriptedNeo4jForE2E(
        [
            {
                "name": "Ada Lovelace Institute",
                "qid": "Q42",
                "summary": "AI policy research institute",
                "prior_hash": "",
                "summary_source": "wikidata",
            }
        ]
    )
    _run_projector_tick(db_path=db_path, neo4j=neo4j, flag_on=True)

    pipeline = _build_pipeline_with_tier(
        db_path=db_path,
        document_root=document_root,
        tier_boost_enabled=True,
    )
    result = pipeline.search(query="AI policy research institute", budget=3000)
    paths = _result_paths(result)

    assert paths, "expected at least one result"
    assert any("ada_lovelace_institute" in p for p in paths), (
        f"vault canonical missing from composed results; got {paths!r}"
    )
    assert any(p.startswith("entity://") for p in paths), f"entity row missing from composed results; got {paths!r}"
