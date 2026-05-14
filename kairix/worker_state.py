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
            # Enforce the field's declared type — JSON's permissive scalars
            # otherwise let "not-an-int" slip into an int field. The cast()
            # appeases mypy without changing runtime behaviour: int()/float()
            # raise on invalid input which read_state catches.
            from typing import cast

            field_type = fields[k].type
            if field_type is int or field_type == "int":
                filtered[k] = int(cast(str, v))
            elif field_type is float or field_type == "float":
                filtered[k] = float(cast(str, v))
            elif field_type is bool or field_type == "bool":
                filtered[k] = bool(v)
            else:
                filtered[k] = v
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
