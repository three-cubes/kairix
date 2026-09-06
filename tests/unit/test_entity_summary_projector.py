"""Unit tests for kairix.knowledge.entities.summary_projector (ADR-036, #460 Slice B).

These cover the pure helpers + the projector's per-row branching:

* ``hash_summary`` — deterministic SHA-256 digest
* ``build_entity_summary_chunk`` — produces the canonical Chunk shape
* projector outcomes — projected / updated / skipped / failed wired
  through ``EntitySummaryProjectionResult``

F47 lifecycle integration + F68 failure injection live in dedicated
contract + integration files.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.protocols import Chunk
from kairix.knowledge.entities.summary_projector import (
    EntitySummaryProjectorImpl,
    build_entity_summary_chunk,
    hash_summary,
)
from tests.fakes import FakeChunkWriter, FakeGraphRepository

pytestmark = pytest.mark.unit


_FIXED_TICK = "2026-06-09T00:00:00Z"


def _fixed_clock() -> str:
    return _FIXED_TICK


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_hash_summary_is_deterministic() -> None:
    """Same input → same digest. Different inputs → different digests.

    Locks the contract the projector relies on: a re-run with the
    same Neo4j state must produce the same content_hash so the
    embed-cache hits and the prior_hash equality check short-circuits.
    """
    assert hash_summary("apple") == hash_summary("apple")
    assert hash_summary("apple") != hash_summary("apple ")


def test_hash_summary_returns_hex_digest() -> None:
    """The digest is a 64-char lowercase hex string (SHA-256 stable shape)."""
    digest = hash_summary("any input")
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # raises if not hex


def test_build_entity_summary_chunk_carries_canonical_shape() -> None:
    """The built chunk has the ADR-036-declared field values."""
    chunk = build_entity_summary_chunk(
        summary="AI policy research institute",
        qid="Q42",
        name="Ada Lovelace Institute",
        tick_iso=_FIXED_TICK,
        content_hash="abc123",
    )
    assert isinstance(chunk, Chunk)
    assert chunk.text == "AI policy research institute"
    assert chunk.content_hash == "abc123"
    assert chunk.source_name == "wikidata"
    assert chunk.source_uri == "entity://Q42"
    assert chunk.source_modified_at == _FIXED_TICK
    assert chunk.sensitivity == "public"
    assert chunk.chunker_version == "entity-summary:v1"
    assert "entity-summary" in chunk.tags
    assert "qid:Q42" in chunk.tags
    assert chunk.metadata == {"entity_name": "Ada Lovelace Institute", "wikidata_qid": "Q42"}


def test_build_entity_summary_chunk_uses_name_locator_when_qid_absent() -> None:
    """A first-party canonical entity (#467) with no ``wikidata_qid`` keys its
    chunk off the name — locator ``entity://name/<slug>`` + a ``name:`` tag —
    so the summary still indexes (#429)."""
    chunk = build_entity_summary_chunk(
        summary="shared knowledge store",
        qid="",
        name="Three Cubes",
        tick_iso=_FIXED_TICK,
        content_hash="abc123",
    )
    assert chunk.source_uri == "entity://name/three_cubes"
    assert "name:three_cubes" in chunk.tags
    assert chunk.metadata == {"entity_name": "Three Cubes", "wikidata_qid": ""}


def test_entity_summary_source_uri_prefers_qid_falls_back_to_name_slug() -> None:
    """The locator prefers the Wikidata qid and falls back to a name slug
    (#429) — the branch that lets no-qid canon entities index."""
    from kairix.knowledge.entities.summary_projector import entity_summary_source_uri

    assert entity_summary_source_uri(qid="Q42", name="Ada") == "entity://Q42"
    assert entity_summary_source_uri(qid="", name="The Kairix Platform") == "entity://name/the_kairix_platform"


# ---------------------------------------------------------------------------
# Projector outcomes — through the public tick() surface
# ---------------------------------------------------------------------------


def test_projector_first_tick_projects_pending_entity() -> None:
    """Fresh entity (no prior_hash) → projected=1, chunk upserted, Neo4j marked.

    Sabotage-proof: drop the ``self._chunk_writer.upsert([chunk])``
    line in ``_process_one`` and ``writer.writes`` stays empty —
    assertion fails.
    """
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary="AI institute")],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=writer,
        clock=_fixed_clock,
    )
    result = projector.tick(per_tick_max_items=10)
    assert result.projected == 1
    assert result.updated == 0
    assert result.skipped == 0
    assert result.failed == 0
    assert len(writer.writes) == 1
    assert writer.writes[0][0].source_uri == "entity://Q42"
    # Neo4j poll + mark-indexed = 2 cypher calls.
    assert len(neo4j.cypher_calls) == 2


