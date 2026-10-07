"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SourceConnector`.

Every public method on :class:`kairix.core.protocols.SourceConnector`
has at least one test here that exercises a named failure class
(``raises`` / ``times_out`` / ``returns_partial`` / ``returns_empty`` /
``unauthorized`` / ``unavailable``) AND asserts a CONCRETE observable
outcome — a row count, a row's column value, an exception type, a
returned value — not a Mock call-count.

Bug 2 (2026-05 SharePoint 429 dead-lettering every item on a throttled
drive) shipped because no contract test exercised the rate-limit path.
F68 makes the failure-behaviour contract mechanically required for
every Protocol method.

Composition follows F47 — pipelines are built via
:func:`kairix.core.factory.build_connector_pipeline` with canonical
fakes from :mod:`tests.fakes` injected as overrides. No monkeypatches,
no private-attribute substitution.

F43: where a shipped connector can exhibit the failure through its
DI seams, the test runs ONE assertion body over that real connector
(:class:`kairix.connectors.slack.SlackConnector` /
:class:`kairix.connectors.sharepoint.SharePointConnector`, driven by
the :class:`tests.fakes.FakeSlackWebApi` /
:class:`tests.fakes.FakeSharePointGraphApi` MockTransport stubs) AND
:class:`tests.fakes.FakeSourceConnector`. ``source_link`` /
``sensitivity_for`` / ``next_cursor`` / ``metadata_for`` raising has no
real-connector counterpart: every shipped connector implements those
as pure cache / config reads that fall back instead of raising. The
F68 tests for those methods therefore pin the shared fallback
behaviour, and the ConnectorPipeline's handling of a connector that
DOES raise there lives in ``tests/unit/test_connector_pipeline.py``.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth; mutations were executed during
authoring and the sabotage assertions failed concretely (then the
mutation was reverted).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import httpx
import pytest

from kairix.core.db.schema import create_schema
from kairix.core.factory import build_connector_pipeline
from kairix.core.protocols import ChangeEvent, SourceConnector
from tests.fakes import (
    FakeChunkWriter,
    FakeEntityGraphSink,
    FakeExtractor,
    FakeSharePointGraphApi,
    FakeSlackWebApi,
    FakeSourceConnector,
)

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Helpers — factory-composed pipeline with canonical fakes (F47-compliant).
# ---------------------------------------------------------------------------


def _build_pipeline(
    db: sqlite3.Connection,
    *,
    chunk_writer: FakeChunkWriter | None = None,
    entity_graph_sink: FakeEntityGraphSink | None = None,
):
    """F47-compliant: ConnectorPipeline composed via the factory entry point."""
    return build_connector_pipeline(
        db=db,
        collection="default",
        chunk_writer=chunk_writer if chunk_writer is not None else FakeChunkWriter(),
        entity_graph_sink=entity_graph_sink if entity_graph_sink is not None else FakeEntityGraphSink(),
    )


def _make_event(item_id: str, modified_at: str = "2026-01-01T00:00:00Z") -> ChangeEvent:
    return ChangeEvent(op="created", item_id=item_id, modified_at=modified_at)


def _dead_letter_rows(db: sqlite3.Connection, source_name: str) -> list[tuple[str, str]]:
    return list(
        db.execute(
            "SELECT item_id, last_error FROM connector_deadletter WHERE source_name = ? ORDER BY item_id",
            (source_name,),
        ).fetchall()
    )


def _cursor_token(db: sqlite3.Connection, source_name: str) -> str | None:
    row = db.execute(
        "SELECT cursor_token FROM connector_cursors WHERE source_name = ?",
        (source_name,),
    ).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# SourceConnector.list_changes
# ---------------------------------------------------------------------------


def _list_changes_raising_real() -> SourceConnector:
    """Real Slack connector whose channel enumeration fails (non-ok ``conversations.list``)."""
    api = FakeSlackWebApi(responses={"conversations.list": {"ok": False, "error": "F68_list_changes_raises"}})
    connector: SourceConnector = api.build_connector()
    return connector


