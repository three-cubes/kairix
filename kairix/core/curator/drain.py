"""GH #334 — drain SQLite ``entity_signals`` staging rows into Neo4j.

Why this exists
---------------
The connector pipeline (Wave 2) stages every extracted
:class:`~kairix.core.protocols.EntitySignal` into the SQLite
``entity_signals`` table via
:class:`kairix.worker._SqliteEntityGraphSink`. The original Wave-2
docstring promised "a separate worker job (Curator-coupling boundary,
Wave 3+) drains the table and pushes to Neo4j" — but no code anywhere
flipped ``pushed_to_neo4j`` from 0 → 1. By 2026-05-27 production held
2,278,272 un-pushed rows (oldest 2022-03-22). Entity-aware retrieval
operated on an empty graph for the entire post-Wave-2 history.

This module is that drain.

Tick contract (see :class:`Neo4jDrainer.tick`)
----------------------------------------------
1. Read up to ``batch_size`` rows from ``entity_signals`` where
   ``pushed_to_neo4j IN (0, -1) AND push_attempt_count < 3``, ordered
   by ``modified_at`` ASC (oldest first — drain age-prioritised so the
   longest-stuck signals push first).
2. Per row:
   * ``kind == "person"`` → MERGE on ``Person {name: $value}``.
   * ``kind == "org"`` → MERGE on ``Organisation {name: $value}``.
   * ``kind == "relationship"`` → skip + increment counter (out of
     scope for this PR; surfaced via ``NeoDrainResult.skipped_relationships``).
3. On success: ``UPDATE entity_signals SET pushed_to_neo4j = 1,
   pushed_at = <iso utc>, push_attempt_count = push_attempt_count + 1,
   last_push_error = NULL WHERE id = ?``.
4. On per-signal failure: ``UPDATE entity_signals SET pushed_to_neo4j = -1,
   push_attempt_count = push_attempt_count + 1, last_push_error = <msg>
   WHERE id = ?`` — and continue with the next row.
5. On connection-level failure (Neo4j unreachable): abort the batch
   without touching any row. Return ``neo4j_available=False, pushed=0``.

Per-signal failures are bounded by ``MAX_PUSH_ATTEMPTS=3``; past that,
the row is skipped on future ticks until an operator clears
``push_attempt_count`` manually (deliberate — protects the drain from
looping forever on a structurally bad row).

F-rule positioning
------------------
* **F66** — :class:`Neo4jDrainer` declares ``per_tick_max_items`` +
  ``disk_watermark_min_free_bytes``. Today F66's scanner doesn't
  include ``kairix/core/curator/`` so the gate doesn't fire here, but
  declaring up-front means adding this tree to F66's scope is a
  zero-edit operation.
* **F62** — the multi-tick idempotency test lives at
  ``tests/integration/test_neo4j_drain_advance.py``. Same caveat:
  F62 scopes to ``kairix/core/connectors`` + ``kairix/core/maintenance``
  today, so the test is forward-armed.
* **F39** — chunks are not constructed here; the EntitySignal frozen
  dataclass carries the metadata.
* **F42** — :class:`NeoDrainResult` is a frozen dataclass.
* **F47** — :func:`kairix.core.factory.build_neo4j_drainer` is the
  sanctioned entry point for integration / BDD tests.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Avoids importing the heavy graph layer at module-import time. The
    # repository is only an interface for typing; tests inject a fake
    # that quacks like Neo4jGraphRepository (the canonical fake lives
    # in tests/fakes.py as FakeNeo4jDrainGraphRepository).
    from kairix.core.curator.protocols import DrainGraphRepository

logger = logging.getLogger("kairix.curator.drain")

# Default per-tick row budget. 500 rows at hourly cadence drains ~12K
# rows/day, which clears the 2.3M historical backlog in ~190 days at
# steady state. Operators with a known good Neo4j can run
# ``kairix curator drain --batch-size 5000 --max-batches 100`` to
# bulk-drain manually; the default protects an unattended worker.
DEFAULT_DRAIN_BATCH_SIZE = 500

# Per-row retry ceiling. A row that fails three times is structurally
# bad (malformed value, MERGE collision, unicode oddity) — better to
# leave it for human triage than to loop forever burning a Neo4j slot
# per tick. Operators can clear ``push_attempt_count`` to retry.
MAX_PUSH_ATTEMPTS = 3

# How often the inner loop commits a batch of UPDATE statements. 50 is
# the same window the connector pipeline uses for chunk writes — keeps
# each commit small enough to not stall the writer for the full batch.
_COMMIT_EVERY_N_ROWS = 50

# Structured-log event names — extracted so the worker / unit tests
# can grep for the exact string (F17).
EVENT_DRAIN_TICK_STARTED = "neo4j_drain_tick_started"
EVENT_DRAIN_TICK_COMPLETED = "neo4j_drain_tick_completed"
EVENT_DRAIN_TICK_NEO4J_UNAVAILABLE = "neo4j_drain_tick_neo4j_unavailable"

# Counter-dict keys — extracted to module constants so the dict-key
# string literal isn't duplicated across the drain loop (F17).
_COUNTER_PUSHED = "pushed"
_COUNTER_FAILED = "failed"
_COUNTER_SKIPPED_RELATIONSHIPS = "skipped_relationships"


@dataclass(frozen=True)
class NeoDrainResult:
    """Frozen envelope returned from one drain tick.

    Fields:
      * ``pushed`` — rows whose ``pushed_to_neo4j`` flipped to 1 this tick.
      * ``failed`` — rows whose MERGE raised; ``pushed_to_neo4j`` set
        to -1, ``last_push_error`` populated, ``push_attempt_count`` bumped.
      * ``skipped_relationships`` — rows with ``kind="relationship"``
        seen and skipped (relationship-shape Cypher is a separate PR).
      * ``neo4j_available`` — ``True`` when the graph backend was
        reachable for this tick. ``False`` means the tick was a no-op
        and the next tick retries.
      * ``elapsed_ms`` — wall-clock duration of the tick, for operator
        latency histograms.

    F42 boundary shape; the worker logs this verbatim on completion and
    the ``kairix curator drain --format json`` envelope embeds it.
    """

    pushed: int
    failed: int
    skipped_relationships: int
    neo4j_available: bool
    elapsed_ms: int


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with Z suffix.

    Matches the convention used by the connector pipeline + integrity
    audit (``...Z`` instead of ``+00:00``) so timestamps round-trip
    consistently across the kairix DB.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _merge_person_cypher() -> str:
    return (
        "MERGE (p:Person {name: $value}) "
        "SET p.last_seen_at = $modified_at, "
        "    p.confidence = $confidence, "
        "    p.source_uri = $source_uri "
        "RETURN p.name AS name"
    )


def _merge_org_cypher() -> str:
    return (
        "MERGE (o:Organisation {name: $value}) "
        "SET o.last_seen_at = $modified_at, "
        "    o.confidence = $confidence, "
        "    o.source_uri = $source_uri "
        "RETURN o.name AS name"
    )


# Map of kind → MERGE query. ``relationship`` is intentionally absent;
# the drain skips and counts it. Future PR will fill in relationship
# extraction once the shape is designed.
_MERGE_CYPHERS: dict[str, str] = {
    "person": _merge_person_cypher(),
    "org": _merge_org_cypher(),
}


def _select_unpushed_rows(
    db: sqlite3.Connection,
    batch_size: int,
) -> list[tuple[int, str, str, str, float, str]]:
    """Read up to ``batch_size`` un-pushed rows oldest-first.

    Filters out rows that have exceeded ``MAX_PUSH_ATTEMPTS`` so the
    drain never re-tries a structurally bad row in a tight loop. The
    LIMIT clause satisfies F63 (no unbounded ``.fetchall()``).
    """
    rows = db.execute(
        "SELECT id, kind, value, source_uri, confidence, modified_at "
        "FROM entity_signals "
        "WHERE pushed_to_neo4j IN (0, -1) AND push_attempt_count < ? "
        "ORDER BY modified_at ASC LIMIT ?",
        (MAX_PUSH_ATTEMPTS, batch_size),
    ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4]), str(r[5])) for r in rows]


def _has_unpushed_rows(db: sqlite3.Connection) -> bool:
    """Whether any ``entity_signals`` row still needs pushing to Neo4j.

    A cheap, indexed existence probe (``LIMIT 1``, F63-bounded) run BEFORE the
    drain builds a Neo4j client, so an idle vault's 10-minute tick doesn't spin
    up a driver (``_connect`` + ``verify_connectivity`` + constraint init) when
    there is demonstrably nothing to drain (PLA-331 idle CPU burn). Uses the
    same pending-row predicate as :func:`_select_unpushed_rows` so the skip
    decision and the batch it would drain never disagree.
    """
    row = db.execute(
        "SELECT 1 FROM entity_signals WHERE pushed_to_neo4j IN (0, -1) AND push_attempt_count < ? LIMIT 1",
        (MAX_PUSH_ATTEMPTS,),
    ).fetchone()
    return row is not None


def _mark_pushed(db: sqlite3.Connection, signal_id: int) -> None:
    """Flip a signal's ``pushed_to_neo4j`` flag to 1 on success."""
    db.execute(
        "UPDATE entity_signals "
        "SET pushed_to_neo4j = 1, pushed_at = ?, "
        "    push_attempt_count = push_attempt_count + 1, "
        "    last_push_error = NULL "
        "WHERE id = ?",
        (_utc_now_iso(), signal_id),
    )


