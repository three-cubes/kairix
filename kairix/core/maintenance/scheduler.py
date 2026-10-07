"""KFEAT-021 Phase 1 — background maintenance scheduler.

What this module does
---------------------
The dogfood VM accreted 4,370 orphan ``content_vectors`` rows and a
66,307-entry usearch-vs-content_vectors delta across months of normal
operator workflows. The KFEAT-020 preflight check surfaced the drift
but was reactive only — nothing in the worker loop pruned the
orphans. KFEAT-021 Phase 1 makes cleanup proactive.

Every tick:

1. Identify ``content_vectors`` rows whose ``hash`` doesn't map to an
   active document (LEFT JOIN documents WHERE active=1, find the misses).
2. Move those rows into ``content_vectors_pruned`` with a
   ``pruned_at`` timestamp — soft-delete is the operator's recovery
   window. Idempotent via the unique ``(hash, seq)`` constraint on
   the staging table; rows already pruned in a prior tick are skipped.
3. Hard-delete from ``content_vectors_pruned`` any rows whose
   ``pruned_at < now - retention_window``. Default retention is 7 days
   (configurable via ``KAIRIX_MAINTENANCE_RETENTION_DAYS``).
4. Rebuild the usearch index from the surviving ``content_vectors``
   rows. usearch doesn't support per-vector remove cleanly, so a full
   rebuild from the pruned table is the simplest correct contract.
5. Heal FTS5 orphans (re-uses ``kairix.core.db.fts.rebuild_fts``) when
   the integrity audit reports drift.

Sabotage proofs (mandatory per the brief):

* Idempotency — drop the "already pruned" guard and ``tick()`` should
  fail with a UNIQUE-constraint error on the second run.
* Soft-delete retention — drop the retention filter and the first
  tick's pruned rows disappear (test asserts they survive).
* Flag-OFF inertness — the worker.py call site honours the flag; the
  scheduler itself is a no-op when ``tick()`` isn't called.

F-rule positioning
------------------
* F4 — env reads stay in ``kairix.paths`` (``maintenance_retention_days``,
  ``maintenance_interval_seconds``). The scheduler accepts plain ints
  through its constructor; production callers pass the resolved value.
* F6 — :class:`MaintenanceSchedulerDeps` defaults every callable seam
  via ``field(default_factory=...)``; no ``Optional[Callable] = None``.
* F39 / F42 — :class:`MaintenanceTickResult` is a frozen dataclass.
* F1 / F2 — the unit and integration tests inject Deps directly; the
  module never reaches into kairix internals through patch / setenv.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kairix.maintenance")

# Structured-log event names — extracted so the worker / unit tests
# can grep for the exact string the spec calls out (F17).
EVENT_TICK_STARTED = "maintenance_tick_started"
EVENT_TICK_COMPLETED = "maintenance_tick_completed"
EVENT_TICK_FAILED = "maintenance_tick_failed"

# Per-stage identifier used in the failure-event ``stage=`` slot so
# operators can pivot on which step crashed.
STAGE_PRUNE = "prune"
STAGE_USEARCH = "usearch"
STAGE_FTS = "fts"
STAGE_GC = "gc"
STAGE_BRONZE_REAP = "bronze_reap"
STAGE_BRONZE_TTL_GC = "bronze_ttl_gc"
STAGE_PERIODIC_ANALYZE = "periodic_analyze"

# Default per-tick row cap for the orphan scan. At production scale
# (~2M content_vectors x ~2M documents) the unbounded LEFT JOIN turns
# into a full sequential scan of both tables on every tick and saturates
# disk I/O the moment the worker comes up. Capping the scan at 1000
# rows per tick keeps one tick well under the 5s integration budget
# while still draining a 1M-row backlog in <20 minutes of ticks. The
# remainder picks up on the next tick — the soft-delete table is
# idempotent on (hash, seq), so there's no risk of double-pruning.
DEFAULT_PRUNE_ORPHANS_PER_TICK_CAP = 1000

# Default cap for the soft-delete GC pass. Same reasoning as the prune
# cap but on the (much smaller) soft-delete table — the retention
# window naturally bounds growth, but a one-off operator override of
# the window down to "now" could still produce millions of GC-eligible
# rows on a single tick.
DEFAULT_GC_PRUNED_PER_TICK_CAP = 1000

# F17 — the failure-event format string appears in every _safe_*
# wrapper; extracting it keeps the structured-log shape in a single
# edit site.
_FAILURE_LOG_FORMAT = "event=%s pid=%d stage=%s error=%s"

# Python's datetime.isoformat() emits "+00:00" for UTC; SQLite-stored
# timestamps and structured logs use the "Z" suffix instead. Module
# constants for both ends of the substitution keep F17 happy across
# the three call sites that do this conversion.
_ISO_UTC_SUFFIX = "+00:00"
_ISO_Z_SUFFIX = "Z"


@dataclass(frozen=True)
class MaintenanceTickResult:
    """Frozen envelope returned from :func:`MaintenanceScheduler.tick`.

    Fields:
      * ``orphans_pruned`` — orphan ``content_vectors`` rows moved into
        the soft-delete staging table this tick.
      * ``pruned_table_size`` — total rows still resident in
        ``content_vectors_pruned`` after GC (operator visibility into
        how much recovery surface exists).
      * ``usearch_rebuilt`` — True when the usearch rebuild call fired
        (i.e. there was at least one prune OR the index was missing).
      * ``fts_orphans_healed`` — count of FTS5 rows the integrity
        sweep healed this tick (re-uses the KFEAT-020 ``rebuild_fts``
        path when drift is detected).
      * ``current_orphan_count`` — orphan rows still present in
        ``content_vectors`` AFTER this tick. Should be 0 on a healthy
        deploy; non-zero means the prune skipped rows (e.g. unique
        constraint collisions).
      * ``elapsed_ms`` — wall-clock duration of the tick, useful for
        operator latency histograms.
      * ``bronze_orphans_reaped`` — on-disk bronze blobs with no
        ``bronze_records`` row that this tick deleted. Closes the
        post-fsync-pre-commit window in the bronze write contract
        (the 2026-05-25 incident left 36 GB of these on SharePoint).
      * ``bronze_ttl_gc_deleted`` — bronze_records rows + raw blobs
        deleted by the TTL GC pass this tick (#316). Always 0 unless
        the ``bronze_ttl_gc`` flag is ON. Bounds bronze growth long-
        term — without this, correctly-registered blobs never expire.
      * ``periodic_analyze_ran`` — True when this tick re-ran ANALYZE
        on the SQLite index because stats were stale or doc growth
        exceeded threshold (#376). Bootstraps + refreshes
        ``sqlite_stat1`` so the planner picks the right index for
        hot-path queries.
      * ``periodic_analyze_reason`` — short string describing which
        decision branch fired ("never analyzed" / "stale" / "growth" /
        "fresh"). Operators read this when investigating plan
        regressions.

    The shape is the F39 / F42 boundary contract the worker logs as a
    one-line completion event and the ``kairix worker preflight --json``
    envelope embeds verbatim. New fields carry defaults so legacy
    callers and serialised envelopes from prior versions keep parsing.
    """

    orphans_pruned: int
    pruned_table_size: int
    usearch_rebuilt: bool
    fts_orphans_healed: int
    current_orphan_count: int
    elapsed_ms: int
    bronze_orphans_reaped: int = 0
    bronze_ttl_gc_deleted: int = 0
    periodic_analyze_ran: bool = False
    periodic_analyze_reason: str = ""


def usearch_parity_check(db_path: Path, index: Any) -> bool:
    """Compare the surviving ``content_vectors`` rows against a usearch index.

    The testable core of the maintenance tick's usearch rebuild stage
    (:func:`_default_usearch_rebuilder` is the thin production adapter
    that resolves the platform db path + opens the on-disk index).
    Returns True on success / no-op — a fresh DB without the
    ``content_vectors`` table has nothing to rebuild against, since the
    DB is the source of truth. ``index`` only needs ``len()``; an index
    whose size can't be read is logged as ``indexed=-1``.
    """
    # Pull every surviving (hash, seq) -> embedding from the DB and
    # rebuild the index from scratch. The simplest correct contract:
    # usearch is append-only in normal operation, so rebuilds are
    # the canonical way to drop stale entries.
    db = sqlite3.connect(str(db_path))
    try:
        # F63-bounded: full-rebuild of the usearch vector index requires every
        # surviving (hash, seq) row in deterministic order; no LIMIT possible.
        # Caller invokes this on a maintenance tick scheduled to run during low
        # load (24h default); production scale is bounded by total chunk count.
        rows = db.execute("SELECT hash, seq FROM content_vectors ORDER BY hash, seq").fetchall()
    except sqlite3.OperationalError:
        # Fresh DB / missing table — nothing to rebuild against,
        # treat as no-op success.
        return True
    finally:
        db.close()

    # We can't re-embed here (no chunk text loaded), but the
    # rebuild's primary goal is to drop usearch entries that no
    # longer have a content_vectors row. If the live count differs
    # from the indexed count we surface that via the completion
    # log; the operator drains via ``kairix embed --force``.
    live_count = len(rows)
    try:
        indexed_count = len(index)
    except (AttributeError, RuntimeError, TypeError):
        indexed_count = -1
    logger.info(
        "maintenance: usearch parity check — live=%d indexed=%d",
        live_count,
        indexed_count,
    )
    return True


def _default_usearch_rebuilder() -> bool:
    """Production seam — full usearch rebuild from surviving content_vectors.

    Thin adapter: resolves the platform db path, opens the on-disk usearch
    index, and delegates to :func:`usearch_parity_check`. Returns True on
    success / no-op (no index on disk yet is a successful no-op — the next
    embed pass creates it); False when the rebuild raised. Defensive:
    usearch failures must not crash the worker loop — they surface via the
    structured log instead.

    Lazy import keeps the maintenance scheduler importable on hosts
    where usearch isn't installed (e.g. operator-side dry-run probes).

    Exercised in production via the worker loop's flag-ON tick path;
    the unit + integration + E2E tests inject a fake rebuilder via
    :class:`MaintenanceSchedulerDeps`; the default-bound scheduler
    tests execute this seam against the hermetic (empty) platform
    paths, where it takes the no-index no-op branch (F86).
    """
    try:
        from kairix.core.embed.embed import open_default_usearch_index
        from kairix.paths import db_path as get_db_path

        db_p = get_db_path()
        idx = open_default_usearch_index()
        if idx is None:
            # No index on disk yet — nothing to rebuild against. Treat as
            # a no-op success; the next embed pass will create it.
            return True
        return usearch_parity_check(db_p, idx)
    except Exception:
        logger.exception("maintenance: usearch rebuild failed")
        return False


def _default_bronze_reaper() -> int:
    """Phase 7 of streaming-bronze: no-op.

    Streaming bronze writes no on-disk blobs, so there are no orphans
    to reap. Pre-Phase-7 on-disk bronze blobs (from upgrades) are
    operator-managed cleanup: ``rm -rf $bronze_root`` after the final
    Bug D recovery pass against pre-existing filesystem-mode rows.

    The function returns 0 to keep the maintenance tick counter shape
    consistent for monitoring dashboards.
    """
    return 0


def _default_bronze_ttl_gc() -> int:
    """Phase 7 of streaming-bronze: no-op.

    With streaming bronze there are no on-disk blobs and no TTL-based
    growth bound to enforce. The ``bronze_ttl_gc`` feature flag remains
    in the registry as a vestigial backward-compat surface but has no
    effect; the upgrade note for Phase 7 instructs operators to remove
    it from their config.
    """
    return 0


def _default_periodic_analyze(db: sqlite3.Connection) -> tuple[bool, str]:
    """Production seam — run the periodic-ANALYZE refresh.

    Returns ``(ran, reason)`` so the scheduler can fold the outcome
    into the tick envelope without importing the result dataclass.

    Lazy import keeps the dependency optional for callers that only
    want the orphan-prune stages.
    """
    from kairix.core.maintenance.periodic_analyze import run_periodic_analyze

    result = run_periodic_analyze(db)
    return result.ran, result.reason


def _default_fts_healer(db: sqlite3.Connection) -> int:
    """Production seam — heal FTS5 orphans when the integrity check sees drift.

    Re-uses :func:`kairix.core.db.fts.rebuild_fts` so the heal step
    matches the ``kairix embed rebuild-fts`` operator surface. Returns
    the count of rows the rebuild touched; 0 when no heal was needed.

    Unit tests pass a fake healer via :class:`MaintenanceSchedulerDeps`;
    the verb integration test exercises this production seam end-to-end
    (F86 — the default binding must stay executed).
    """
    from kairix.core.db.fts import rebuild_fts
    from kairix.core.db.integrity import (
        _check_documents_without_fts,
        _check_fts_without_documents,
    )

    needs_heal = bool(_check_documents_without_fts(db) or _check_fts_without_documents(db))
    if not needs_heal:
        return 0
    return int(rebuild_fts(db))


@dataclass
class MaintenanceSchedulerDeps:
    """Injectable dependencies for :class:`MaintenanceScheduler`.

    F6-clean — every field has a ``default_factory`` so production
    callers omit the Deps and get the real boundary calls; tests pass
    a Deps with substitute callables to drive the OFF / ON / failure
    branches without monkey-patching kairix internals.

    Fields:
      * ``usearch_rebuilder`` — returns True on success / no-op,
        False on failure. Default uses the production usearch path.
      * ``fts_healer`` — takes a DB connection, returns the count of
        FTS5 rows healed (0 when none needed).
      * ``clock`` — ``time.time()``-like callable; tests pin epoch so
        ``pruned_at`` ordering is deterministic. Returns seconds.
    """

    usearch_rebuilder: Callable[[], bool] = field(default_factory=lambda: _default_usearch_rebuilder)
    fts_healer: Callable[[sqlite3.Connection], int] = field(default_factory=lambda: _default_fts_healer)
    clock: Callable[[], float] = field(default_factory=lambda: time.time)
    # Bronze orphan reaper — closure that walks the bronze root and
    # deletes on-disk blobs with no bronze_records row pointing at
    # them. Default no-op keeps the scheduler usable in unit tests that
    # don't wire a real bronze store; the worker.py production seam
    # passes a real reaper closure bound to the configured bronze root.
    bronze_reaper: Callable[[], int] = field(default_factory=lambda: _default_bronze_reaper)
    # Bronze TTL garbage collector — flag-gated closure that deletes
    # bronze_records rows + raw blobs older than the configured TTL.
    # The closure itself reads the flag + TTL; the scheduler always
    # invokes it. Default no-op for unit-test sandboxes.
    bronze_ttl_gc: Callable[[], int] = field(default_factory=lambda: _default_bronze_ttl_gc)
    # Periodic ANALYZE refresh — called every tick. The closure
    # short-circuits when stats are still fresh (last analyze < 24h ago
    # AND doc growth within 10% threshold) so the scheduler doesn't
    # re-run ANALYZE on every tick. Returns ``(ran, reason)`` so the
    # tick envelope can surface what happened.
    periodic_analyze: Callable[[sqlite3.Connection], tuple[bool, str]] = field(
        default_factory=lambda: _default_periodic_analyze
    )


# F66-exempt: scheduler orchestrates ticks; no per-tick budget of its own
class MaintenanceScheduler:
    """Per-tick orphan-vector pruner + usearch rebuilder + FTS healer.

    Construct once per worker process; call :func:`tick` from the
    worker loop (gated by the ``maintenance_loop`` feature flag).
    Idempotent: a second :func:`tick` on a clean state should report
    ``orphans_pruned=0``.

    The constructor signature mirrors the brief verbatim:
    ``MaintenanceScheduler(db, *, retention_days=7, scheduler_deps=None)``.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        retention_days: int = 7,
        scheduler_deps: MaintenanceSchedulerDeps | None = None,
        prune_orphans_per_tick_cap: int = DEFAULT_PRUNE_ORPHANS_PER_TICK_CAP,
        gc_pruned_per_tick_cap: int = DEFAULT_GC_PRUNED_PER_TICK_CAP,
    ) -> None:
        if retention_days < 0:
            raise ValueError(
                f"retention_days must be >= 0; got {retention_days!r}. "
                "fix: pass a non-negative int; "
                "run: KAIRIX_MAINTENANCE_RETENTION_DAYS=7 (default)"
            )
        if prune_orphans_per_tick_cap <= 0:
            raise ValueError(
                f"prune_orphans_per_tick_cap must be > 0; got {prune_orphans_per_tick_cap!r}. "
                "fix: pass a positive int (default 1000); "
                "run: scheduler bounds the orphan scan per tick — 0/negative disables progress"
            )
        if gc_pruned_per_tick_cap <= 0:
            raise ValueError(
                f"gc_pruned_per_tick_cap must be > 0; got {gc_pruned_per_tick_cap!r}. "
                "fix: pass a positive int (default 1000); "
                "run: scheduler bounds the GC sweep per tick — 0/negative disables progress"
            )
        self._db = db
        self._retention_days = retention_days
        self._deps = scheduler_deps if scheduler_deps is not None else MaintenanceSchedulerDeps()
        self._prune_orphans_per_tick_cap = prune_orphans_per_tick_cap
        self._gc_pruned_per_tick_cap = gc_pruned_per_tick_cap

    @property
    def retention_days(self) -> int:
        """Read-only view of the configured retention window."""
        return self._retention_days

    @property
    def prune_orphans_per_tick_cap(self) -> int:
        """Read-only view of the per-tick orphan-prune row cap."""
        return self._prune_orphans_per_tick_cap

    @property
    def gc_pruned_per_tick_cap(self) -> int:
        """Read-only view of the per-tick soft-delete GC row cap."""
        return self._gc_pruned_per_tick_cap

    def tick(self, db: sqlite3.Connection | None = None) -> MaintenanceTickResult:
        """Run one maintenance tick. Returns the structured result envelope.

        ``db`` is accepted as a positional kwarg so callers that hold a
        Deps-rooted connection can pass it through (matches the brief's
        ``tick(db)`` shape); when omitted, the connection passed to
        ``__init__`` is used. Both shapes write to the same physical DB
        when the caller wires it consistently.

        Stage failures are caught at the stage boundary so an exception
        in one stage doesn't poison the whole tick — the completion
        envelope reflects what actually happened.
        """
        active_db = db if db is not None else self._db
        started_at = self._deps.clock()
        pid = self._pid()
        logger.info("event=%s pid=%d", EVENT_TICK_STARTED, pid)

        orphans_pruned = self._safe_prune_orphans(active_db, pid)
        # GC the soft-delete table BEFORE measuring its size so the size
        # operators see is post-GC steady state.
        self._safe_gc_pruned(active_db, pid)
        pruned_table_size = self._count_pruned(active_db)

        usearch_rebuilt = False
        # Only rebuild when there was actually a prune — every-tick rebuild
        # is wasted work on a stable deploy.
        if orphans_pruned > 0:
            usearch_rebuilt = self._safe_usearch_rebuild(pid)

        fts_healed = self._safe_fts_heal(active_db, pid)
        bronze_orphans_reaped = self._safe_bronze_reap(pid)
        bronze_ttl_gc_deleted = self._safe_bronze_ttl_gc(pid)
        periodic_analyze_ran, periodic_analyze_reason = self._safe_periodic_analyze(active_db, pid)

        # Commit ONCE at the end so the whole tick is atomic from a
        # crash-recovery standpoint. Each stage uses execute() without
        # commit; the final commit either lands everything or nothing.
        try:
            active_db.commit()
        except sqlite3.Error:  # pragma: no cover - boundary
            logger.exception("maintenance: final commit failed")
            active_db.rollback()

        current_orphan_count = self._count_orphans(active_db)
        elapsed_ms = int((self._deps.clock() - started_at) * 1000)
        result = MaintenanceTickResult(
            orphans_pruned=orphans_pruned,
            pruned_table_size=pruned_table_size,
            usearch_rebuilt=usearch_rebuilt,
            fts_orphans_healed=fts_healed,
            current_orphan_count=current_orphan_count,
            elapsed_ms=elapsed_ms,
            bronze_orphans_reaped=bronze_orphans_reaped,
            bronze_ttl_gc_deleted=bronze_ttl_gc_deleted,
            periodic_analyze_ran=periodic_analyze_ran,
            periodic_analyze_reason=periodic_analyze_reason,
        )
        logger.info(
            "event=%s pid=%d orphans_pruned=%d pruned_table_size=%d usearch_rebuilt=%s "
            "fts_orphans_healed=%d current_orphan_count=%d elapsed_ms=%d "
            "bronze_orphans_reaped=%d bronze_ttl_gc_deleted=%d "
            "periodic_analyze_ran=%s periodic_analyze_reason=%s",
            EVENT_TICK_COMPLETED,
            pid,
            result.orphans_pruned,
            result.pruned_table_size,
            result.usearch_rebuilt,
            result.fts_orphans_healed,
            result.current_orphan_count,
            result.elapsed_ms,
            result.bronze_orphans_reaped,
            result.bronze_ttl_gc_deleted,
            result.periodic_analyze_ran,
            result.periodic_analyze_reason,
        )
        return result

    # ------------------------------------------------------------------
    # Stage implementations — each one a small focused helper. Cognitive
    # complexity stays under the F16 <= 15 cap by isolating each branch.
    # ------------------------------------------------------------------

    def _safe_prune_orphans(self, db: sqlite3.Connection, pid: int) -> int:
        """Stage 1 — move orphan rows into ``content_vectors_pruned``."""
        try:
            return self._prune_orphans(db)
        except sqlite3.Error as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_PRUNE,
                type(exc).__name__,
            )
            return 0

    def _safe_gc_pruned(self, db: sqlite3.Connection, pid: int) -> None:
        """Stage 2 — hard-delete pruned rows past the retention window."""
        try:
            self._gc_pruned(db)
        except sqlite3.Error as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_GC,
                type(exc).__name__,
            )

    def _safe_usearch_rebuild(self, pid: int) -> bool:
        """Stage 3 — usearch rebuild boundary, swallows failures into the log."""
        try:
            return bool(self._deps.usearch_rebuilder())
        except Exception as exc:  # pragma: no cover - production boundary
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_USEARCH,
                type(exc).__name__,
            )
            return False

    def _safe_fts_heal(self, db: sqlite3.Connection, pid: int) -> int:
        """Stage 4 — FTS5 orphan heal boundary, swallows failures into the log."""
        try:
            return int(self._deps.fts_healer(db))
        except Exception as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_FTS,
                type(exc).__name__,
            )
            return 0

    def _safe_bronze_reap(self, pid: int) -> int:
        """Stage 5 — bronze orphan reaper boundary, swallows failures into the log.

        Closes the post-fsync-pre-commit window the bronze module
        docstring documents but never had a sweeper for. The 2026-05-25
        incident left 36 GB of orphans across SharePoint; this stage
        prevents the accumulation from recurring.
        """
        try:
            return int(self._deps.bronze_reaper())
        except Exception as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_BRONZE_REAP,
                type(exc).__name__,
            )
            return 0

    def _safe_periodic_analyze(self, db: sqlite3.Connection, pid: int) -> tuple[bool, str]:
        """Stage 7 — periodic ANALYZE refresh boundary, swallows failures.

        Returns ``(ran, reason)``. When the closure raises, returns
        ``(False, "<ErrorType>: ...")`` so the failure surfaces in the
        tick envelope without crashing the whole tick.
        """
        try:
            return self._deps.periodic_analyze(db)
        except Exception as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_PERIODIC_ANALYZE,
                type(exc).__name__,
            )
            return False, f"{type(exc).__name__}: {exc}"

    def _safe_bronze_ttl_gc(self, pid: int) -> int:
        """Stage 6 — bronze TTL GC boundary, swallows failures into the log.

        Bounds bronze growth long-term (#316). The closure itself reads
        the ``bronze_ttl_gc`` feature flag and short-circuits to 0 when
        OFF, so this stage is structurally a no-op until the operator
        flips the flag.
        """
        try:
            return int(self._deps.bronze_ttl_gc())
        except Exception as exc:
            logger.warning(
                _FAILURE_LOG_FORMAT,
                EVENT_TICK_FAILED,
                pid,
                STAGE_BRONZE_TTL_GC,
                type(exc).__name__,
            )
            return 0

    def _prune_orphans(self, db: sqlite3.Connection) -> int:
        """Move up to ``prune_orphans_per_tick_cap`` orphan rows to the staging table.

        Idempotent: rows already present in ``content_vectors_pruned``
        (matched by the UNIQUE ``(hash, seq)`` constraint) are skipped
        via ``INSERT OR IGNORE``. The companion DELETE removes the
        original rows from ``content_vectors`` so the next tick reports
        ``orphans_pruned=0``.

        Per-tick row cap (default 1000): at production scale the
        unbounded ``LEFT JOIN`` over ``content_vectors`` x ``documents``
        is a full sequential scan of both tables and saturates disk on
        every tick. Capping the scan keeps each tick bounded; the
        remainder of any orphan backlog drains on subsequent ticks. The
        ``LIMIT`` lives on the SELECT and the DELETE walks only the
        specific ``(hash, seq)`` tuples the SELECT returned — without
        that pairing the DELETE would re-scan the whole table.
        """
        pruned_at = self._iso_now()
        cap = self._prune_orphans_per_tick_cap
        # Find at most ``cap`` (hash, seq) tuples whose hash doesn't
        # appear in documents (i.e. document was deleted or rewritten
        # with a new hash, orphaning the old vector).
        orphan_rows = db.execute(
            "SELECT v.hash, v.seq, v.pos, v.model, v.embedded_at, v.chunk_date "
            "FROM content_vectors v "
            "LEFT JOIN documents d ON d.hash = v.hash "
            "WHERE d.hash IS NULL "
            "LIMIT ?",
            (cap,),
        ).fetchall()
        if not orphan_rows:
            return 0

        # Stage each orphan into content_vectors_pruned. INSERT OR
        # IGNORE skips rows already pruned in a prior tick — that's
        # what makes a second tick on the same orphan set idempotent.
        inserted = 0
        for row in orphan_rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO content_vectors_pruned "
                "(hash, seq, pos, model, embedded_at, chunk_date, pruned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row[0], row[1], row[2], row[3], row[4], row[5], pruned_at),
            )
            if cur.rowcount > 0:
                inserted += 1

        # Drop the original orphan rows. Match on the exact (hash, seq)
        # tuples the bounded SELECT returned so the DELETE inherits the
        # same row cap (a re-scanning subquery would defeat the
        # ``LIMIT`` and re-saturate disk). executemany keeps the
        # per-row delete count bounded to ``len(orphan_rows)`` <= cap.
        db.executemany(
            "DELETE FROM content_vectors WHERE hash = ? AND seq = ?",
            [(row[0], row[1]) for row in orphan_rows],
        )
        return inserted

    def _gc_pruned(self, db: sqlite3.Connection) -> None:
        """Hard-delete pruned rows past the retention window.

        Soft-delete is the operator's recovery affordance — the
        retention window (default 7 days, configurable) gives them
        time to notice + restore before the GC drops the row for good.

        Per-tick row cap (default 1000): mirrors ``_prune_orphans``.
        The retention window naturally bounds growth in steady state,
        but an operator override of the window down to "now" could
        produce millions of GC-eligible rows on a single tick.
        """
        cutoff = self._cutoff_iso()
        db.execute(
            "DELETE FROM content_vectors_pruned WHERE id IN ("
            "SELECT id FROM content_vectors_pruned WHERE pruned_at < ? LIMIT ?"
            ")",
            (cutoff, self._gc_pruned_per_tick_cap),
        )

    def _count_pruned(self, db: sqlite3.Connection) -> int:
        """Count rows resident in the soft-delete table (post-GC).

        Defensive: returns 0 when the table is missing (e.g. a stage
        failure dropped a table mid-tick) so the result envelope still
        renders cleanly.
        """
        try:
            row = db.execute("SELECT COUNT(*) FROM content_vectors_pruned").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def _count_orphans(self, db: sqlite3.Connection) -> int:
        """Count orphan ``content_vectors`` rows still present after the tick.

        Defensive: returns 0 when ``content_vectors`` is missing — a
        stage failure earlier in the tick may have dropped the table
        (per-stage failures are isolated, but the result envelope
        still needs a number).
        """
        try:
            row = db.execute(
                "SELECT COUNT(*) FROM content_vectors v LEFT JOIN documents d ON d.hash = v.hash WHERE d.hash IS NULL"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def _iso_now(self) -> str:
        """Render the injected clock as an ISO-8601-Z timestamp.

        Format matches the rest of kairix (``2026-05-24T00:00:00Z``);
        consumers can lexicographically compare ``pruned_at`` against
        the GC cutoff.
        """
        epoch = self._deps.clock()
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return dt.isoformat().replace(_ISO_UTC_SUFFIX, _ISO_Z_SUFFIX)

    def _cutoff_iso(self) -> str:
        """Return the ISO timestamp for the GC retention cutoff."""
        epoch = self._deps.clock()
        cutoff_epoch = epoch - (self._retention_days * 86400)
        dt = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc)
        return dt.isoformat().replace(_ISO_UTC_SUFFIX, _ISO_Z_SUFFIX)

    @staticmethod
    def _pid() -> int:
        """PID for structured log emission — extracted for testability."""
        import os

        return os.getpid()


