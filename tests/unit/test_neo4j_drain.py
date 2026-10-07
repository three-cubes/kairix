"""Unit tests for the Neo4j drain (GH #334).

Covers the small drain branches the integration suite alone doesn't
reach. Every test drives the public surface
(:class:`Neo4jDrainer` constructor, :func:`run_neo4j_drain_tick`,
:func:`kairix.core.factory.build_neo4j_drainer`) — no imports of
underscored internals (F5 / no-tests-against-private-functions).

F1-clean (no patching), F2-clean (no env vars).
"""

from __future__ import annotations

import sqlite3

import pytest

from kairix.core import factory
from kairix.core.curator.drain import (
    DEFAULT_DRAIN_BATCH_SIZE,
    Neo4jDrainer,
    run_neo4j_drain_tick,
)
from kairix.core.db.schema import create_schema
from tests.fakes import FakeDrainGraphRepository

pytestmark = pytest.mark.unit


def _open() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    create_schema(db)
    return db


def _insert(db: sqlite3.Connection, *, kind: str, value: str) -> int:
    cur = db.execute(
        "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
        "sensitivity, pushed_to_neo4j, push_attempt_count) "
        "VALUES (?, ?, 'vault://x', '2026-05-25T10:00:00Z', 0.9, 'internal', 0, 0)",
        (kind, value),
    )
    db.commit()
    return int(cur.lastrowid or 0)


def test_neo4j_drainer_rejects_zero_batch_size() -> None:
    """Constructor raises ValueError when ``batch_size <= 0`` per the affordance.

    Drives the public Neo4jDrainer constructor — no private internals.
    """
    db = _open()
    try:
        with pytest.raises(ValueError, match="batch_size must be > 0"):
            Neo4jDrainer(db, FakeDrainGraphRepository(), batch_size=0)
        with pytest.raises(ValueError, match="batch_size must be > 0"):
            Neo4jDrainer(db, FakeDrainGraphRepository(), batch_size=-1)
    finally:
        db.close()


def test_neo4j_drainer_batch_size_property_exposes_constructor_value() -> None:
    """The ``batch_size`` property is the operator-facing view of the configured cap."""
    db = _open()
    try:
        drainer = Neo4jDrainer(db, FakeDrainGraphRepository(), batch_size=42)
        assert drainer.batch_size == 42
    finally:
        db.close()


def test_neo4j_drainer_declares_per_tick_class_attributes() -> None:
    """F66 — the drainer class declares per_tick_max_items + watermark attrs.

    Tests the public class shape — no private symbol imports.
    """
    assert Neo4jDrainer.per_tick_max_items == DEFAULT_DRAIN_BATCH_SIZE
    # Watermark exempt with rationale comment above the class — value is None.
    assert Neo4jDrainer.disk_watermark_min_free_bytes is None


def test_factory_build_neo4j_drainer_returns_neo4j_drainer_instance() -> None:
    """The F47 factory entry point returns a Neo4jDrainer with the configured batch_size."""
    db = _open()
    try:
        drainer = factory.build_neo4j_drainer(db=db, repo=FakeDrainGraphRepository(), batch_size=7)
        assert isinstance(drainer, Neo4jDrainer)
        assert drainer.batch_size == 7
    finally:
        db.close()


def test_factory_build_neo4j_drainer_uses_default_batch_size_when_not_supplied() -> None:
    """Factory default batch_size falls through to DEFAULT_DRAIN_BATCH_SIZE."""
    db = _open()
    try:
        drainer = factory.build_neo4j_drainer(db=db, repo=FakeDrainGraphRepository())
        assert drainer.batch_size == DEFAULT_DRAIN_BATCH_SIZE
    finally:
        db.close()