def test_projector_skips_when_prior_hash_matches() -> None:
    """Entity already indexed under the same hash → skipped, no
    writer call, no mark-indexed update.

    Locks the idempotency contract from ADR-036 §Expected behaviours #4."""
    summary = "an unchanged description"
    digest = hash_summary(summary)
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary=summary, prior_hash=digest)],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    result = projector.tick(per_tick_max_items=10)
    assert result.skipped == 1
    assert result.projected == 0
    assert result.updated == 0
    assert writer.writes == []
    assert writer.deletes == []
    # Only the poll happened — no mark-indexed write.
    assert len(neo4j.cypher_calls) == 1


def test_projector_updates_when_summary_hash_changed() -> None:
    """Entity has a stale prior_hash → updated=1, delete-then-upsert
    on the same source_uri, then mark-indexed with the new hash.

    Sabotage-proof: drop the ``self._chunk_writer.delete_by_source_uri(...)``
    line in the re-projection branch and ``writer.deletes`` stays
    empty — the stale row would survive in production.
    """
    neo4j = FakeGraphRepository(
        cypher_rows=[
            _row(name="Ada", qid="Q42", summary="new text", prior_hash="some_old_hash"),
        ],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    result = projector.tick(per_tick_max_items=10)
    assert result.updated == 1
    assert result.projected == 0
    assert writer.deletes == ["entity://Q42"]
    assert len(writer.writes) == 1
    assert writer.writes[0][0].text == "new text"


def test_projector_projects_no_qid_row_via_name_locator() -> None:
    """A first-party canonical entity (#467) has a summary but no
    ``wikidata_qid`` — the projector still indexes it, keyed off the
    entity name (#429), so its summary reaches retrieval.

    Sabotage-proof: restore the ``or not qid`` clause in
    ``_process_one``'s guard and this row is skipped — ``writes`` empties
    and both assertions below fail.
    """
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Kairix", qid="", summary="shared knowledge store")],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    result = projector.tick(per_tick_max_items=10)
    assert result.projected == 1
    assert len(writer.writes) == 1
    chunk = writer.writes[0][0]
    assert chunk.source_uri == "entity://name/kairix"
    assert chunk.text == "shared knowledge store"


def test_projector_skips_row_missing_name_or_summary() -> None:
    """A row with no ``name`` is skipped — the name is load-bearing for
    the locator once ``qid`` is optional (#429). Locks the
    safe-on-bad-data contract."""
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="", qid="Q1", summary="orphaned")],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    result = projector.tick(per_tick_max_items=10)
    assert result.skipped == 1
    assert writer.writes == []