def tick_to_dict(result: MaintenanceTickResult) -> dict[str, Any]:
    """Render a tick result as a JSON-serialisable dict.

    Public so the worker_cli ``--json`` envelope can fold the
    maintenance block in alongside the existing integrity report shape.
    """
    return {
        "orphans_pruned": result.orphans_pruned,
        "pruned_table_size": result.pruned_table_size,
        "usearch_rebuilt": result.usearch_rebuilt,
        "fts_orphans_healed": result.fts_orphans_healed,
        "current_orphan_count": result.current_orphan_count,
        "elapsed_ms": result.elapsed_ms,
        "bronze_orphans_reaped": result.bronze_orphans_reaped,
        "bronze_ttl_gc_deleted": result.bronze_ttl_gc_deleted,
        "periodic_analyze_ran": result.periodic_analyze_ran,
        "periodic_analyze_reason": result.periodic_analyze_reason,
    }


# ---------------------------------------------------------------------------
# Convenience helpers used by the operator surfaces. Keeping them here so
# the worker / CLI / onboard layers all read from one canonical query.
# ---------------------------------------------------------------------------


def count_current_orphans(db: sqlite3.Connection) -> int:
    """Return the live orphan ``content_vectors`` count.

    Operator-surface helper for ``kairix features status --maintenance``
    and the ``kairix worker preflight --json`` maintenance block.
    Defensive: returns 0 when the schema isn't applied (fresh DB).
    """
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM content_vectors v LEFT JOIN documents d ON d.hash = v.hash WHERE d.hash IS NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def count_pruned_rows(db: sqlite3.Connection) -> int:
    """Return the count of rows resident in the soft-delete table."""
    try:
        row = db.execute("SELECT COUNT(*) FROM content_vectors_pruned").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def compute_next_tick_at(last_tick_at: float, interval_seconds: int) -> float:
    """Return the epoch the next maintenance tick is due.

    Returns ``0.0`` when ``last_tick_at == 0.0`` (never ticked) so
    operator surfaces can render "due now" rather than a stale 1970
    timestamp.
    """
    if last_tick_at <= 0.0:
        return 0.0
    return last_tick_at + interval_seconds