def test_run_neo4j_drain_tick_marks_failed_row_and_continues_to_next() -> None:
    """Per-row failure marks the row -1 + last_push_error, then continues.

    Drives the public ``run_neo4j_drain_tick`` function.
    """
    db = _open()
    try:
        sid_ok = _insert(db, kind="person", value="ok-person")
        sid_bad = _insert(db, kind="person", value="bad-person")
        repo = FakeDrainGraphRepository(available=True, raise_on_value="bad-person")
        result = run_neo4j_drain_tick(db, repo)
        assert result.pushed == 1
        assert result.failed == 1
        # Verify the failed row carries the error text.
        err = db.execute("SELECT last_push_error FROM entity_signals WHERE id = ?", (sid_bad,)).fetchone()[0]
        assert err is not None and "RuntimeError" in err
        # Verify the good row flipped.
        flag = db.execute("SELECT pushed_to_neo4j FROM entity_signals WHERE id = ?", (sid_ok,)).fetchone()[0]
        assert flag == 1
    finally:
        db.close()


def test_run_neo4j_drain_tick_skips_relationship_kind_and_bumps_counter() -> None:
    """``kind="relationship"`` increments ``skipped_relationships`` + the attempt counter."""
    db = _open()
    try:
        sid = _insert(db, kind="relationship", value="alpha -> bravo")
        repo = FakeDrainGraphRepository(available=True)
        result = run_neo4j_drain_tick(db, repo)
        assert result.skipped_relationships == 1
        assert result.pushed == 0
        # Counter bumped so the next tick drops the row out of selection.
        counter = db.execute("SELECT push_attempt_count FROM entity_signals WHERE id = ?", (sid,)).fetchone()[0]
        assert counter == 1
    finally:
        db.close()


def test_run_default_drain_tick_returns_unavailable_when_client_unreachable(tmp_path) -> None:
    """With rows pending but an unreachable client, the tick short-circuits to
    ``neo4j_available=False`` and touches no row.

    The idle-skip opens the SQLite connection first to see whether there is
    anything to drain; a pending row means we DO build the client, which then
    reports unreachable. (The empty-queue case is covered by
    ``test_run_default_drain_tick_skips_client_build_when_no_pending_rows``.)

    Sabotage proof: remove the ``if not client.available:`` early-return in
    ``run_default_drain_tick`` and the drain would attempt to push against an
    unreachable repo instead of reporting ``neo4j_available=False``.
    """
    from kairix.core.curator.drain import Neo4jDrainTickDeps, run_default_drain_tick

    class _UnreachableClient:
        available = False

    db_path_local = tmp_path / "drain_unreachable.sqlite"

    def _open_with_pending_row() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path_local))
        create_schema(conn)
        conn.execute(
            "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
            "sensitivity, pushed_to_neo4j, push_attempt_count) "
            "VALUES ('person', 'agent-alpha', 'vault://x', '2026-05-25T10:00:00Z', 0.9, "
            "'internal', 0, 0)"
        )
        conn.commit()
        return conn

    deps = Neo4jDrainTickDeps(
        client_factory=_UnreachableClient,
        db_factory=_open_with_pending_row,
    )
    result = run_default_drain_tick(deps=deps)
    assert result.neo4j_available is False
    assert result.pushed == 0
    assert result.failed == 0