def _list_changes_raising_fake() -> SourceConnector:
    return FakeSourceConnector(
        name="raising-list",
        raise_on_list_changes=RuntimeError("F68_list_changes_raises"),
    )


@pytest.mark.parametrize(
    "factory",
    [_list_changes_raising_real, _list_changes_raising_fake],
    ids=["real", "fake"],
)
def test_list_changes_raises_propagates_and_leaves_cursor_unchanged(
    factory: Callable[[], SourceConnector],
) -> None:
    """A connector whose :meth:`list_changes` raises must surface the
    exception (not silently truncate the batch) AND the prior cursor
    must NOT be clobbered — the next tick can resume from the
    last-known-good token.

    Sabotage proof: in ``kairix.connectors.slack.connector.SlackConnector.list_changes``
    wrap ``self._enumerate_member_channels(web)`` in
    ``try/except RuntimeError: return iter([])``.
    Re-run: the real leg fails because ``pytest.raises`` sees no
    exception. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    source = factory()
    # Seed a baseline cursor so we can prove ``list_changes`` failing
    # didn't overwrite it.
    db.execute(
        "INSERT INTO connector_cursors (source_name, cursor_token, updated_at) VALUES (?, ?, ?)",
        (source.name, "cursor-A", "2026-01-01T00:00:00Z"),
    )
    db.commit()

    pipeline = _build_pipeline(db)

    with pytest.raises(RuntimeError, match="F68_list_changes_raises"):
        pipeline.run_batch(source, FakeExtractor())

    # Cursor must NOT have been clobbered — the next tick will re-read
    # ``cursor-A`` and retry the same range.
    assert _cursor_token(db, source.name) == "cursor-A"
    # No items dead-lettered (the failure happened before any item was
    # processed) — proves the exception fired at the iteration boundary.
    assert _dead_letter_rows(db, source.name) == []
    db.close()


# ---------------------------------------------------------------------------
# SourceConnector.fetch
# ---------------------------------------------------------------------------


def _three_items() -> list[dict[str, object]]:
    return [{"id": f"item-{i:03d}", "content": f"body-{i}".encode()} for i in range(3)]


def _fetch_raising_real() -> SourceConnector:
    """Real SharePoint connector; Graph answers 404 on item-001's ``/content``."""
    connector: SourceConnector = FakeSharePointGraphApi(
        items=_three_items(),
        content_failures={"item-001": 404},
    ).build_connector()
    return connector


def _fetch_raising_fake() -> SourceConnector:
    return FakeSourceConnector(
        name="fetch-raises",
        events=[_make_event(f"item-{i:03d}") for i in range(3)],
        content={f"item-{i:03d}": f"body-{i}".encode() for i in range(3)},
        fail_on_fetch={"item-001"},
    )


@pytest.mark.parametrize("factory", [_fetch_raising_real, _fetch_raising_fake], ids=["real", "fake"])
def test_fetch_raises_propagates_to_dead_letter(factory: Callable[[], SourceConnector]) -> None:
    """A connector that raises on ``fetch`` for one item must
    dead-letter that item; sibling items still process and the
    per-item failure does not abort the batch.

    Sabotage proof: in ``kairix.connectors.sharepoint.graph_client.SharePointGraphClient._authorised_get``
    drop the trailing ``response.raise_for_status()``. Re-run: the real
    leg fails because the 404 body is indexed (processed == 3,
    dead_lettered == 0). Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    source = factory()
    pipeline = _build_pipeline(db)
    result = pipeline.run_batch(source, FakeExtractor())

    rows = _dead_letter_rows(db, source.name)
    assert result.processed == 2
    assert result.dead_lettered == 1
    assert len(rows) == 1
    assert rows[0][0] == "item-001"
    assert "fetch" in rows[0][1].lower()
    db.close()


def _fetch_timing_out_real() -> SourceConnector:
    """Real SharePoint connector; the transport times out on item-001's ``/content``."""
    connector: SourceConnector = FakeSharePointGraphApi(
        items=[{"id": "item-001", "content": b"a"}, {"id": "item-002", "content": b"b"}],
        content_failures={"item-001": httpx.ReadTimeout("simulated Graph read timeout")},
    ).build_connector()
    return connector