def _mark_failed(db: sqlite3.Connection, signal_id: int, error: str) -> None:
    """Record a per-row failure: flag -1, error text, bumped attempt counter."""
    db.execute(
        "UPDATE entity_signals "
        "SET pushed_to_neo4j = -1, "
        "    push_attempt_count = push_attempt_count + 1, "
        "    last_push_error = ? "
        "WHERE id = ?",
        (error[:500], signal_id),  # truncate to keep the column bounded
    )


def _maybe_commit(db: sqlite3.Connection, processed_in_window: int) -> int:
    """Commit when the per-window threshold has been crossed.

    Returns the new ``processed_in_window`` value (0 after a commit, or
    the unchanged input when no commit fired). Centralised so the
    drain loop has one place to enforce the commit cadence (F17).
    """
    if processed_in_window >= _COMMIT_EVERY_N_ROWS:
        db.commit()
        return 0
    return processed_in_window


def _push_one_row(
    repo: DrainGraphRepository,
    row: tuple[int, str, str, str, float, str],
) -> tuple[bool, str]:
    """Attempt to MERGE one entity row into Neo4j.

    Returns ``(success, error_message)``. ``error_message`` is empty
    on success. Catches every exception class because the underlying
    Neo4j driver raises a wide variety of typed errors (transient,
    constraint, syntax) and the drain treats them all as "this row
    failed, mark it, continue with the next".
    """
    _signal_id, kind, value, source_uri, confidence, modified_at = row
    cypher = _MERGE_CYPHERS.get(kind)
    if cypher is None:
        # Should not be reachable — caller filters relationship out
        # before calling _push_one_row. Defensive log only.
        return (False, f"unknown kind: {kind!r}")
    try:
        repo.cypher(
            cypher,
            {
                "value": value,
                "modified_at": modified_at,
                "confidence": float(confidence),
                "source_uri": source_uri,
            },
        )
        return (True, "")
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def run_neo4j_drain_tick(
    db: sqlite3.Connection,
    repo: DrainGraphRepository,
    *,
    batch_size: int = DEFAULT_DRAIN_BATCH_SIZE,
) -> NeoDrainResult:
    """Drain up to ``batch_size`` ``entity_signals`` rows into Neo4j.

    The free-function shape is the one operators reach via the
    ``kairix curator drain`` CLI; :class:`Neo4jDrainer` wraps it in a
    class for the worker-loop tick contract. Both paths share this
    implementation so a tick and a CLI invocation drain the same way.

    Contract:
      * Connection failure (``repo.available == False``) → no row is
        touched; ``NeoDrainResult(neo4j_available=False, pushed=0)``.
      * ``kind="relationship"`` rows → skipped; counter bumped on the
        row so it isn't re-selected; ``skipped_relationships``
        incremented in the envelope.
      * Per-signal MERGE failure → row marked ``pushed_to_neo4j = -1``
        with ``last_push_error`` set; ``failed`` counter incremented.
        The drain continues with the next row.
      * Success → row marked ``pushed_to_neo4j = 1`` with ``pushed_at``
        set; ``pushed`` counter incremented.

    The function commits every ``_COMMIT_EVERY_N_ROWS`` (50) rows so
    a crash mid-batch loses at most ~50 row-acks rather than the
    whole tick. A final commit lands at the end.
    """
    started_at = time.monotonic()
    logger.info("event=%s batch_size=%d", EVENT_DRAIN_TICK_STARTED, batch_size)

    if not repo.available:
        logger.warning("event=%s", EVENT_DRAIN_TICK_NEO4J_UNAVAILABLE)
        return NeoDrainResult(
            pushed=0,
            failed=0,
            skipped_relationships=0,
            neo4j_available=False,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )

    rows = _select_unpushed_rows(db, batch_size)
    counters = _drain_rows(db, repo, rows)
    db.commit()

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    result = NeoDrainResult(
        pushed=counters[_COUNTER_PUSHED],
        failed=counters[_COUNTER_FAILED],
        skipped_relationships=counters[_COUNTER_SKIPPED_RELATIONSHIPS],
        neo4j_available=True,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "event=%s pushed=%d failed=%d skipped_relationships=%d elapsed_ms=%d",
        EVENT_DRAIN_TICK_COMPLETED,
        result.pushed,
        result.failed,
        result.skipped_relationships,
        result.elapsed_ms,
    )
    return result


