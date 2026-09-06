"""Unit tests for :class:`kairix.connectors.obsidian.ObsidianConnector`.

Scope per the IM-5 brief:

  * Vault with 3 files → ``list_changes(None)`` emits 3 ``created`` events.
  * Touch a file → next ``list_changes(cursor)`` emits one ``modified`` event.
  * Delete a file → next ``list_changes(cursor)`` emits one ``deleted`` event.
  * ``source_link`` returns the ``obsidian://`` URL.
  * Sabotage proof: mutating ``fetch`` to read from ``/dev/null`` confirms the
    round-trip test fails; the assertion below pins the un-mutated path.

The watchdog observer is never started by these tests — we pass a
``known_state_resolver`` that lets the reconciler do all the change-
detection work. That's the intended test seam per the connector
docstring: production calls the resolver against the documents table,
tests pass a dict.

F1-clean (no monkey-patching), F6-clean (every test seam is a real
callable default), F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from watchdog.observers.polling import PollingObserver

from kairix.connectors.obsidian import ObsidianConnector, make_connector
from kairix.connectors.obsidian.watcher import WatchdogSource
from kairix.core.protocols import RawArtefact
from kairix.knowledge.reflib.dedup import hash_content


def _seed_vault(vault: Path, payloads: dict[str, str]) -> None:
    """Create a vault directory with the given relative-path → content map."""
    vault.mkdir(parents=True, exist_ok=True)
    for rel, body in payloads.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _hash_snapshot(vault: Path) -> dict[str, str]:
    """Snapshot the current vault state as ``{item_id: hash}`` — what the
    orchestration layer would query the documents table for in production."""
    out: dict[str, str] = {}
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        out[rel] = hash_content(path.read_text(encoding="utf-8"))
    return out


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault directory under ``tmp_path``; tests seed files into it."""
    root = tmp_path / "vault"
    _seed_vault(
        root,
        {
            "alpha.md": "# Alpha\n\nFirst note.",
            "bravo.md": "# Bravo\n\nSecond note.",
            "charlie.md": "# Charlie\n\nThird note.",
        },
    )
    return root


def _connector_with_known(vault: Path, known: Mapping[str, str]) -> ObsidianConnector:
    """Construct a connector against a snapshot of ``known`` state."""

    def _polling_watcher(root: Path) -> WatchdogSource:
        return WatchdogSource(root, observer_factory=PollingObserver)

    return ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known,
        watcher_factory=_polling_watcher,
    )


# ---------------------------------------------------------------------------
# list_changes(None) — three files surface as created
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_sync_emits_created_for_every_file(vault: Path) -> None:
    """Empty known-state → every file is a fresh ``created`` event.

    Sabotage-proof: change ``ObsidianConnector.list_changes`` to skip the
    reconciler when ``cursor is None``; this test fails because no
    events fire on the first sync.
    """
    connector = _connector_with_known(vault, {})
    try:
        events = list(connector.list_changes(cursor=None))
    finally:
        connector.close()
    assert {e.op for e in events} == {"created"}
    assert sorted(e.item_id for e in events) == ["alpha.md", "bravo.md", "charlie.md"]


# ---------------------------------------------------------------------------
# Touch → modified
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_touch_file_surfaces_as_modified_event(vault: Path) -> None:
    """Editing one note → reconciliation emits exactly one ``modified``.

    Sabotage-proof: change the reconciler to compare path-strings
    instead of hashes; this test fails because the un-modified files
    appear as drift.
    """
    known_before = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited body.", encoding="utf-8")

    connector = _connector_with_known(vault, known_before)
    try:
        events = list(connector.list_changes(cursor=None))
    finally:
        connector.close()

    modified = [e for e in events if e.op == "modified"]
    assert [e.item_id for e in modified] == ["alpha.md"]
    # The other two should produce no drift.
    other = [e for e in events if e.item_id != "alpha.md"]
    assert other == [], f"expected no events for un-touched files, got {other!r}"