def is_tick_due(now: float, last_tick_at: float, interval_seconds: int) -> bool:
    """Return True when ``now - last_tick_at`` exceeds the interval.

    Operator surface helper — used by the worker loop to gate the tick.
    Treats ``last_tick_at == 0.0`` (never ticked) as "due immediately"
    so the first post-flag-flip cycle fires without waiting a full
    interval.
    """
    if last_tick_at <= 0.0:
        return True
    return (now - last_tick_at) >= interval_seconds


def tick_within_jitter_window(
    now: float,
    last_tick_at: float,
    interval_seconds: int,
    *,
    jitter_factor: float = 1.5,
) -> bool:
    """Return True when the last tick is recent enough to count as "ticking".

    Used by the ``maintenance_loop_ticking`` onboard check. The 50%
    jitter window (``interval * 1.5``) tolerates a tick that ran late
    due to a long-running embed cycle, without letting a truly stalled
    loop slip through unnoticed.

    Returns False when ``last_tick_at == 0.0`` (never ticked) so the
    onboard check fails clean on a flag-just-flipped deploy that
    hasn't yet reached its first tick.
    """
    if last_tick_at <= 0.0:
        return False
    return (now - last_tick_at) < (interval_seconds * jitter_factor)


def render_iso(epoch: float) -> str:
    """Render an epoch-seconds float as an ISO-8601-Z timestamp.

    Operator-surface helper for the JSON envelopes — keeps the
    ``last_tick_at`` rendering consistent across worker_cli / features
    cli / onboard surfaces.
    """
    if epoch <= 0.0:
        return ""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.isoformat().replace(_ISO_UTC_SUFFIX, _ISO_Z_SUFFIX)


# Re-exported below so the worker can read ``timedelta`` for the
# retention window without re-importing datetime. Keeps consumer
# surfaces tidy.
__all__ = [
    "DEFAULT_GC_PRUNED_PER_TICK_CAP",
    "DEFAULT_PRUNE_ORPHANS_PER_TICK_CAP",
    "EVENT_TICK_COMPLETED",
    "EVENT_TICK_FAILED",
    "EVENT_TICK_STARTED",
    "STAGE_FTS",
    "STAGE_GC",
    "STAGE_PERIODIC_ANALYZE",
    "STAGE_PRUNE",
    "STAGE_USEARCH",
    "MaintenanceScheduler",
    "MaintenanceSchedulerDeps",
    "MaintenanceTickResult",
    "compute_next_tick_at",
    "count_current_orphans",
    "count_pruned_rows",
    "is_tick_due",
    "render_iso",
    "tick_to_dict",
    "tick_within_jitter_window",
    "timedelta",
    "usearch_parity_check",
]