def _drain_rows(
    db: sqlite3.Connection,
    repo: DrainGraphRepository,
    rows: Sequence[tuple[int, str, str, str, float, str]],
) -> dict[str, int]:
    """Apply the per-row dispatch — pushed / failed / skipped.

    Extracted to a helper so :func:`run_neo4j_drain_tick` stays under
    the F16 cognitive-complexity ceiling. Returns a counters dict
    that the caller folds into the result envelope.
    """
    counters = {_COUNTER_PUSHED: 0, _COUNTER_FAILED: 0, _COUNTER_SKIPPED_RELATIONSHIPS: 0}
    processed_in_window = 0
    for row in rows:
        signal_id, kind, _value, _source_uri, _confidence, _modified_at = row
        if kind == "relationship":
            # Bump the attempt counter so the row drops out of the
            # selection window on the next tick (otherwise we'd
            # re-read it on every drain). The relationship row stays
            # at pushed_to_neo4j=0 — a future PR that ships
            # relationship MERGE flips it then.
            db.execute(
                "UPDATE entity_signals SET push_attempt_count = push_attempt_count + 1 WHERE id = ?",
                (signal_id,),
            )
            counters[_COUNTER_SKIPPED_RELATIONSHIPS] += 1
            processed_in_window += 1
            processed_in_window = _maybe_commit(db, processed_in_window)
            continue

        success, error = _push_one_row(repo, row)
        if success:
            _mark_pushed(db, signal_id)
            counters[_COUNTER_PUSHED] += 1
        else:
            _mark_failed(db, signal_id, error)
            counters[_COUNTER_FAILED] += 1
        processed_in_window += 1
        processed_in_window = _maybe_commit(db, processed_in_window)
    return counters