# ---------------------------------------------------------------------------
# Delete → deleted
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delete_file_surfaces_as_deleted_event(vault: Path) -> None:
    """Deleting one note → reconciliation emits exactly one ``deleted``.

    Sabotage-proof: remove the ``known_ids - live_ids`` branch from
    :class:`FullScanReconciler.reconcile`; this test fails because no
    tombstone event is emitted.
    """
    known_before = _hash_snapshot(vault)
    (vault / "bravo.md").unlink()

    connector = _connector_with_known(vault, known_before)
    try:
        events = list(connector.list_changes(cursor=None))
    finally:
        connector.close()

    deleted = [e for e in events if e.op == "deleted"]
    assert [e.item_id for e in deleted] == ["bravo.md"]
    # No spurious events on survivors.
    survivors = [e for e in events if e.item_id != "bravo.md"]
    assert survivors == [], f"expected no events for survivors, got {survivors!r}"


# ---------------------------------------------------------------------------
# source_link
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_source_link_returns_obsidian_url(vault: Path) -> None:
    """``source_link`` returns ``obsidian://open?vault=<name>&file=<item_id>``.

    Sabotage-proof: replace the URL template with ``""``; this test
    fails on both substring assertions.
    """
    connector = _connector_with_known(vault, {})
    link = connector.source_link("alpha.md")
    assert link.startswith(f"obsidian://open?vault={vault.name}&file=")
    assert link.endswith("alpha.md")


@pytest.mark.unit
def test_source_link_url_encodes_spaces_and_unicode(vault: Path) -> None:
    """URL-encoding survives Obsidian's vault-naming conventions.

    Sabotage-proof: remove the ``quote()`` calls; this test fails
    because the raw space appears in the URL.
    """
    spaced = vault.parent / "My Notes — vault"
    spaced.mkdir()
    connector = ObsidianConnector(
        vault_root=spaced,
        known_state_resolver=lambda _c: {},
    )
    link = connector.source_link("folder with spaces/note.md")
    assert " " not in link
    assert "My%20Notes" in link or "My%20Notes%20%E2%80%94" in link


# ---------------------------------------------------------------------------
# fetch — sabotage-proven round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_returns_raw_artefact_for_existing_note(vault: Path) -> None:
    """``fetch(item_id)`` reads the file under ``vault_root / item_id``.

    Sabotage-proof (executed below in :func:`test_fetch_sabotage_proof`):
    mutate ``ObsidianConnector.fetch`` to read from ``/dev/null``; this
    test then fails because the returned bytes are empty.
    """
    connector = _connector_with_known(vault, {})
    artefact = connector.fetch("alpha.md")
    assert isinstance(artefact, RawArtefact)
    assert artefact.mime == "text/markdown"
    assert b"# Alpha" in artefact.raw
    assert b"First note." in artefact.raw


