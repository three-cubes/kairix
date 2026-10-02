"""Step impls for entity_summary_indexing.feature (ADR-036, #461 Slice C).

Drives the real :func:`run_entity_summary_projector_tick` against a
real ``legacy_chunk_writer`` SQLite + a scripted Neo4j fake, then
queries through the production :func:`build_search_pipeline`. F46-clean:
the composition happens via the production factory, not a stitched
test pipeline.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.connectors.collection_router import legacy_chunk_writer
from kairix.core.db.fts import rebuild_fts
from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema
from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache
from kairix.core.search.config import RetrievalConfig
from kairix.knowledge.entities.summary_projector import (
    EntitySummaryProjectorDeps,
    EntitySummaryProjectorImpl,
    run_entity_summary_projector_tick,
)
from tests.fakes import (
    FakeFeatureFlagResolver,
    FakePaths,
    FakeProvider,
    FakeProviderRegistry,
)

pytestmark = pytest.mark.bdd


_FIXED_TICK = "2026-06-09T00:00:00Z"


class _ScriptedNeo4j:
    """Minimal scripted Neo4j fake for the BDD scenarios.

    Returns the configured pool on each poll; drops entities from the
    pool when a SET ``n.summary_indexed_at`` write lands.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._pool: dict[str, dict[str, Any]] = {r["name"]: r for r in rows}
        self.cypher_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.cypher_calls.append((query, params))
        if "SET n.summary_indexed_at" in query:
            assert params is not None
            removed = self._pool.pop(str(params["name"]), None)
            return [{"name": params["name"]}] if removed is not None else []
        per_tick = int((params or {}).get("per_tick_max_items", 200))
        return list(self._pool.values())[:per_tick]


@dataclass
class _Ctx:
    tmp_path: Path
    flag_on: bool = False
    entities: list[dict[str, Any]] = field(default_factory=list)
    db_path: Path | None = None
    document_root: Path | None = None
    search_result: Any = None


@pytest.fixture
def entity_summary_ctx(tmp_path: Path) -> _Ctx:
    return _Ctx(tmp_path=tmp_path)


def _build_db(ctx: _Ctx) -> None:
    document_root = ctx.tmp_path / "vault"
    document_root.mkdir(exist_ok=True)
    # Seed an unrelated vault doc so the BM25 backend has at least one row.
    (document_root / "unrelated.md").write_text("# Unrelated\nproject delivery notes.\n")
    db_path = ctx.tmp_path / "index.sqlite"
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
    ctx.db_path = db_path
    ctx.document_root = document_root


@given(parsers.parse("an entity '{name}' enriched with description '{description}'"))
def _seed_entity(entity_summary_ctx: _Ctx, name: str, description: str) -> None:
    entity_summary_ctx.entities.append(
        {
            "name": name,
            "qid": "Q42",
            "summary": description,
            "prior_hash": "",
            "summary_source": "wikidata",
        }
    )


@given(parsers.parse("a first-party entity '{name}' described as '{description}'"))
def _seed_first_party_entity(entity_summary_ctx: _Ctx, name: str, description: str) -> None:
    # #467/#429 — a first-party canonical entity carries a summary but NO
    # wikidata_qid; the projector keys its chunk off the name
    # (entity://name/<slug>) so the description still reaches retrieval.
    entity_summary_ctx.entities.append(
        {
            "name": name,
            "qid": "",
            "summary": description,
            "prior_hash": "",
            "summary_source": "",
        }
    )


@given(parsers.parse("the entity-summary-indexing flag is {state}"))
def _set_flag(entity_summary_ctx: _Ctx, state: str) -> None:
    entity_summary_ctx.flag_on = state.strip().lower() == "true"