def _fetch_timing_out_fake() -> SourceConnector:
    return FakeSourceConnector(
        name="fetch-times-out",
        events=[_make_event("item-001"), _make_event("item-002")],
        content={"item-001": b"a", "item-002": b"b"},
        timeout_on_fetch={"item-001"},
    )


@pytest.mark.parametrize("factory", [_fetch_timing_out_real, _fetch_timing_out_fake], ids=["real", "fake"])
def test_fetch_times_out_propagates_to_dead_letter(factory: Callable[[], SourceConnector]) -> None:
    """A connector whose ``fetch`` times out for one item must
    dead-letter it with the timeout error preserved in ``last_error``.
    Mirrors the SharePoint Graph timeout path that surfaced as Bug 2 in
    2026-05 — a timeout is a different failure class than a generic
    RuntimeError but the pipeline's handler must absorb both shapes
    identically.

    Sabotage proof: in ``kairix.connectors.sharepoint.connector.SharePointConnector.fetch``
    wrap the ``fetch_item_content`` call in ``try/except httpx.TimeoutException: raw = b"x"``.
    Re-run: the real leg fails because no dead-letter row appears and
    ``result.dead_lettered == 0`` instead of 1. Restored.
    """
    db = sqlite3.connect(":memory:")
    create_schema(db)
    source = factory()
    pipeline = _build_pipeline(db)
    result = pipeline.run_batch(source, FakeExtractor())

    rows = _dead_letter_rows(db, source.name)
    assert result.dead_lettered == 1
    assert len(rows) == 1
    assert rows[0][0] == "item-001"
    assert "timeout" in rows[0][1].lower()
    db.close()


# ---------------------------------------------------------------------------
# SourceConnector.source_link
# ---------------------------------------------------------------------------


def _undrained_real() -> SourceConnector:
    """Real SharePoint connector before any ``list_changes`` drain (empty envelope cache)."""
    connector: SourceConnector = FakeSharePointGraphApi(
        items=[{"id": "item-001", "content": b"body"}]
    ).build_connector()
    return connector


def _undrained_fake() -> SourceConnector:
    return FakeSourceConnector(
        name="undrained",
        events=[_make_event("item-001")],
        content={"item-001": b"body"},
    )


_UNDRAINED = [_undrained_real, _undrained_fake]


@pytest.mark.parametrize("factory", _UNDRAINED, ids=["real", "fake"])
def test_source_link_unavailable_envelope_falls_back_to_resolvable_link(
    factory: Callable[[], SourceConnector],
) -> None:
    """``unavailable`` failure class — when the source envelope for an
    item is unavailable (never drained / evicted from the per-tick
    cache), ``source_link`` does NOT raise: it falls back to a
    synthesised link that still names the item, so a citation is never
    lost. (No shipped connector raises from ``source_link``; the
    pipeline's handling of a connector that does is pinned in
    ``tests/unit/test_connector_pipeline.py``.)

    Sabotage proof: in ``kairix.connectors.sharepoint.connector.SharePointConnector.source_link``
    replace the final ``return f"sharepoint://items/{item_id}"`` with
    ``raise KeyError(item_id)``. Re-run: the real leg raises. Restored.
    """
    source = factory()
    link = source.source_link("item-001")
    assert "://" in link, f"link must carry a scheme; got {link!r}"
    assert "item-001" in link, f"fallback link must still name the item; got {link!r}"


