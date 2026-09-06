"""Entity-summary projector — ADR-036 §Mechanics (Issue #460, Slice B).

Public surface:

  * :class:`EntitySummaryProjectorImpl` — the real projector
    implementation
  * :class:`EntitySummaryProjectorDeps` — F6-clean injection seam
  * :func:`run_entity_summary_projector_tick` — flag-gated tick
    dispatcher the worker loop composes (continuous cadence wiring
    lands alongside the Slice C E2E composed-path)



The :class:`EntitySummaryProjectorImpl` reads pending entities from
Neo4j (``n.summary`` populated, hash mismatched or never indexed),
writes one chunk per entity into the synthetic ``entity-summaries``
collection via the canonical
:class:`~kairix.core.protocols.ChunkWriter` seam, then marks each
entity ``n.summary_indexed_at`` in Neo4j on success.

Failure isolation: per-entity write failures are logged at WARN and
counted via :attr:`EntitySummaryProjectionResult.failed`; the rest of
the tick continues. A Neo4j poll failure produces an idle result
(``projected=0``, ``failed=0``) — the worker boundary decides whether
to surface based on telemetry, not by absorbing the wrong failure.

ADR-036 §Q6 idempotency contract: a re-tick with no Neo4j changes
projects zero new chunks because the hash filter short-circuits each
row. A re-projection (summary text changed) deletes the prior chunk
via :meth:`ChunkWriter.delete_by_source_uri` before upserting, so the
new ``content_hash`` doesn't leave a stale row behind.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kairix.core.protocols import (
    Chunk,
    EntitySummaryProjectionResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cypher statements
# ---------------------------------------------------------------------------

# F63-bounded: LIMIT $per_tick_max_items declared inline. The worker stage
# threads the same per_tick_max_items value declared at construction time
# (F66) so the bound is operator-visible.
_POLL_CYPHER = """
MATCH (n)
WHERE n.summary IS NOT NULL AND n.summary <> ''
RETURN n.name AS name,
       n.wikidata_qid AS qid,
       n.summary AS summary,
       n.summary_indexed_content_hash AS prior_hash,
       n.summary_source AS summary_source
LIMIT $per_tick_max_items
"""

_MARK_INDEXED_CYPHER = """
MATCH (n {name: $name})
SET n.summary_indexed_at = $now,
    n.summary_indexed_content_hash = $hash