@given("the worker has run a projector tick")
def _run_tick(entity_summary_ctx: _Ctx) -> None:
    _build_db(entity_summary_ctx)
    assert entity_summary_ctx.db_path is not None

    resolver = (
        FakeFeatureFlagResolver().with_flag("entity_summary_indexing_enabled", True)
        if entity_summary_ctx.flag_on
        else FakeFeatureFlagResolver().with_flag("entity_summary_indexing_enabled", False)
    )
    neo4j = _ScriptedNeo4j(entity_summary_ctx.entities)
    db = sqlite3.connect(str(entity_summary_ctx.db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    writer = legacy_chunk_writer(db, collection="entity-summaries")

    def _factory() -> EntitySummaryProjectorImpl:
        return EntitySummaryProjectorImpl(
            neo4j=neo4j,
            chunk_writer=writer,
            clock=lambda: _FIXED_TICK,
            commit=db.commit,
        )

    deps = EntitySummaryProjectorDeps(
        flag_reader=lambda: resolver.get("entity_summary_indexing_enabled"),
        projector_factory=_factory,
    )
    run_entity_summary_projector_tick(deps)
    # Deterministic BM25 surfacing (#493): rebuild documents_fts from the
    # committed documents+content rows after the tick, exactly as the
    # production self-heal path does (kairix.core.db.fts.rebuild_fts, the
    # `kairix embed --rebuild-fts` surface). The projector's chunk writer
    # already lands a per-chunk documents_fts row, but rebuilding here makes
    # the entity's BM25 row authoritative regardless of FTS5/WAL write-order
    # timing under full-suite load — so the assertion surfaces the entity via
    # the deterministic, per-test-isolated BM25 leg and never hinges on the
    # process-shared vector-index singleton (which resolves against the global
    # cache path, not FakePaths). The chunk writer leaves the connection mid
    # transaction, so rebuild_fts folds its DROP/CREATE/INSERT into the open
    # transaction and the step's commit below makes it atomic.
    rebuild_fts(db)
    db.commit()
    db.close()


@when(parsers.parse("the operator searches for '{query}'"))
def _search(entity_summary_ctx: _Ctx, query: str) -> None:
    assert entity_summary_ctx.db_path is not None
    assert entity_summary_ctx.document_root is not None

    paths = FakePaths(
        document_root=entity_summary_ctx.document_root,
        db_path=entity_summary_ctx.db_path,
        log_dir=entity_summary_ctx.tmp_path / "logs",
        workspace_root=entity_summary_ctx.tmp_path / "workspaces",
    )
    cfg = RetrievalConfig(provider="fake")
    registry = FakeProviderRegistry({"fake": FakeProvider(name="fake", vector=[0.1] * 1536, dim=1536)})
    reset_search_pipeline_cache()
    pipeline = build_search_pipeline(config=cfg, registry=registry, paths=paths)
    entity_summary_ctx.search_result = pipeline.search(query=query, budget=3000)


def _hit_paths(ctx: _Ctx) -> list[str]:
    out: list[str] = []
    for row in ctx.search_result.results:
        inner = getattr(row, "result", None)
        out.append(str(getattr(inner, "path", "") or ""))
    return out


@then(parsers.parse("the results include a chunk with source uri prefix '{prefix}'"))
def _then_includes_prefix(entity_summary_ctx: _Ctx, prefix: str) -> None:
    paths = _hit_paths(entity_summary_ctx)
    assert any(p.startswith(prefix) for p in paths), f"expected a hit with prefix {prefix!r}; got {paths!r}"


@then(parsers.parse("no result has a source uri prefix '{prefix}'"))
def _then_excludes_prefix(entity_summary_ctx: _Ctx, prefix: str) -> None:
    paths = _hit_paths(entity_summary_ctx)
    assert all(not p.startswith(prefix) for p in paths), f"expected no hit with prefix {prefix!r}; got {paths!r}"


def _result_to_search_output(ctx: _Ctx) -> Any:
    """Adapt the raw SearchResult into a SearchOutput so the renderer +
    envelope projection have the canonical input shape ADR-036 Slice D
    pinned for the badge contract."""
    from kairix.use_cases.search import SearchHit, SearchOutput

    raw_results = ctx.search_result.results
    hits = []
    for row in raw_results:
        inner = getattr(row, "result", None)
        hits.append(
            SearchHit(
                path=str(getattr(inner, "path", "") or ""),
                title=str(getattr(inner, "title", "") or ""),
                snippet=str(getattr(inner, "snippet", "") or ""),
                score=float(getattr(inner, "boosted_score", 0.0) or 0.0),
                tier="search",
                tokens=0,
                collection=str(getattr(inner, "collection", "") or ""),
            )
        )
    return SearchOutput(
        query=str(getattr(ctx.search_result, "query", "") or ""),
        intent="semantic",
        results=hits,
        bm25_count=int(getattr(ctx.search_result, "bm25_count", 0) or 0),
        vec_count=int(getattr(ctx.search_result, "vec_count", 0) or 0),
        fused_count=len(hits),
        vec_failed=bool(getattr(ctx.search_result, "vec_failed", False) or False),
        total_tokens=0,
        latency_ms=float(getattr(ctx.search_result, "latency_ms", 0.0) or 0.0),
    )


@then(parsers.parse("the rendered text output contains the badge '{badge}'"))
def _then_rendered_contains_badge(entity_summary_ctx: _Ctx, badge: str) -> None:
    from kairix.core.search.cli import format_text

    rendered = format_text(_result_to_search_output(entity_summary_ctx))
    assert badge in rendered, f"expected badge {badge!r} in CLI output; got:\n{rendered}"


@then(parsers.parse("the result envelope marks the entity row with 'entity_summary' equal to {value}"))
def _then_envelope_marks_entity_row(entity_summary_ctx: _Ctx, value: str) -> None:
    from kairix.use_cases.search import search_output_to_envelope

    envelope = search_output_to_envelope(_result_to_search_output(entity_summary_ctx))
    entity_rows = [row for row in envelope["results"] if str(row.get("path") or "").startswith("entity://")]
    assert entity_rows, f"expected at least one entity row in envelope; got {envelope['results']!r}"
    expected = value.strip().lower() == "true"
    assert entity_rows[0].get("entity_summary") is expected