# ---------------------------------------------------------------------------
# SourceConnector.sensitivity_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", _UNDRAINED, ids=["real", "fake"])
def test_sensitivity_for_returns_empty_envelope_falls_back_to_configured_tier(
    factory: Callable[[], SourceConnector],
) -> None:
    """``returns_empty`` failure class — with no envelope for the item
    (empty cache), ``sensitivity_for`` returns the connector's configured
    default tier rather than raising or returning nothing. (The pipeline's
    handling of a connector that DOES raise is pinned in
    ``tests/unit/test_connector_pipeline.py``.)

    Sabotage proof: in ``kairix.connectors.sharepoint.connector.SharePointConnector.sensitivity_for``
    return ``"public"`` instead of ``self._default_sensitivity``. Re-run:
    the real leg's ``== "internal"`` assertion fails. Restored.
    """
    source = factory()
    assert source.sensitivity_for("item-001") == "internal"


# ---------------------------------------------------------------------------
# SourceConnector.next_cursor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", _UNDRAINED, ids=["real", "fake"])
def test_next_cursor_returns_empty_before_first_drain(factory: Callable[[], SourceConnector]) -> None:
    """``returns_empty`` failure class — before any ``list_changes``
    drain there is no position to persist, so ``next_cursor`` returns
    ``None`` (the orchestrator then keeps the prior cursor instead of
    clobbering it). (The pipeline's handling of a connector whose
    ``next_cursor`` raises is pinned in
    ``tests/unit/test_connector_pipeline.py``.)

    Sabotage proof: in ``kairix.connectors.sharepoint.connector.SharePointConnector.next_cursor``
    return ``self._next_cursor or "{}"``. Re-run: the real leg's
    ``is None`` assertion fails. Restored.
    """
    source = factory()
    assert source.next_cursor() is None


# ---------------------------------------------------------------------------
# SourceConnector.metadata_for
# ---------------------------------------------------------------------------


def _metadata_empty_real() -> SourceConnector:
    """Real SharePoint connector — ``metadata_for`` on an uncached id is empty."""
    connector: SourceConnector = FakeSharePointGraphApi(
        items=[{"id": "item-001", "content": b"body-content"}],
    ).build_connector()
    return connector


def _metadata_empty_fake() -> SourceConnector:
    # No metadata mapping passed — every metadata_for call returns an
    # empty :class:`SourceMetadata`.
    return FakeSourceConnector(
        name="metadata-empty",
        events=[_make_event("item-001")],
        content={"item-001": b"body-content"},
    )


@pytest.mark.parametrize("factory", [_metadata_empty_real, _metadata_empty_fake], ids=["real", "fake"])
def test_metadata_for_returns_empty_when_item_not_in_scripted_map(factory: Callable[[], SourceConnector]) -> None:
    """``returns_empty`` failure class — when ``metadata_for`` has no
    data for an item (no error, just no envelope), it returns an empty
    :class:`SourceMetadata`; the chunk still indexes and the pipeline
    continues.

    Sabotage proof: in ``kairix.connectors.sharepoint.connector.SharePointConnector.metadata_for``
    return ``None`` instead of ``SourceMetadata()`` on a cache miss.
    Re-run: the real leg's ``isinstance(md, SourceMetadata)`` assertion
    fails. Restored.
    """
    from kairix.core.protocols import SourceMetadata

    db = sqlite3.connect(":memory:")
    create_schema(db)
    writer = FakeChunkWriter()
    source = factory()
    # Drive the empty path explicitly via the Protocol surface — proves
    # the empty-return shape is the canonical fallback.
    md = source.metadata_for("any-missing-id")
    assert isinstance(md, SourceMetadata)
    assert md.author is None
    pipeline = _build_pipeline(db, chunk_writer=writer)
    result = pipeline.run_batch(source, FakeExtractor())
    assert result.processed == 1
    assert len(writer.writes) == 1, f"writer should have received exactly one chunk batch; got {writer.writes!r}"
    db.close()