RETURN n.name AS name
"""


# ---------------------------------------------------------------------------
# Helpers (public so tests can drive them directly; F5 clean)
# ---------------------------------------------------------------------------


def hash_summary(summary: str) -> str:
    """SHA-256 hex digest of ``summary`` — used as both the chunk's
    ``content_hash`` and Neo4j's ``n.summary_indexed_content_hash``.

    Same string → same digest, so re-running a tick with no changes
    short-circuits via the prior-hash equality check below.
    """
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def entity_name_slug(name: str) -> str:
    """URL-safe slug for an entity name — lowercase, spaces/hyphens → ``_``.

    Matches :func:`kairix.knowledge.entities.canonical._slug_for` so a
    first-party entity's graph node id and its summary-chunk locator stay
    relatable. Public (F5-clean) so tests drive it without a private import.
    """
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def entity_summary_source_uri(*, qid: str, name: str) -> str:
    """Stable ``entity://`` locator for an entity's summary chunk (#429).

    Wikidata-enriched entities key off their ``qid`` (``entity://Q42``).
    First-party canonical entities (#467 — ``Kairix`` and friends) carry a
    summary but no ``wikidata_qid``, so they key off a name-derived slug
    (``entity://name/kairix``). Both keep the well-known ``entity://``
    prefix the routing boost + facts_about + CLI badge recognise, so a
    no-qid summary is indexed and retrievable just like a Wikidata one.
    """
    if qid:
        return f"entity://{qid}"
    return f"entity://name/{entity_name_slug(name)}"


def build_entity_summary_chunk(
    *,
    summary: str,
    qid: str,
    name: str,
    tick_iso: str,
    content_hash: str,
) -> Chunk:
    """Build the canonical :class:`Chunk` for one entity summary.

    F39: ``sensitivity="public"`` is declared via the synthetic
    ``wikidata`` connector-config entry (operator overlay landed
    alongside this slice). The chunker namespace
    ``entity-summary:v1`` is the F55 chunker-version stamp so a
    future re-chunk sweep can filter the affected corpus by stamp.

    ``qid`` may be empty for a first-party canonical entity (#467/#429):
    the locator + secondary tag then key off the entity name via
    :func:`entity_summary_source_uri` so the summary still indexes.
    """
    tag = f"qid:{qid}" if qid else f"name:{entity_name_slug(name)}"
    return Chunk(
        text=summary,
        content_hash=content_hash,
        source_name="wikidata",
        source_uri=entity_summary_source_uri(qid=qid, name=name),
        source_modified_at=tick_iso,
        source_page=None,
        sensitivity="public",
        chunker_version="entity-summary:v1",
        tags=("entity-summary", tag),
        metadata={"entity_name": name, "wikidata_qid": qid},
    )


def now_iso() -> str:
    """Return the current UTC time as a Zulu ISO-8601 string.

    Wrapped in a helper so the projector can override it for tests
    via :class:`EntitySummaryProjectorImpl`'s ``clock`` kwarg.

    Public so tests can drive it directly (F5-clean) — no underscore
    prefix means callers can compose around it.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_now_iso = now_iso  # Backwards-compatible alias for any internal callers.


def noop_projector_action() -> None:
    """Default lifecycle action for directly composed projectors."""


# ---------------------------------------------------------------------------
# Projector implementation
# ---------------------------------------------------------------------------


class EntitySummaryProjectorImpl:
    """Real :class:`EntitySummaryProjector` (ADR-036, Slice B).

    Composes a Neo4j client + a :class:`ChunkWriter` (typically the
    canonical ``_SqliteChunkWriter`` routed via :func:`legacy_chunk_writer`
    at construction time). The worker-tick stage instantiates one of
    these at startup and calls :meth:`tick` once per tick window when
    ``entity_summary_indexing_enabled`` is ON.

    Construction:
      * ``neo4j`` — any object exposing ``cypher(query, params) -> list[dict]``
      * ``chunk_writer`` — a :class:`ChunkWriter` Protocol implementation
      * ``clock`` — optional zero-arg callable returning the tick-start
        ISO string. Default is :func:`_now_iso`; tests inject a fixed
        timestamp so the chunk's ``source_modified_at`` is deterministic.

    Failure isolation: per-entity failures (chunk-write raise, Neo4j
    mark-indexed raise) are logged at WARN and counted; the tick
    never raises.
    """

    def __init__(
        self,
        *,
        neo4j: Any,
        chunk_writer: Any,
        clock: Callable[[], str] = now_iso,
        commit: Callable[[], None] = noop_projector_action,
        close_actions: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self._neo4j = neo4j
        self._chunk_writer = chunk_writer
        self._clock = clock
        self._commit = commit
        self._close_actions = close_actions
        self._closed = False

    def tick(self, *, per_tick_max_items: int = 200) -> EntitySummaryProjectionResult:
        rows = self._fetch_pending(per_tick_max_items)
        if not rows:
            return EntitySummaryProjectionResult()

        tick_iso = str(self._clock())
        projected = updated = skipped = failed = 0
        for row in rows:
            try:
                outcome = self._process_one(row, tick_iso=tick_iso)
            except Exception as exc:
                name = str(row.get("name") or "?")
                logger.warning(
                    "EntitySummaryProjector: per-entity tick failed for %s — %s",
                    name,
                    exc,
                )
                failed += 1
                continue
            if outcome == "projected":
                projected += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "skipped":
                skipped += 1
        result = EntitySummaryProjectionResult(
            projected=projected,
            updated=updated,
            skipped=skipped,
            failed=failed,
        )
        self._commit()
        return result

    def close(self) -> None:
        """Release resources owned by the production builder exactly once."""
        if self._closed:
            return
        self._closed = True
        for action in self._close_actions:
            action()

    def _fetch_pending(self, per_tick_max_items: int) -> list[Mapping[str, Any]]:
        try:
            return list(
                self._neo4j.cypher(
                    _POLL_CYPHER,
                    {"per_tick_max_items": int(per_tick_max_items)},
                )
            )
        except Exception as exc:
            logger.warning("EntitySummaryProjector: Neo4j poll failed — %s", exc)
            return []

    def _process_one(self, row: Mapping[str, Any], *, tick_iso: str) -> str:
        """Project one entity row. Returns the outcome label.

        Outcomes:
          * ``"projected"`` — net-new chunk written + entity marked
            indexed for the first time
          * ``"updated"`` — prior chunk existed for this entity, was
            deleted, and a new chunk written for the changed summary
          * ``"skipped"`` — already indexed under the same hash, or
            malformed row (missing summary / qid / name)
        """
        summary = str(row.get("summary") or "")
        qid = str(row.get("qid") or "")
        name = str(row.get("name") or "")
        prior_hash = str(row.get("prior_hash") or "")

        # #429: ``qid`` is optional — a first-party canonical entity (#467)
        # has a summary but no ``wikidata_qid`` and must still index, keyed
        # off its name. Only summary + name are load-bearing.
        if not summary or not name:
            return "skipped"

        current_hash = hash_summary(summary)
        if prior_hash == current_hash:
            return "skipped"

        source_uri = entity_summary_source_uri(qid=qid, name=name)
        chunk = build_entity_summary_chunk(
            summary=summary,
            qid=qid,
            name=name,
            tick_iso=tick_iso,
            content_hash=current_hash,
        )

        if prior_hash:
            # Re-projection: drop the prior chunk so the new content_hash
            # doesn't leave a stale row behind. Idempotent on the
            # never-projected branch via the prior_hash truthiness check.
            self._chunk_writer.delete_by_source_uri(source_uri)
        self._chunk_writer.upsert([chunk])

        self._mark_indexed(name=name, content_hash=current_hash, tick_iso=tick_iso)
        return "updated" if prior_hash else "projected"

    def _mark_indexed(self, *, name: str, content_hash: str, tick_iso: str) -> None:
        """Stamp ``n.summary_indexed_at`` + ``n.summary_indexed_content_hash``.

        Same try-block discipline as the rest of ``_process_one`` —
        if Neo4j fails here, the surrounding ``except`` in :meth:`tick`
        catches and increments ``failed``. The chunk stays written
        (idempotent next tick via content_hash) but the entity will
        re-project until Neo4j recovers.
        """
        self._neo4j.cypher(
            _MARK_INDEXED_CYPHER,
            {"name": name, "now": tick_iso, "hash": content_hash},
        )


# ---------------------------------------------------------------------------
# Flag-gated tick dispatcher — composed by the worker loop (ADR-036 §Worker)
# ---------------------------------------------------------------------------


def default_flag_reader() -> bool:
    """Default flag-reader for production wiring — reads the canonical
    feature-flag registry value for ``entity_summary_indexing_enabled``.

    Production callers omit the deps and get this; tests pass a
    pinned-bool lambda to drive the OFF / ON branches without
    monkey-patching the resolver. F1/F2/F5 clean — public surface so
    tests can drive it directly without underscore-prefixed imports.
    """
    from kairix.core.features.resolver import flag

    return flag("entity_summary_indexing_enabled")


@dataclass
class EntitySummaryProjectorDeps:
    """F6-clean injection seam for :func:`run_entity_summary_projector_tick`.

    F66-compliant: ``per_tick_max_items`` is declared here so the
    operator-visible cap travels with the deps. The worker stage's
    ``disk_watermark_min_free_bytes`` is shared with every other tick
    stage via the worker-level config (not duplicated per-stage).

    Fields:

      * ``flag_reader`` — returns ``True`` iff
        ``entity_summary_indexing_enabled`` is ON. Defaults to the
        production resolver. Tests pass a lambda returning a
        deterministic bool.
      * ``projector_factory`` — zero-arg builder returning a fully-wired
        :class:`EntitySummaryProjectorImpl`. Production wires Neo4j +
        ``legacy_chunk_writer`` against the live SQLite. Tests pass a
        factory returning a projector built with fakes.
      * ``per_tick_max_items`` — F66 per-tick cap. Default 200 matches
        ADR-036 §Worker. Operators tune in
        ``kairix.config.yaml`` (next-slice wiring).
    """

    flag_reader: Callable[[], bool] = field(default_factory=lambda: default_flag_reader)
    projector_factory: Callable[[], EntitySummaryProjectorImpl] = field(
        default_factory=lambda: default_projector_builder
    )
    per_tick_max_items: int = 200


def default_projector_db_factory() -> Any:
    """Open the canonical production SQLite database."""
    from kairix.core.db import open_db

    return open_db()


def default_projector_neo4j_factory() -> Any:
    """Open the canonical production Neo4j client."""
    from kairix.knowledge.graph.client import Neo4jClient

    return Neo4jClient()


@dataclass(frozen=True)
class DefaultProjectorBuilderDeps:
    """Process-boundary factories used by the production projector builder."""

    db_factory: Callable[[], Any] = field(default_factory=lambda: default_projector_db_factory)
    neo4j_factory: Callable[[], Any] = field(default_factory=lambda: default_projector_neo4j_factory)


def default_projector_builder(
    deps: DefaultProjectorBuilderDeps | None = None,
) -> EntitySummaryProjectorImpl:
    """Compose the live Neo4j reader and SQLite entity-summary writer."""
    from kairix.core.connectors.collection_router import legacy_chunk_writer
    from kairix.core.db.schema import create_schema

    deps = deps if deps is not None else DefaultProjectorBuilderDeps()
    db = deps.db_factory()
    neo4j: Any | None = None
    try:
        create_schema(db)
        neo4j = deps.neo4j_factory()
        writer = legacy_chunk_writer(db, collection="entity-summaries")
        close_actions: list[Callable[[], None]] = []
        neo4j_close = getattr(neo4j, "close", None)
        if callable(neo4j_close):
            close_actions.append(neo4j_close)
        close_actions.append(db.close)
        return EntitySummaryProjectorImpl(
            neo4j=neo4j,
            chunk_writer=writer,
            commit=db.commit,
            close_actions=tuple(close_actions),
        )
    except Exception:
        neo4j_close = getattr(neo4j, "close", None)
        if callable(neo4j_close):
            neo4j_close()
        db.close()
        raise


def run_entity_summary_projector_tick(
    deps: EntitySummaryProjectorDeps | None = None,
) -> EntitySummaryProjectionResult | None:
    """Run one flag-gated entity-summary projector tick.

    Returns the :class:`EntitySummaryProjectionResult` envelope when the
    flag is ON, or ``None`` when the flag is OFF (structural no-op).
    Per ADR-036 §Cutover the OFF branch MUST be a byte-for-byte
    no-op so flipping the flag is reversible.

    The continuous worker-loop dispatch (cadence + state persistence)
    lands alongside the Slice C E2E composed-path PR (#461); this
    function is the operator-visible call point both Slice C's E2E
    and the worker loop will compose.
    """
    deps = deps if deps is not None else EntitySummaryProjectorDeps()
    if not deps.flag_reader():
        return None
    projector = deps.projector_factory()
    try:
        return projector.tick(per_tick_max_items=deps.per_tick_max_items)
    finally:
        close = getattr(projector, "close", None)
        if callable(close):
            close()