# F66-watermark-exempt: drain reads SQLite + pushes to Neo4j; no on-disk blob writes.
class Neo4jDrainer:
    """Tick-driven drain of ``entity_signals`` → Neo4j.

    Construct once per worker process; call :meth:`tick` from the
    worker loop. Class-level ``per_tick_max_items`` declares the per-
    tick row budget (F66); the ``tick`` method delegates to
    :func:`run_neo4j_drain_tick` so a one-shot operator drain via the
    CLI shares the same code path as the unattended worker loop.

    The constructor accepts ``db`` and ``repo`` as positional kwargs
    so test composition is explicit: tests pass a sqlite ``:memory:``
    + a fake graph repo; production wires the worker DB + the real
    :class:`kairix.knowledge.graph.repository.Neo4jGraphRepository`.
    """

    # F66 — declare the per-tick budget at the class level. Today F66's
    # scanner doesn't include kairix/core/curator/, but the declaration
    # is forward-armed and serves as readable documentation of the cap.
    per_tick_max_items: int = DEFAULT_DRAIN_BATCH_SIZE
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        db: sqlite3.Connection,
        repo: DrainGraphRepository,
        *,
        batch_size: int = DEFAULT_DRAIN_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(
                f"batch_size must be > 0; got {batch_size!r}. "
                "fix: pass a positive int (default 500); "
                "run: kairix curator drain --batch-size 500"
            )
        self._db = db
        self._repo = repo
        self._batch_size = batch_size

    @property
    def batch_size(self) -> int:
        """Read-only view of the configured per-tick batch size."""
        return self._batch_size

    def tick(self) -> NeoDrainResult:
        """Run one drain tick. Delegates to :func:`run_neo4j_drain_tick`.

        Multi-tick contract (F62 spirit): when no un-pushed rows
        remain, tick 2 returns ``pushed=0, failed=0, skipped_relationships=0``
        — i.e. zero work. The matching integration test lives at
        ``tests/integration/test_neo4j_drain_advance.py``.
        """
        return run_neo4j_drain_tick(self._db, self._repo, batch_size=self._batch_size)