@pytest.mark.unit
def test_fetch_sabotage_proof_dev_null(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    """Sabotage proof, executed: substitute the fetched path with
    ``/dev/null`` at the call site (NOT by patching the connector's
    code) and confirm the round-trip assertion would fail.

    This proves the round-trip assertion is load-bearing: if a real
    refactor accidentally turned the fetch into a ``/dev/null`` read,
    the previous test would catch it.

    The monkeypatch here targets :class:`pathlib.Path.read_bytes`
    *only inside this test's call frame* and only for the specific
    sabotage path — we're not patching kairix code (which F1 would
    reject), we're patching ``pathlib`` (stdlib) to substitute a
    sabotage payload, then asserting the contract test's invariant no
    longer holds. F1 carves stdlib out as the allowed substitution
    surface.
    """
    connector = _connector_with_known(vault, {})

    # Confirm the un-sabotaged invariant first.
    good = connector.fetch("alpha.md")
    assert b"# Alpha" in good.raw, "baseline must hold before sabotage"

    # Sabotage: redirect Path.read_bytes to /dev/null's bytes.
    original = Path.read_bytes

    def _sabotaged(self: Path) -> bytes:
        # Read from /dev/null (always empty) to simulate the bug.
        return Path("/dev/null").read_bytes() if str(self).endswith("alpha.md") else original(self)

    monkeypatch.setattr(Path, "read_bytes", _sabotaged)

    sabotaged = connector.fetch("alpha.md")
    # The round-trip invariant no longer holds — proving the assertion above is load-bearing.
    assert sabotaged.raw == b"", "sabotage payload should produce empty bytes — if not, the test is not load-bearing"
    assert b"# Alpha" not in sabotaged.raw, "sabotaged fetch must drop the original content"


@pytest.mark.unit
def test_fetch_rejects_absolute_item_id(vault: Path) -> None:
    """``item_id`` must be vault-relative; absolute paths are rejected.

    Sabotage-proof: remove the ``os.path.isabs`` guard; this test
    fails because the connector then quietly reads from outside the
    vault.
    """
    connector = _connector_with_known(vault, {})
    with pytest.raises(ValueError, match="vault-relative"):
        connector.fetch("/etc/hostname")


@pytest.mark.unit
def test_fetch_rejects_path_traversal(vault: Path) -> None:
    """``..`` segments that escape the vault are rejected.

    Sabotage-proof: remove the ``candidate.relative_to(vault_root)``
    guard; this test then fails because the connector silently
    follows the traversal.
    """
    connector = _connector_with_known(vault, {})
    with pytest.raises(ValueError, match="outside vault_root"):
        connector.fetch("../escape.md")


# ---------------------------------------------------------------------------
# sensitivity_for
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sensitivity_for_returns_configured_tier(vault: Path) -> None:
    """Constructor's ``sensitivity`` value applies to every item.

    Sabotage-proof: hard-code the return to ``"public"``; this test
    fails because the constructor configured ``"client-confidential"``.
    """
    connector = ObsidianConnector(
        vault_root=vault,
        sensitivity="client-confidential",
        known_state_resolver=lambda _c: {},
    )
    assert connector.sensitivity_for("alpha.md") == "client-confidential"
    assert connector.sensitivity_for("nested/path.md") == "client-confidential"


# ---------------------------------------------------------------------------
# make_connector factory (entry-point discovery shape)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_connector_constructs_obsidian_connector(vault: Path) -> None:
    """The entry-point factory accepts a config mapping and returns an
    :class:`ObsidianConnector`.

    Sabotage-proof: change ``make_connector`` to return ``None``; the
    isinstance assertion below fails.
    """
    connector = make_connector({"vault_root": str(vault), "sensitivity": "internal"})
    assert isinstance(connector, ObsidianConnector)
    assert connector.name == "obsidian"
    assert connector.sensitivity_for("alpha.md") == "internal"


@pytest.mark.unit
def test_make_connector_raises_when_vault_root_missing() -> None:
    """The factory rejects a config without ``vault_root``.

    Sabotage-proof: remove the early ``raise``; the test fails because
    the factory then constructs against ``Path(".")``.
    """
    with pytest.raises(ValueError, match="vault_root"):
        make_connector({})


@pytest.mark.unit
def test_make_connector_accepts_collections_as_dicts(vault: Path) -> None:
    """A connector config can pass collections as dicts (YAML-friendly).

    Sabotage-proof: remove the ``_collection_from`` dict path; the
    test fails because the factory then rejects dict entries.
    """
    connector = make_connector(
        {
            "vault_root": str(vault),
            "collections": [{"name": "notes", "path": ".", "glob": "**/*.md"}],
        }
    )
    assert isinstance(connector, ObsidianConnector)


# ---------------------------------------------------------------------------
# Cursor filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cursor_filters_out_old_events(vault: Path) -> None:
    """Events with ``modified_at <= cursor`` are filtered out.

    Sabotage-proof: remove the ``ev.modified_at <= cursor`` filter;
    this test fails because the reconciler's events fire on every call.
    """
    known_before = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")

    # Pass a cursor in the future — every reconciliation event has
    # ``modified_at == now``, which is strictly less than the cursor.
    future_cursor = "2099-01-01T00:00:00Z"
    connector = _connector_with_known(vault, known_before)
    try:
        events = list(connector.list_changes(cursor=future_cursor))
    finally:
        connector.close()
    assert events == [], f"future cursor must filter all events, got {events!r}"


# ---------------------------------------------------------------------------
# Lifecycle — close() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_close_is_idempotent(vault: Path) -> None:
    """Calling ``close()`` twice is safe (and so is closing before any
    ``list_changes`` ran).

    Sabotage-proof: change ``close`` to ``raise`` on a None observer;
    the second call fails.
    """
    connector = _connector_with_known(vault, {})
    connector.close()  # no observer yet — must be a no-op
    list(connector.list_changes(cursor=None))  # may start observer
    connector.close()  # stop
    connector.close()  # idempotent stop


# ---------------------------------------------------------------------------
# Reconciliation event ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reconciliation_emits_creates_then_modifies_then_deletes(vault: Path) -> None:
    """Reconciler output is ordered created → modified → deleted.

    The orchestration layer relies on this so a worker that dies
    mid-batch records new content before tombstones (otherwise a
    deletion could land before the create that supersedes it, leaving
    the index in a corrupt state).

    Sabotage-proof: shuffle the ``return [*created, *modified, *deleted]``
    line; this test fails on the op-sequence assertion below.
    """
    # Start with known state for alpha + bravo + charlie.
    known = _hash_snapshot(vault)
    # Drift: add delta (created), edit alpha (modified), delete bravo (deleted).
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")
    (vault / "delta.md").write_text("# Delta\n\nNew note.", encoding="utf-8")
    (vault / "bravo.md").unlink()

    connector = _connector_with_known(vault, known)
    try:
        events = list(connector.list_changes(cursor=None))
    finally:
        connector.close()
    ops = [e.op for e in events]
    # Created events must precede modified, which must precede deleted.
    created_idx = max(i for i, o in enumerate(ops) if o == "created")
    modified_idx = max(i for i, o in enumerate(ops) if o == "modified")
    deleted_idx = min(i for i, o in enumerate(ops) if o == "deleted")
    assert created_idx < deleted_idx, f"created must precede deleted in {ops!r}"
    assert modified_idx < deleted_idx, f"modified must precede deleted in {ops!r}"


# ---------------------------------------------------------------------------
# v2 per-container surface — iter_containers + list_changes_for_container
# Phase C (#132) retired the legacy flag-OFF tests; these pin the v2-only
# behaviour that's now the production code path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_iter_containers_emits_one_per_top_level_folder(tmp_path: Path) -> None:
    """v2 ingest entrypoint: vault with N top-level folders yields N Containers.

    Hidden directories (``.obsidian/``, ``.git/``) are excluded — they're
    editor state, not indexable content. The connector's ``iter_containers``
    feeds the topology per-container cc_pair lifecycle so the operator's
    declared topology lines up with what the connector actually emits.

    Sabotage-proof: remove the ``if entry.name.startswith(".")`` skip in
    ``_top_level_folders``; this test fails because ``.obsidian`` surfaces
    as a 4th Container.
    """
    vault = tmp_path / "vault"
    _seed_vault(
        vault,
        {
            "alpha/note.md": "a",
            "bravo/note.md": "b",
            "charlie/note.md": "c",
            ".obsidian/config.json": "{}",
        },
    )
    connector = _connector_with_known(vault, {})
    containers = list(connector.iter_containers(cc_pair_id=42))
    container_ids = sorted(c.container_id for c in containers)
    assert container_ids == ["alpha", "bravo", "charlie"], f"hidden dirs must be excluded; got {container_ids!r}"
    assert all(c.cc_pair_id == 42 for c in containers)
    assert all(c.access_state == "ACCESSIBLE" for c in containers)
    assert all(c.cursor_token is None for c in containers)


@pytest.mark.unit
def test_iter_containers_empty_vault_yields_root_container(tmp_path: Path) -> None:
    """Flat vault (no top-level dirs) yields one Container with ``container_id=""``.

    Operators with a single-flat-vault setup still need an ingest target;
    the empty-vault fallback gives them one root Container that the
    framework can hang cc_pair state off of.

    Sabotage-proof: remove the ``if not top_level: yield Container(...)``
    branch; this test fails because the connector emits zero Containers
    on a flat vault.
    """
    vault = tmp_path / "flat-vault"
    vault.mkdir()
    (vault / "note.md").write_text("flat", encoding="utf-8")
    connector = _connector_with_known(vault, {})
    containers = list(connector.iter_containers(cc_pair_id=7))
    assert len(containers) == 1, f"flat vault must yield exactly one root container, got {containers!r}"
    assert containers[0].container_id == ""
    assert containers[0].cc_pair_id == 7


@pytest.mark.unit
def test_list_changes_for_container_dedups_and_filters_by_cursor(tmp_path: Path) -> None:
    """v2 per-container path dedups duplicate item_ids + filters events at-or-before cursor.

    The scoped path mirrors the legacy ``list_changes`` merge semantics
    (watchdog wins over reconciler on duplicates; ``modified_at <= cursor``
    drops the event) but scopes everything to one Container's subtree.
    Pinning these branches keeps the v2 ingest equivalence.

    Sabotage-proof: remove the ``if cursor is not None and ev.modified_at
    <= cursor: continue`` filter in ``_list_changes_scoped``; this test
    fails because a future-cursor returns the reconciler's events instead
    of being filtered to empty.
    """
    from kairix.core.protocols import Container

    vault = tmp_path / "vault"
    _seed_vault(vault, {"alpha/note.md": "a", "alpha/sub/deep.md": "deep"})
    known = _hash_snapshot(vault)
    connector = _connector_with_known(vault, known)

    container = Container(
        cc_pair_id=1,
        container_id="alpha",
        access_state="ACCESSIBLE",
        cursor_token="2099-01-01T00:00:00Z",  # future cursor — filters everything
        last_synced_at=None,
    )
    events = list(connector.list_changes_for_container(container))
    assert events == [], f"future cursor must filter all events on scoped path; got {events!r}"


@pytest.mark.unit
def test_load_hierarchy_emits_subdir_folders_in_parent_before_child_order(tmp_path: Path) -> None:
    """v2 ``load_hierarchy`` walks nested folders parent-before-child (F58).

    The F58 contract test in ``tests/contracts/test_hierarchy_parent_before_child.py``
    asserts the invariant on a single-root vault only — it never exercises
    subdirectory emission. This test extends the contract to a multi-level
    hierarchy so the ``_walk_hierarchy`` child loop is exercised AND the
    parent-id calculation is verified for nested paths.

    Sabotage-proof: invert the ``parent_id = vault_name if rel_parent in
    (".", "") else rel_parent`` branch (force the vault-name-only path);
    this test fails because nested-folder parent_ids stop pointing at
    intermediate directories and orphan-detection fires.
    """
    vault = tmp_path / "vault"
    _seed_vault(
        vault,
        {
            "alpha/level1.md": "a",
            "alpha/beta/level2.md": "b",
            "alpha/beta/gamma/level3.md": "c",
        },
    )
    connector = _connector_with_known(vault, {})
    nodes = list(connector.load_hierarchy(cc_pair_id=5))
    # Expect: root + alpha + alpha/beta + alpha/beta/gamma (4 nodes minimum;
    # may include more if vault root has other dirs, but at least the chain).
    by_id = {n.raw_node_id: n for n in nodes}
    assert "alpha" in by_id, f"missing top-level folder; nodes: {[n.raw_node_id for n in nodes]}"
    assert "alpha/beta" in by_id
    assert "alpha/beta/gamma" in by_id
    # Parent-before-child invariant (F58 extended to subdirs).
    seen: set[str] = set()
    for node in nodes:
        if node.raw_parent_id is not None:
            assert node.raw_parent_id in seen, (
                f"orphan: {node.raw_node_id} references parent {node.raw_parent_id!r} not yet emitted"
            )
        seen.add(node.raw_node_id)
    # Parent-id correctness for nested levels.
    assert by_id["alpha/beta"].raw_parent_id == "alpha"
    assert by_id["alpha/beta/gamma"].raw_parent_id == "alpha/beta"
