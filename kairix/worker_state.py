"""Worker state — observable phase + activity counters (#224 phase 4-5 scaffold).

Two responsibilities:

1. **State model** (`WorkerState` dataclass): structured fields ops can read
   to tell whether the worker is idle / ingesting / doing maintenance, when
   the last embed run was, how many recall alerts have fired this session, etc.

2. **Atomic JSON persistence**: the worker writes its state to a single
   ``worker-state.json`` file in the kairix data dir on every phase change.
   ``kairix worker status`` and external monitors read it. Writes go through
   ``write_state`` which does temp-file + rename so concurrent readers never
   see a half-written file.

This module ships as scaffolding before the consumer phases (#224 phase 4
pause/resume and phase 5 health fields) so multiple agents can build on it
without conflict.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


class WorkerPhase(str, Enum):
    """High-level state the worker can be in.

    ``str`` mixin so values JSON-serialize cleanly and operator-readable
    ``kairix worker status`` prints the lowercase name directly.
    """

    STARTING = "starting"
    IDLE = "idle"
    INGEST = "ingest"
    MAINTENANCE = "maintenance"
    PAUSED = "paused"
    REPAIR = "repair"


def _coerce_to_field_type(field_type: object, value: object) -> object:
    """Coerce a JSON value to a ``WorkerState`` field's declared scalar type.

    Enforces the declared type — JSON's permissive scalars otherwise let
    "not-an-int" slip into an int field. The cast() appeases mypy without
    changing runtime behaviour: int()/float() raise on invalid input, which
    read_state catches. Non-scalar field types pass the value through.
    """
    if field_type is int or field_type == "int":
        return int(cast(str, value))
    if field_type is float or field_type == "float":
        return float(cast(str, value))
    if field_type is bool or field_type == "bool":
        return bool(value)
    return value


@dataclass
class WorkerState:
    """Observable worker state — persisted to JSON, read by ops tooling.

    All fields are scalars (int / float / str / bool) so JSON round-trip is
    lossless. Timestamps are epoch-seconds floats; consumers format as needed.
    """

    current_phase: WorkerPhase = WorkerPhase.STARTING
    started_at: float = field(default_factory=time.time)
    last_phase_change_at: float = field(default_factory=time.time)
    last_embed_run_at: float = 0.0
    last_embed_did_work: bool = False
    consecutive_embed_noops: int = 0
    embedded_total: int = 0
    failed_chunks_total: int = 0
    recall_alerts_total: int = 0
    restart_count: int = 0
    next_scheduled_embed_at: float = 0.0
    # KFEAT-021 Phase 1 — maintenance-tick observability fields. Persisted
    # so ``kairix features status --maintenance`` and the
    # ``maintenance_loop_ticking`` onboard check can read the last-tick
    # cadence across worker restarts.
    last_maintenance_tick_at: float = 0.0
    last_maintenance_orphans_pruned: int = 0
    last_maintenance_pruned_table_size: int = 0
    last_maintenance_elapsed_ms: int = 0
    # ADR-036 — entity-summary projector tick observability fields.
    # Persisted so ``kairix features status`` + ``kairix doctor`` can
    # read the last projection counts across worker restarts.
    last_entity_summary_tick_at: float = 0.0
    last_entity_summary_projected: int = 0
    last_entity_summary_updated: int = 0
    last_entity_summary_skipped: int = 0
    last_entity_summary_failed: int = 0
    # SYNC-OBS — connector-sync tick observability cluster. The #1 blind
    # spot is that a quiet source (polled OK, zero new docs) looked
    # identical to a dead one. ``syncs_attempted`` increments on EVERY
    # connector-sync tick regardless of yield, so ``kairix worker status``
    # proves the worker is still polling even on a zero-doc tick.
    # ``last_connector_tick_yielded`` records whether the last tick
    # surfaced any items (False == quiet, True == items flowed); the rest
    # mirror the ``last_maintenance_*`` / ``last_entity_summary_*`` clusters
    # above so the same persist/restore + status-render path applies.
    syncs_attempted: int = 0
    last_connector_sync_at: float = 0.0
    last_connector_tick_yielded: bool = False
    last_connector_synced: int = 0
    last_connector_dead_letter_added: int = 0
    last_connector_connectors_polled: int = 0

    def to_dict(self) -> dict[str, object]:
        """JSON-safe dict. Enum values export as strings via the ``str`` mixin."""
        d = asdict(self)
        d["current_phase"] = self.current_phase.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WorkerState:
        """Reverse of ``to_dict``. Missing fields fall back to dataclass defaults.

        Raises ``ValueError`` / ``TypeError`` on schema mismatch (e.g. a string
        where an int is required); ``read_state`` catches those and treats the
        file as no-prior-state.
        """
        # Filter to known fields so future schema additions don't break older readers.
        fields = cls.__dataclass_fields__
        filtered: dict[str, object] = {}
        for k, v in data.items():
            if k not in fields:
                continue
            if k == "current_phase":
                filtered[k] = WorkerPhase(v)
                continue
            filtered[k] = _coerce_to_field_type(fields[k].type, v)
        # mypy can't see that ``filtered`` was type-coerced above; cast tells
        # it the kwargs are the right shape per the dataclass field types.
        return cls(**filtered)  # type: ignore[arg-type]  # type-coerced in the loop above


def write_state(state: WorkerState, path: Path) -> None:
    """Atomically persist ``state`` to ``path``.

    Writes a sibling ``<path>.tmp`` then renames over the target — ``os.rename``
    is atomic on POSIX so concurrent readers see either the old file or the
    new one, never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def read_state(path: Path) -> WorkerState | None:
    """Read worker state from JSON. Returns ``None`` if the file is missing or
    unreadable — callers treat that as "no prior state, fresh start"."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("worker_state.read_state: %s — treating as no prior state", e)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return WorkerState.from_dict(data)
    except (TypeError, ValueError) as e:
        logger.warning("worker_state.read_state: schema mismatch — %s; treating as no prior state", e)
        return None