def _make_drainer(db: sqlite3.Connection, repo: Any, *, batch_size: int = DEFAULT_DRAIN_BATCH_SIZE) -> Neo4jDrainer:
    """Internal seam used by :func:`kairix.core.factory.build_neo4j_drainer`.

    Splitting the construction out keeps the factory call-site one
    line and avoids the factory importing the drainer class twice
    (once for typing, once for ``isinstance`` checks downstream).
    """
    return Neo4jDrainer(db, repo, batch_size=batch_size)


def _default_get_client() -> Any:
    """Production-default Neo4j client factory.

    Late-import to keep module-import latency low — the graph layer
    only pays its cost when the drain actually fires.
    """
    from kairix.knowledge.graph.client import get_client

    return get_client()


def default_open_db() -> sqlite3.Connection:
    """Production-default SQLite connection factory."""
    from kairix.paths import db_path

    # F77-allow: default-factory for worker-tick neo4j-drain; called by orchestrator at startup.
    return sqlite3.connect(str(db_path()))


def _default_make_repo(client: Any) -> Any:
    """Production-default wrap of a Neo4j client into a DrainGraphRepository.

    ``raise_on_error=True``: a rejected MERGE must raise so the row is marked
    failed, not acknowledged as pushed (the read-path client swallows errors).
    """
    from kairix.knowledge.graph.repository import Neo4jGraphRepository

    return Neo4jGraphRepository(client, raise_on_error=True)


@dataclass
class Neo4jDrainTickDeps:
    """Injectable seam for :func:`run_default_drain_tick`.

    Canonical kairix Deps shape (F6-exempt — fields live on a ClassDef
    per CLAUDE.md's Deps-pattern rule). Production callers leave
    ``deps`` as ``None`` and the function binds these defaults; unit
    tests pass a Deps with their own factories so the orchestration
    composition (client → availability gate → repo → DB → tick) can
    be exercised without a real Neo4j endpoint.
    """

    client_factory: Any = field(default=_default_get_client)
    db_factory: Any = field(default=default_open_db)
    repo_factory: Any = field(default=_default_make_repo)


def run_default_drain_tick(deps: Neo4jDrainTickDeps | None = None) -> NeoDrainResult:
    """Production-default drain tick — open Neo4j client + SQLite + run.

    Composition shape:
      1. Open the SQLite connection via ``deps.db_factory()``
      2. Idle-skip: if no rows are pending, return
         ``NeoDrainResult(neo4j_available=True, pushed=0, ...)`` WITHOUT
         building a Neo4j client — an idle vault must not spin up a driver
         (connect + verify_connectivity + constraint init) every tick
      3. Build the live Neo4j client via ``deps.client_factory()``
      4. Early-return ``NeoDrainResult(neo4j_available=False, ...)`` if
         the backend is unreachable (the worker logs a single warning;
         the next tick retries)
      5. Wrap the client into a DrainGraphRepository via
         ``deps.repo_factory(client)`` and delegate to
         :func:`run_neo4j_drain_tick` for the row-level work
      6. Close the SQLite connection in ``finally`` regardless of outcome

    Extracted from :func:`kairix.worker._default_neo4j_drain` so the
    composition is owned by the drain module rather than worker.py.
    Keeps worker.py a thin dispatcher and lets unit tests cover the
    full orchestration branches by injecting ``Neo4jDrainTickDeps``
    with stand-in factories. SQLite read failures propagate; the
    worker's ``(Exception, SystemExit)`` discipline at the dispatch
    site keeps the loop alive.
    """
    deps = deps or Neo4jDrainTickDeps()
    db = deps.db_factory()
    try:
        # Idle-skip: open the (cheap) SQLite connection first and short-circuit
        # when nothing is queued, BEFORE building the Neo4j client. The drain
        # tick is exempt from the worker's idle no-op gate so it always makes
        # progress on a backlog — but on an idle vault that meant rebuilding a
        # driver (connect + verify_connectivity + constraint init) every tick
        # for zero rows (PLA-331 idle CPU burn). Report neo4j_available=True:
        # there is demonstrably nothing to drain, and the separate health tick
        # + the ENTITY-intent query gate still surface a real outage when it
        # actually matters.
        if not _has_unpushed_rows(db):
            return NeoDrainResult(
                pushed=0,
                failed=0,
                skipped_relationships=0,
                neo4j_available=True,
                elapsed_ms=0,
            )

        client = deps.client_factory()
        if not client.available:
            return NeoDrainResult(
                pushed=0,
                failed=0,
                skipped_relationships=0,
                neo4j_available=False,
                elapsed_ms=0,
            )

        repo = deps.repo_factory(client)
        return run_neo4j_drain_tick(db, repo)
    finally:
        db.close()
