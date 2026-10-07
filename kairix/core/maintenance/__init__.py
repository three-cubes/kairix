"""KFEAT-021 Phase 1 — background maintenance scheduler.

Public surface:

* :class:`MaintenanceScheduler` — owns the per-tick cleanup logic.
* :class:`MaintenanceTickResult` — frozen-dataclass envelope returned
  from :func:`MaintenanceScheduler.tick` (F39 / F42 boundary shape).
* :class:`MaintenanceSchedulerDeps` — injection seam for usearch +
  fts-rebuild boundaries; production callers omit it.

Design rationale: Phase 1 ships proactive orphan cleanup behind the
``maintenance_loop`` feature flag; subsequent phases expand the surface
to vector-index compaction and FTS rebuilds.
"""

from __future__ import annotations

from kairix.core.maintenance.scheduler import (
    DEFAULT_GC_PRUNED_PER_TICK_CAP,
    DEFAULT_PRUNE_ORPHANS_PER_TICK_CAP,
    EVENT_TICK_COMPLETED,
    EVENT_TICK_FAILED,
    EVENT_TICK_STARTED,
    STAGE_FTS,
    STAGE_GC,
    STAGE_PERIODIC_ANALYZE,
    STAGE_PRUNE,
    STAGE_USEARCH,
    MaintenanceScheduler,
    MaintenanceSchedulerDeps,
    MaintenanceTickResult,
    compute_next_tick_at,
    count_current_orphans,
    count_pruned_rows,
    is_tick_due,
    render_iso,
    tick_to_dict,
    tick_within_jitter_window,
    usearch_parity_check,
)

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
    "usearch_parity_check",
]