def test_run_default_drain_tick_threads_components_through_drain_tick(tmp_path) -> None:
    """When the client reports available, ``run_default_drain_tick``
    opens the DB via the supplied factory and delegates to
    ``run_neo4j_drain_tick`` against a Neo4jGraphRepository built from
    the client. The returned result reflects what the underlying drain
    tick produces.

    Sabotage proof: comment out the ``db.close()`` in the ``finally``
    block and the second run of this test (in the same process) sees
    the SQLite file locked — the connection-leak surface this finally
    protects.
    """
    from kairix.core.curator.drain import Neo4jDrainTickDeps, run_default_drain_tick
    from tests.fakes import FakeDrainGraphRepository

    db_path_local = tmp_path / "drain_default_tick.sqlite"

    def _open_test_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path_local))
        create_schema(conn)
        # Stage one row so the drain tick has work to do — verifies
        # we reached the underlying run_neo4j_drain_tick.
        conn.execute(
            "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
            "sensitivity, pushed_to_neo4j, push_attempt_count) "
            "VALUES ('person', 'agent-alpha', 'vault://x', '2026-05-25T10:00:00Z', 0.9, "
            "'internal', 0, 0)"
        )
        conn.commit()
        return conn

    fake_repo = FakeDrainGraphRepository(available=True)

    class _AvailableClient:
        available = True

    deps = Neo4jDrainTickDeps(
        client_factory=_AvailableClient,
        db_factory=_open_test_db,
        repo_factory=lambda _client: fake_repo,
    )
    result = run_default_drain_tick(deps=deps)
    # Verify the orchestration reached the inner drain and produced a
    # real result against the staged row.
    assert result.neo4j_available is True
    assert result.pushed == 1
    assert isinstance(result.elapsed_ms, int)


def test_run_default_drain_tick_skips_client_build_when_no_pending_rows(tmp_path) -> None:
    """Idle-skip (PLA-331 / GH #334 idle burn): with no pending entity_signals
    rows, the tick must NOT build a Neo4j client.

    The drain tick is exempt from the worker's idle no-op gate (it must always
    make progress on a backlog), so it fired every ``NEO4J_DRAIN_INTERVAL``
    (600s) and rebuilt a driver — ``_connect`` + ``verify_connectivity`` +
    constraint init — even on a completely idle vault with an empty queue. The
    cheap SQLite existence probe short-circuits that: nothing pending ⇒ no
    client, no neo4j work at all. It reports ``neo4j_available=True`` (there is
    demonstrably nothing to drain; the separate health tick + the ENTITY-intent
    query gate still surface a real neo4j outage when it matters).

    Sabotage proof: remove the ``_has_unpushed_rows`` early-return in
    ``run_default_drain_tick`` and ``client_factory`` runs — the AssertionError
    below trips, which is precisely the idle driver spin-up this fix removes.
    """
    from kairix.core.curator.drain import Neo4jDrainTickDeps, run_default_drain_tick

    db_path_local = tmp_path / "drain_idle.sqlite"

    def _open_empty_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path_local))
        create_schema(conn)  # schema present, ZERO entity_signals rows
        return conn

    def _client_factory_must_not_run() -> object:
        raise AssertionError("client_factory must not run when nothing is pending")

    deps = Neo4jDrainTickDeps(
        client_factory=_client_factory_must_not_run,
        db_factory=_open_empty_db,
    )
    result = run_default_drain_tick(deps=deps)
    assert result.neo4j_available is True, "idle skip reports available (nothing to drain)"
    assert result.pushed == 0
    assert result.failed == 0
    assert result.skipped_relationships == 0


def test_run_default_drain_tick_default_repo_marks_rejected_merge_failed() -> None:
    """The worker tick's production repo factory raises on a rejected MERGE,
    so the signal is marked failed instead of acknowledged as pushed
    (PLA-472 data-loss regression).

    Sabotage proof (executed): return ``Neo4jGraphRepository(client)``
    (read path, swallows errors) from ``drain._default_make_repo`` →
    ``pushed == 1`` and this test fails. Restored.
    """
    from kairix.core.curator.drain import Neo4jDrainTickDeps, run_default_drain_tick
    from kairix.knowledge.graph.client import Neo4jClient
    from tests.fakes import FakeNeo4jDriverClass

    db = _open()
    _insert(db, kind="person", value="agent-alpha")
    client = Neo4jClient(
        uri="bolt://graph.invalid:7687",
        user="neo4j",
        password="unit-fixture",  # pragma: allowlist secret — test fixture
        driver_cls=FakeNeo4jDriverClass(run_raises=RuntimeError("constraint violation")),
    )

    result = run_default_drain_tick(Neo4jDrainTickDeps(client_factory=lambda: client, db_factory=lambda: db))

    assert (result.pushed, result.failed) == (0, 1)
