"""Tests for kairix.worker_state — #224 phase 4-5 scaffolding.

Cover the three things subsequent phase agents will rely on:

  - WorkerState round-trips through to_dict / from_dict losslessly.
  - write_state is atomic — a concurrent read never sees a partial JSON file.
  - read_state returns None on missing / unreadable / schema-mismatch files
    so callers don't need defensive try/except.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.worker_state import (
    WorkerPhase,
    WorkerState,
    read_state,
    write_state,
)


@pytest.mark.unit
def test_worker_state_defaults_to_starting_phase() -> None:
    """Fresh WorkerState starts in STARTING phase with zero counters."""
    s = WorkerState()
    assert s.current_phase is WorkerPhase.STARTING
    assert s.consecutive_embed_noops == 0
    assert s.embedded_total == 0
    assert s.restart_count == 0


@pytest.mark.unit
def test_worker_state_round_trip_preserves_phase_enum() -> None:
    """to_dict + from_dict survives the WorkerPhase enum mixin.

    Sabotage-prove: if from_dict didn't reconstruct the enum, the assertion
    would compare a bare string to the enum and fail.
    """
    original = WorkerState(
        current_phase=WorkerPhase.MAINTENANCE,
        consecutive_embed_noops=4,
        embedded_total=128,
    )
    restored = WorkerState.from_dict(original.to_dict())
    assert restored.current_phase is WorkerPhase.MAINTENANCE
    assert restored.consecutive_embed_noops == 4
    assert restored.embedded_total == 128


@pytest.mark.unit
def test_worker_state_from_dict_drops_unknown_fields(tmp_path: Path) -> None:
    """Forward-compat: unknown fields in a stored JSON don't blow up an older reader."""
    raw = {
        "current_phase": "idle",
        "consecutive_embed_noops": 2,
        "future_field_added_in_next_version": "ignored",
    }
    s = WorkerState.from_dict(raw)
    assert s.current_phase is WorkerPhase.IDLE
    assert s.consecutive_embed_noops == 2


@pytest.mark.unit
def test_write_state_is_atomic_via_temp_rename(tmp_path: Path) -> None:
    """write_state writes a sibling .tmp then renames — concurrent readers
    never see a half-written file. Sabotage-prove by asserting the temp
    file does NOT linger after the call."""
    target = tmp_path / "worker-state.json"
    s = WorkerState(current_phase=WorkerPhase.INGEST, embedded_total=7)

    write_state(s, target)

    assert target.exists()
    assert not target.with_suffix(target.suffix + ".tmp").exists()

    data = json.loads(target.read_text())
    assert data["current_phase"] == "ingest"
    assert data["embedded_total"] == 7


@pytest.mark.unit
def test_read_state_returns_none_when_file_missing(tmp_path: Path) -> None:
    """No file → no prior state. Caller treats as fresh start."""
    assert read_state(tmp_path / "does-not-exist.json") is None


@pytest.mark.unit
def test_read_state_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """Garbage JSON is a crash-recovery scenario — treat as no prior state
    so the worker boots fresh instead of failing to start."""
    bad = tmp_path / "worker-state.json"
    bad.write_text("{this is not valid json:")
    assert read_state(bad) is None


@pytest.mark.unit
def test_read_state_returns_none_on_schema_mismatch(tmp_path: Path) -> None:
    """A JSON file whose values don't match the dataclass types is also
    treated as 'no prior state' rather than raising."""
    bad = tmp_path / "worker-state.json"
    bad.write_text('{"consecutive_embed_noops": "not-an-int"}')
    assert read_state(bad) is None


@pytest.mark.unit
def test_read_state_coerces_float_fields_to_float(tmp_path: Path) -> None:
    """Float timestamps written as JSON strings / ints come back as real floats.

    Sabotage-prove: skipping the float coercion leaves ``"1700000000.5"``
    a str and ``1700000000`` an int — the type assertions fail.
    """
    target = tmp_path / "worker-state.json"
    target.write_text(json.dumps({"last_embed_run_at": "1700000000.5", "started_at": 1700000000}))

    restored = read_state(target)

    assert restored is not None
    assert type(restored.last_embed_run_at) is float
    assert restored.last_embed_run_at == 1700000000.5
    assert type(restored.started_at) is float


@pytest.mark.unit
def test_read_state_returns_none_when_float_field_is_not_numeric(tmp_path: Path) -> None:
    """A non-numeric timestamp is a schema mismatch, not a value to carry forward."""
    bad = tmp_path / "worker-state.json"
    bad.write_text('{"last_embed_run_at": "yesterday"}')
    assert read_state(bad) is None


@pytest.mark.unit
def test_read_state_coerces_bool_fields_to_bool(tmp_path: Path) -> None:
    """Bool flags persisted as 0/1 (older writers, hand-edited files) read back as bools.

    Sabotage-prove: skipping the bool coercion leaves the raw ints — the
    ``is True`` / ``is False`` identity checks fail.
    """
    target = tmp_path / "worker-state.json"
    target.write_text(json.dumps({"last_embed_did_work": 1, "last_connector_tick_yielded": 0}))

    restored = read_state(target)

    assert restored is not None
    assert restored.last_embed_did_work is True
    assert restored.last_connector_tick_yielded is False


@pytest.mark.unit
def test_read_state_returns_none_when_root_not_a_dict(tmp_path: Path) -> None:
    """A JSON array (or scalar) at the root isn't a valid state — return None."""
    bad = tmp_path / "worker-state.json"
    bad.write_text("[1, 2, 3]")
    assert read_state(bad) is None


@pytest.mark.unit
def test_write_then_read_round_trip(tmp_path: Path) -> None:
    """Real-world flow: write then read returns the same state."""
    target = tmp_path / "ws.json"
    original = WorkerState(
        current_phase=WorkerPhase.PAUSED,
        consecutive_embed_noops=12,
        restart_count=3,
        recall_alerts_total=2,
    )
    write_state(original, target)

    restored = read_state(target)
    assert restored is not None
    assert restored.current_phase is WorkerPhase.PAUSED
    assert restored.consecutive_embed_noops == 12
    assert restored.restart_count == 3
    assert restored.recall_alerts_total == 2


@pytest.mark.unit
def test_worker_state_round_trip_preserves_connector_sync_fields() -> None:
    """SYNC-OBS: the connector-sync cluster survives the JSON round-trip.

    The fields persist across worker restarts (mirroring the
    ``last_maintenance_*`` cluster) so ``kairix worker status`` reports the
    last-known sync heartbeat even immediately after a container bounce.

    Sabotage proof: drop ``syncs_attempted`` from the dataclass and the
    ``from_dict`` filter discards it → the assertion fails.
    """
    original = WorkerState(
        syncs_attempted=11,
        last_connector_sync_at=1234.5,
        last_connector_tick_yielded=True,
        last_connector_synced=6,
        last_connector_dead_letter_added=2,
        last_connector_connectors_polled=4,
    )
    restored = WorkerState.from_dict(original.to_dict())
    assert restored.syncs_attempted == 11
    assert restored.last_connector_sync_at == 1234.5
    assert restored.last_connector_tick_yielded is True
    assert restored.last_connector_synced == 6
    assert restored.last_connector_dead_letter_added == 2
    assert restored.last_connector_connectors_polled == 4