def test_projector_failure_isolation_counts_per_entity_writer_raises(caplog) -> None:
    """ChunkWriter raises mid-tick → that entity's row counts as
    ``failed``, the rest of the rows still process.

    Sabotage-proof: drop the broad ``except Exception`` in
    :meth:`tick` and the second row never gets processed — projected
    drops to 0.
    """
    import logging

    class _PartialWriter:
        def __init__(self) -> None:
            self.upsert_calls = 0
            self.delete_calls: list[str] = []

        def upsert(self, chunks: Any) -> int:
            self.upsert_calls += 1
            if self.upsert_calls == 1:
                raise RuntimeError("simulated write failure on first row")
            return len(list(chunks))

        def delete_by_source_uri(self, source_uri: str) -> int:
            self.delete_calls.append(source_uri)
            return 0

    neo4j = FakeGraphRepository(
        cypher_rows=[
            _row(name="Ada", qid="Q1", summary="first"),
            _row(name="Bob", qid="Q2", summary="second"),
        ],
    )
    writer = _PartialWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    with caplog.at_level(logging.WARNING):
        result = projector.tick(per_tick_max_items=10)

    assert result.failed == 1
    assert result.projected == 1
    assert writer.upsert_calls == 2
    assert any("per-entity tick failed" in r.getMessage() for r in caplog.records)


def test_projector_handles_neo4j_poll_unavailable_returns_idle(caplog) -> None:
    """Neo4j ``cypher`` raises on the poll → projector returns
    all-zero result, never raises. Locks ADR-036 §Expected behaviours
    #6 failure isolation at the poll boundary."""
    import logging

    neo4j = FakeGraphRepository(raises=RuntimeError("simulated neo4j unavailable"))
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    with caplog.at_level(logging.WARNING):
        result = projector.tick(per_tick_max_items=10)

    assert (result.projected, result.updated, result.skipped, result.failed) == (0, 0, 0, 0)
    assert writer.writes == []
    assert any("Neo4j poll failed" in r.getMessage() for r in caplog.records)


def test_projector_respects_per_tick_max_items_param() -> None:
    """The per_tick_max_items kwarg is threaded into the Cypher
    params so Neo4j can cap server-side.

    Sabotage-proof: drop the param from the cypher call and the
    assertion below misses the LIMIT bind value.
    """
    neo4j = FakeGraphRepository(cypher_rows=[])
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer, clock=_fixed_clock)

    projector.tick(per_tick_max_items=42)
    assert neo4j.cypher_calls, "projector should call cypher even on empty result"
    _query, params = neo4j.cypher_calls[0]
    assert params == {"per_tick_max_items": 42}


def test_default_clock_returns_utc_zulu_iso_string() -> None:
    """The production default clock returns an ISO-8601 string ending
    in ``Z`` (UTC). Locks the chunk's ``source_modified_at`` shape so
    downstream temporal boosts get a parseable timestamp.

    Drives the production fallback that tests typically bypass via a
    pinned ``clock`` kwarg.
    """
    from kairix.knowledge.entities.summary_projector import now_iso

    iso = now_iso()
    assert iso.endswith("Z")
    # Round-trip via datetime to verify parseability.
    import datetime as _dt

    _dt.datetime.fromisoformat(iso.rstrip("Z"))


def test_default_clock_used_when_no_clock_kwarg_supplied() -> None:
    """Omitting ``clock`` falls through to :func:`_now_iso` — the
    projector still ticks; chunk's ``source_modified_at`` carries a
    real timestamp.

    Production callers don't pass ``clock``; this test pins the
    fallback wiring."""
    neo4j = FakeGraphRepository(
        cypher_rows=[_row(name="Ada", qid="Q42", summary="AI institute")],
    )
    writer = FakeChunkWriter()
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=writer)
    result = projector.tick(per_tick_max_items=10)
    assert result.projected == 1
    chunk = writer.writes[0][0]
    assert chunk.source_modified_at.endswith("Z")


def test_default_flag_reader_returns_registry_default_false() -> None:
    """The production default flag-reader delegates to the canonical
    feature-flag resolver. The flag's registry default is OFF (per
    ADR-036 §Cutover); calling the helper with no overrides reads
    ``False`` — the cutover-safe default.

    Locks the F52 contract: the call site references the registry
    name, so a rename of the registry key would surface as
    ``KeyError`` here at runtime.
    """
    from kairix.knowledge.entities.summary_projector import default_flag_reader

    assert default_flag_reader() is False
