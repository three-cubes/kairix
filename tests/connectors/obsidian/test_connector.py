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

import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

# Imported from the defining module (not the package re-export) so the
# mutation-parity import graph (scripts/checks/mutation_parity.py) selects
# this file as connector.py's own test when mutating that module.
from kairix.connectors.obsidian.connector import ObsidianConnector, make_connector
from kairix.connectors.obsidian.watcher import FileChange, WatchdogSource
from kairix.core.db.scanner import CollectionConfig
from kairix.core.protocols import Container, RawArtefact
from kairix.knowledge.reflib.dedup import hash_content
from tests.fakes import FakeWatchdogObserver, fake_obsidian_watcher_factory

# A cursor before any event the tests produce, so a tick runs with
# ``cursor is not None`` without filtering anything out.
_PAST_CURSOR = "2000-01-01T00:00:00Z"


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
    """Construct a connector against a snapshot of ``known`` state.

    The watcher runs on the in-process fake observer (no OS thread): these
    tests pin reconciler behaviour, and a real observer is a thread that
    outlives any test that does not close the connector.
    """
    return ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known,
        watcher_factory=fake_obsidian_watcher_factory(),
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
    with _connector_with_known(vault, {}) as connector:
        events = list(connector.list_changes(cursor=None))
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

    with _connector_with_known(vault, known_before) as connector:
        events = list(connector.list_changes(cursor=None))

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

    with _connector_with_known(vault, known_before) as connector:
        events = list(connector.list_changes(cursor=None))

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

    Sabotage-proof: make ``_after_cursor`` return ``events`` unfiltered;
    this test fails because the reconciler's events fire on every call.
    """
    known_before = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")

    # Pass a cursor in the future — every reconciliation event has
    # ``modified_at == now``, which is strictly less than the cursor.
    # ``reconcile_every=1`` makes this call reconcile, so the drift on
    # ``alpha.md`` reaches the cursor filter (and the past-cursor control
    # proves it is the filter, not a skipped reconcile, that empties it).
    future_cursor = "2099-01-01T00:00:00Z"
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known_before,
        watcher_factory=fake_obsidian_watcher_factory(),
        reconcile_every=1,
    )
    with connector:
        assert [e.item_id for e in connector.list_changes(cursor=_PAST_CURSOR)] == ["alpha.md"]
        events = list(connector.list_changes(cursor=future_cursor))
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

    with _connector_with_known(vault, known) as connector:
        events = list(connector.list_changes(cursor=None))
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
    (one event per item_id; ``modified_at <= cursor`` drops the event) but scopes everything to one Container's subtree.
    Pinning these branches keeps the v2 ingest equivalence.

    Sabotage-proof: make ``_after_cursor`` return ``events`` unfiltered;
    this test
    fails because a future-cursor returns the reconciler's events instead
    of being filtered to empty.
    """

    vault = tmp_path / "vault"
    _seed_vault(vault, {"alpha/note.md": "a", "alpha/sub/deep.md": "deep"})
    known = _hash_snapshot(vault)
    # Drift the reconciler will report, so the cursor filter has work to do.
    (vault / "alpha" / "note.md").write_text("edited", encoding="utf-8")
    connector = _connector_with_known(vault, known)

    container = Container(
        cc_pair_id=1,
        container_id="alpha",
        access_state="ACCESSIBLE",
        cursor_token="2099-01-01T00:00:00Z",  # future cursor — filters everything
        last_synced_at=None,
    )
    with connector:
        unfiltered = list(connector.list_changes_for_container(replace(container, cursor_token=None)))
        events = list(connector.list_changes_for_container(container))
    assert [e.item_id for e in unfiltered] == ["alpha/note.md"]
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


# ---------------------------------------------------------------------------
# metadata_for — frontmatter author / tags normalisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("frontmatter", "expected_author", "expected_tags"),
    [
        # Happy path: a non-blank author string and a list of tags.
        ("author: agent-alpha\ntags: [alpha, beta]", "agent-alpha", ("alpha", "beta")),
        # Whitespace-only author is "no author", not an empty string.
        ('author: "   "', None, ()),
        # Non-string author (a YAML int) is ignored, never ``.strip()``-ed.
        ("author: 42", None, ()),
        # Tag list: blank entries dropped.
        ('tags: [alpha, "   "]', None, ("alpha",)),
        # Tag list: non-string entries dropped, never ``.strip()``-ed.
        ("tags: [alpha, 7]", None, ("alpha",)),
        # A single non-blank tag string becomes a one-tuple.
        ("tags: solo", None, ("solo",)),
        # A whitespace-only tag string yields no tags.
        ('tags: "   "', None, ()),
        # Non-list, non-string tags (a YAML int) yield no tags.
        ("tags: 5", None, ()),
    ],
)
def test_metadata_for_normalises_frontmatter_author_and_tags(
    tmp_path: Path,
    frontmatter: str,
    expected_author: str | None,
    expected_tags: tuple[str, ...],
) -> None:
    """``metadata_for`` surfaces only non-blank string authors / tags.

    Sabotage-proof (executed): flipping any ``isinstance(...) and
    x.strip()`` guard in ``_frontmatter_author_tags`` to ``or`` either
    lets a blank value through (``""`` author, ``("   ",)`` / ``("",)``
    tags) or calls ``.strip()`` on a non-string (``AttributeError``) —
    each guard is pinned by at least one case below.
    """
    vault = tmp_path / "vault"
    _seed_vault(vault, {"note.md": f"---\n{frontmatter}\n---\n# Note\n\nBody."})
    connector = _connector_with_known(vault, {})

    meta = connector.metadata_for("note.md")

    assert meta.author == expected_author
    assert meta.tags == expected_tags
    assert meta.modified_at is not None


# ---------------------------------------------------------------------------
# Watchdog + reconciler merge rule — one event per item_id
# ---------------------------------------------------------------------------


class _ReplayingObserver(FakeWatchdogObserver):
    """Fake observer that replays queued events the moment it starts.

    Models the macOS FSEvents observer delivering history for files written
    just before the stream started: the replayed events land in the queue
    before the connector's first drain, deterministically.
    """

    def __init__(self, replay: list[Any]) -> None:
        super().__init__()
        self._replay = replay

    def start(self) -> None:
        super().start()
        for event in self._replay:
            self.emit(event)


def _replaying_connector(vault: Path, known: Mapping[str, str], replay: list[Any]) -> ObsidianConnector:
    def _factory(root: Path) -> WatchdogSource:
        return WatchdogSource(root, observer_factory=lambda: _ReplayingObserver(replay))

    return ObsidianConnector(vault_root=vault, known_state_resolver=lambda _c: known, watcher_factory=_factory)


@pytest.mark.unit
def test_watchdog_edit_then_delete_between_ticks_emits_tombstone(vault: Path) -> None:
    """A note edited then deleted between ticks surfaces as ``deleted``, not ``modified``.

    No reconcile runs on the second tick, so the watchdog drain alone
    decides; its run for ``bravo.md`` must collapse to the LAST op.

    Sabotage-proof: make the watchdog loop in ``_merge_change_events`` keep
    ``prior`` (first event wins); this test fails because ``modified`` is
    emitted for a file that no longer exists.
    """
    observers: list[FakeWatchdogObserver] = []
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: _hash_snapshot(vault),
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=100,
    )
    with connector:
        assert list(connector.list_changes(cursor=None)) == []
        bravo = str(vault / "bravo.md")
        (vault / "bravo.md").write_text("# Bravo\n\nEdited.", encoding="utf-8")
        observers[0].emit(FileModifiedEvent(bravo))
        (vault / "bravo.md").unlink()
        observers[0].emit(FileDeletedEvent(bravo))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "bravo.md")]


@pytest.mark.unit
def test_watchdog_create_then_edit_between_ticks_stays_created(vault: Path) -> None:
    """A note created then edited between ticks surfaces once, as ``created``.

    Sabotage-proof: make ``_collapse_watchdog`` always take ``later.op``;
    this test fails because the op becomes ``modified``.
    """
    observers: list[FakeWatchdogObserver] = []
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: _hash_snapshot(vault),
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=100,
    )
    with connector:
        assert list(connector.list_changes(cursor=None)) == []
        delta = str(vault / "delta.md")
        (vault / "delta.md").write_text("# Delta", encoding="utf-8")
        observers[0].emit(FileCreatedEvent(delta))
        observers[0].emit(FileModifiedEvent(delta))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("created", "delta.md")]


@pytest.mark.unit
def test_cold_start_replayed_create_does_not_mask_reconciled_delete(vault: Path) -> None:
    """A replayed FSEvents ``created`` for a deleted note loses to the reconciler's tombstone.

    Sabotage-proof: make the reconcile loop in ``_merge_change_events`` keep
    ``prior`` when the watchdog already reported the item; this test fails
    because ``created bravo.md`` is emitted with no ``deleted`` event.
    """
    known = _hash_snapshot(vault)
    (vault / "bravo.md").unlink()
    replay = [FileCreatedEvent(str(vault / "bravo.md"))]

    with _replaying_connector(vault, known, replay) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "bravo.md")]


@pytest.mark.unit
def test_cold_start_replayed_create_does_not_mask_reconciled_modify(vault: Path) -> None:
    """The reconciler's ``modified`` verdict wins over a replayed ``created``.

    Sabotage-proof: same mutation as the delete case (reconcile loop keeps
    ``prior``); this test fails because the op is ``created``.
    """
    known = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")
    replay = [FileCreatedEvent(str(vault / "alpha.md"))]

    with _replaying_connector(vault, known, replay) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("modified", "alpha.md")]


@pytest.mark.unit
def test_container_scoped_path_reconciled_delete_beats_replayed_create(tmp_path: Path) -> None:
    """The per-container path applies the same merge rule as ``list_changes``.

    Sabotage-proof: point ``_list_changes_scoped`` back at a first-event-wins
    dedup; this test fails because ``created alpha/gone.md`` is emitted.
    """
    vault = tmp_path / "vault"
    _seed_vault(vault, {"alpha/note.md": "a", "alpha/gone.md": "g"})
    known = _hash_snapshot(vault)
    (vault / "alpha" / "gone.md").unlink()
    replay = [FileCreatedEvent(str(vault / "alpha" / "gone.md"))]
    container = Container(
        cc_pair_id=1,
        container_id="alpha",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )

    with _replaying_connector(vault, known, replay) as connector:
        events = list(connector.list_changes_for_container(container))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha/gone.md")]


@pytest.mark.unit
def test_watchdog_create_then_delete_between_ticks_emits_tombstone(vault: Path) -> None:
    """A scratch note created then deleted between ticks surfaces as ``deleted``.

    Sabotage-proof: change the ``and`` in ``_collapse_watchdog`` to ``or``;
    this test fails because the run keeps ``created`` for a file that is gone.
    """
    observers: list[FakeWatchdogObserver] = []
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: _hash_snapshot(vault),
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=100,
    )
    with connector:
        assert list(connector.list_changes(cursor=None)) == []
        scratch = str(vault / "scratch.md")
        (vault / "scratch.md").write_text("tmp", encoding="utf-8")
        observers[0].emit(FileCreatedEvent(scratch))
        (vault / "scratch.md").unlink()
        observers[0].emit(FileDeletedEvent(scratch))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "scratch.md")]


class _PinnedStampSource(WatchdogSource):
    """``WatchdogSource`` over the fake observer whose drained events all carry ``stamp``.

    Events still arrive through :meth:`FakeWatchdogObserver.emit` and the real
    queueing handler; only ``observed_at`` is pinned so a test can place an
    event exactly on the cursor.
    """

    def __init__(self, root: Path, observers: list[FakeWatchdogObserver], stamp: str) -> None:
        def _observer() -> FakeWatchdogObserver:
            observer = FakeWatchdogObserver()
            observers.append(observer)
            return observer

        super().__init__(root, observer_factory=_observer)
        self._stamp = stamp

    def drain(self) -> list[FileChange]:
        return [replace(c, observed_at=self._stamp) for c in super().drain()]


@pytest.mark.unit
def test_watchdog_event_exactly_at_cursor_is_filtered(vault: Path) -> None:
    """An event stamped exactly at the cursor was already processed — it is dropped.

    Sabotage-proof: change ``>`` to ``>=`` in ``_after_cursor``; this test
    fails because the at-cursor event is emitted again.
    """
    stamp = "2026-01-01T00:00:00Z"
    observers: list[FakeWatchdogObserver] = []
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: _hash_snapshot(vault),
        watcher_factory=lambda root: _PinnedStampSource(root, observers, stamp),
        reconcile_every=100,
    )
    alpha = str(vault / "alpha.md")
    with connector:
        assert list(connector.list_changes(cursor=None)) == []
        observers[0].emit(FileModifiedEvent(alpha))
        at_cursor = list(connector.list_changes(cursor=stamp))
        observers[0].emit(FileModifiedEvent(alpha))
        before_cursor = list(connector.list_changes(cursor="2025-12-31T23:59:59Z"))

    assert at_cursor == []
    assert [(e.op, e.item_id) for e in before_cursor] == [("modified", "alpha.md")]


def _watchdog_only_connector(vault: Path, observers: list[FakeWatchdogObserver], **kwargs: Any) -> ObsidianConnector:
    """Connector whose ticks after the first never reconcile — the watchdog alone decides."""
    return ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: _hash_snapshot(vault),
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=100,
        **kwargs,
    )


@pytest.mark.unit
def test_watchdog_create_for_missing_file_settles_to_deleted(vault: Path) -> None:
    """A watchdog ``created`` for a file that no longer exists is emitted as ``deleted``.

    Models a late or replayed event on a tick with no reconcile, so the
    connector never hands the pipeline a fetch that would dead-letter.

    Sabotage-proof: make ``_settle_against_fs`` return ``ev`` unchanged;
    this test fails because ``created bravo.md`` is emitted.
    """
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        (vault / "bravo.md").unlink()
        observers[0].emit(FileCreatedEvent(str(vault / "bravo.md")))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "bravo.md")]


@pytest.mark.unit
def test_watchdog_delete_for_existing_file_settles_to_modified(vault: Path) -> None:
    """A watchdog ``deleted`` for a file that exists again is emitted as ``modified``.

    Sabotage-proof: drop the ``live and ev.op == "deleted"`` branch of
    ``_settle_against_fs``; this test fails because the live note is
    tombstoned.
    """
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        observers[0].emit(FileDeletedEvent(str(vault / "alpha.md")))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("modified", "alpha.md")]


@pytest.mark.unit
def test_watchdog_events_outside_collections_are_dropped(vault: Path) -> None:
    """Editor state, non-matching extensions and excluded paths never surface.

    The reconciler only walks the configured collections; the watchdog sees
    the whole vault. Without the filter, every Obsidian ``workspace.json``
    write became a change event.

    Sabotage-proof: make ``_filter_to_specs`` return ``changes`` unchanged;
    this test fails because the out-of-scope paths are emitted.
    """
    _seed_vault(
        vault,
        {".obsidian/workspace.json": "{}", "notes/wip/draft.md": "d", "notes/kept.md": "k", "notes/data.csv": "c"},
    )
    observers: list[FakeWatchdogObserver] = []
    collections = [CollectionConfig(name="notes", path="notes", exclude=["wip"])]
    with _watchdog_only_connector(vault, observers, collections=collections) as connector:
        list(connector.list_changes(cursor=None))
        for rel in (".obsidian/workspace.json", "notes/wip/draft.md", "notes/data.csv", "alpha.md", "notes/kept.md"):
            observers[0].emit(FileModifiedEvent(str(vault / rel)))
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("modified", "notes/kept.md")]


@pytest.mark.unit
def test_container_scoped_watchdog_events_outside_collections_are_dropped(tmp_path: Path) -> None:
    """The per-container path applies the same collection filter to watchdog events.

    Sabotage-proof: drop the ``_filter_to_specs`` call in
    ``_list_changes_scoped``; this test fails because ``alpha/data.json``
    is emitted.
    """
    vault = tmp_path / "vault"
    _seed_vault(vault, {"alpha/note.md": "a", "alpha/data.json": "{}"})
    container = Container(
        cc_pair_id=1,
        container_id="alpha",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        list(connector.list_changes_for_container(container))
        (vault / "alpha" / "note.md").write_text("edited", encoding="utf-8")
        for rel in ("alpha/data.json", "alpha/note.md"):
            observers[0].emit(FileModifiedEvent(str(vault / rel)))
        events = list(connector.list_changes_for_container(container))

    assert [(e.op, e.item_id) for e in events] == [("modified", "alpha/note.md")]


# ---------------------------------------------------------------------------
# Settlement of every merged winner (PLA-550)
# ---------------------------------------------------------------------------


class _ChangesFileOnRead(Mapping[str, str]):
    """Known state whose first read deletes (or recreates) one vault file.

    The reconciler walks the vault and only then reads ``known_state``, so a
    change made on that first read lands after the walk and before the merge —
    a deterministic stand-in for a note deleted or recreated mid-tick.
    """

    def __init__(self, known: Mapping[str, str], target: Path, *, recreate_with: str | None = None) -> None:
        self._known = dict(known)
        self._target = target
        self._recreate_with = recreate_with
        self._fired = False

    def _fire(self) -> None:
        if self._fired:
            return
        self._fired = True
        if self._recreate_with is None:
            self._target.unlink()
        else:
            self._target.write_text(self._recreate_with, encoding="utf-8")

    def __getitem__(self, key: str) -> str:
        self._fire()
        return self._known[key]

    def __iter__(self) -> Iterator[str]:
        self._fire()
        return iter(self._known)

    def __len__(self) -> int:
        return len(self._known)


def _reconciling_connector(vault: Path, known: Mapping[str, str], **kwargs: Any) -> ObsidianConnector:
    return ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known,
        watcher_factory=fake_obsidian_watcher_factory(),
        **kwargs,
    )


@pytest.mark.unit
def test_note_deleted_after_the_walk_settles_the_reconciler_winner_to_deleted(vault: Path) -> None:
    """A watchdog event and a reconciler ``modified`` for a note deleted after the walk tombstone it.

    Sabotage-proof: settle only the watchdog events, before the reconciler
    merges (the PLA-547 order); this test fails because the reconciler's
    stale ``modified`` overwrites the settled ``deleted``.
    """
    known = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")
    racing = _ChangesFileOnRead(known, vault / "alpha.md")

    with _replaying_connector(vault, racing, [FileModifiedEvent(str(vault / "alpha.md"))]) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]


@pytest.mark.unit
def test_note_deleted_after_the_walk_tombstones_without_a_watchdog_event(vault: Path) -> None:
    """A reconciler-only ``modified`` for a note deleted after the walk is emitted as ``deleted``.

    Sabotage-proof: return the merged winners without ``_settle_against_fs``
    for reconciler events; this test fails because ``modified`` is emitted.
    """
    known = _hash_snapshot(vault)
    (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")

    with _reconciling_connector(vault, _ChangesFileOnRead(known, vault / "alpha.md")) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]


@pytest.mark.unit
def test_note_recreated_after_the_walk_is_tombstoned_then_recreated_next_tick(vault: Path) -> None:
    """A reconciler tombstone stands this tick; the watchdog re-creates the note on the next.

    The merge never reverses a reconciler ``deleted``: existence alone does
    not prove the scan can reach a file. Recreating the note queues a
    watchdog ``created``, which brings it back one tick later.

    Sabotage-proof: make ``_settle_reconciled`` turn a tombstone whose note
    is ``"present"`` or ``"revivable"`` into ``modified``; this test fails because the first tick emits
    ``modified`` instead of keeping the reconciler's tombstone.
    """
    known = _hash_snapshot(vault)
    (vault / "bravo.md").unlink()
    racing = _ChangesFileOnRead(known, vault / "bravo.md", recreate_with="# Bravo\n\nBack.")
    observers: list[FakeWatchdogObserver] = []
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: racing,
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=100,
    )
    with connector:
        first = list(connector.list_changes(cursor=None))
        observers[0].emit(FileCreatedEvent(str(vault / "bravo.md")))
        second = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in first] == [("deleted", "bravo.md")]
    assert [(e.op, e.item_id) for e in second] == [("created", "bravo.md")]


@pytest.mark.unit
def test_tombstone_under_a_directory_the_scan_no_longer_traverses_stands(vault: Path) -> None:
    """A note under a directory replaced by a symlink keeps the reconciler's tombstone.

    ``Path.glob("**/*.md")`` does not traverse directory symlinks, so the
    walk stops reporting the note and the reconciler tombstones it, though
    the file is still reachable on disk — inside the vault, so only the
    never-reverse rule (not the vault boundary) keeps the tombstone.

    Sabotage-proof: make ``_settle_reconciled`` turn a tombstone whose note
    is ``"present"`` or ``"revivable"`` into ``modified``; this test fails because ``modified`` is emitted.
    """
    _seed_vault(vault, {"notes/deep.md": "d"})
    known = _hash_snapshot(vault)
    (vault / "notes").rename(vault / "archive")
    (vault / "notes").symlink_to(vault / "archive", target_is_directory=True)

    with _reconciling_connector(vault, known) as connector:
        events = list(connector.list_changes(cursor=None))

    # The scan now reaches the note only at its real path, as a new item.
    assert sorted((e.op, e.item_id) for e in events) == [("created", "archive/deep.md"), ("deleted", "notes/deep.md")]


@pytest.mark.unit
def test_note_left_on_disk_but_moved_out_of_scope_keeps_its_tombstone(vault: Path) -> None:
    """A previously indexed note that a narrowed collection now excludes stays ``deleted``.

    The file is still on disk, so a settlement that only checked existence
    would turn the reconciler's tombstone into ``modified`` and re-index
    out-of-scope content.

    Sabotage-proof: make ``_settle_reconciled`` turn a tombstone whose note
    is ``"present"`` or ``"revivable"`` into ``modified``; this test fails because ``modified`` is emitted.
    """
    _seed_vault(vault, {"notes/kept.md": "k", "notes/WIP/draft.md": "d"})
    known = {k: v for k, v in _hash_snapshot(vault).items() if k.startswith("notes/")}
    collections = [CollectionConfig(name="notes", path="notes", exclude=["WIP/"])]

    with _reconciling_connector(vault, known, collections=collections) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "notes/WIP/draft.md")]


@pytest.mark.unit
def test_container_pass_leaves_other_containers_alone(tmp_path: Path) -> None:
    """A container pass tombstones only its own notes, given a connector-wide known state.

    The resolver API is cursor-only, so it can return every indexed note; the
    ``alpha`` walk must not read ``beta/other.md``'s absence from its own walk
    as a deletion. A note genuinely gone from ``alpha`` is still tombstoned.

    Sabotage-proof: pass the resolver's full mapping to ``reconcile`` in
    ``_list_changes_scoped`` (drop ``_known_in_container``); this test fails
    because ``deleted beta/other.md`` is emitted. Drop the trailing ``/`` in
    ``_in_container``; it fails because ``alpha-old/x.md`` counts as ``alpha``.
    """
    vault = tmp_path / "vault"
    _seed_vault(vault, {"alpha/note.md": "a", "alpha/gone.md": "g", "beta/other.md": "b", "alpha-old/x.md": "x"})
    known = _hash_snapshot(vault)
    (vault / "alpha" / "gone.md").unlink()

    with _reconciling_connector(vault, known) as connector:
        events = list(
            connector.list_changes_for_container(
                Container(
                    cc_pair_id=1,
                    container_id="alpha",
                    access_state="ACCESSIBLE",
                    cursor_token=None,
                    last_synced_at=None,
                )
            )
        )

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha/gone.md")]


@pytest.mark.unit
# Root bypasses directory permissions, so the liveness check cannot fail there;
# Windows has no POSIX mode bits to lock a directory with.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable directory",
)
def test_unreadable_directory_keeps_the_source_verdict_and_siblings(vault: Path) -> None:
    """A settlement check that fails keeps the event as reported and the tick going.

    ``stat`` under a directory that became unreadable raises
    ``PermissionError``; the merge must keep that item's watchdog verdict and
    still emit the sibling change, not fail the tick.

    Sabotage-proof: let ``_liveness`` re-raise the ``OSError`` instead of
    returning ``"unknown"``; this test fails with ``PermissionError``. Map it
    to ``"missing"`` instead; this test fails because the note is tombstoned.
    """
    _seed_vault(vault, {"locked/note.md": "n"})
    observers: list[FakeWatchdogObserver] = []
    locked = vault / "locked"
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        (vault / "alpha.md").write_text("# Alpha\n\nEdited.", encoding="utf-8")
        observers[0].emit(FileModifiedEvent(str(locked / "note.md")))
        observers[0].emit(FileModifiedEvent(str(vault / "alpha.md")))
        locked.chmod(0o000)
        try:
            events = list(connector.list_changes(cursor=_PAST_CURSOR))
        finally:
            locked.chmod(0o755)

    assert [(e.op, e.item_id) for e in events] == [("modified", "locked/note.md"), ("modified", "alpha.md")]


@pytest.mark.unit
def test_note_replaced_by_a_symlink_outside_the_vault_is_tombstoned(vault: Path) -> None:
    """A deleted note replaced by a symlink to a file outside the vault stays ``deleted``.

    ``stat`` follows the link and finds a regular file, but ``fetch`` refuses a
    path that resolves outside the vault, so reviving the tombstone would
    dead-letter the note and leave its old chunks searchable.

    Sabotage-proof: use ``self._vault_root / item_id`` in ``_liveness``
    instead of ``self._safe_resolve(item_id)``; this test fails because the
    tombstone becomes ``modified``.
    """
    outside = vault.parent / "outside.md"
    outside.write_text("# Outside", encoding="utf-8")
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        (vault / "alpha.md").unlink()
        observers[0].emit(FileDeletedEvent(str(vault / "alpha.md")))
        (vault / "alpha.md").symlink_to(outside)
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]


@pytest.mark.unit
def test_reconciled_note_now_a_symlink_outside_the_vault_is_tombstoned(vault: Path) -> None:
    """The walk follows a note's symlink out of the vault and reports ``modified``; it settles to ``deleted``.

    Sabotage-proof: use ``self._vault_root / item_id`` in ``_liveness``
    instead of ``self._safe_resolve(item_id)``; this test fails because
    ``modified`` is emitted for a note ``fetch`` refuses.
    """
    known = _hash_snapshot(vault)
    outside = vault.parent / "outside.md"
    outside.write_text("# Outside", encoding="utf-8")
    (vault / "alpha.md").unlink()
    (vault / "alpha.md").symlink_to(outside)

    with _reconciling_connector(vault, known) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]


# Root reads any file regardless of mode bits, so the read cannot fail there;
# Windows has no POSIX mode bits to lock a file with.
_NEEDS_UNREADABLE_FILE = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable file",
)


@pytest.mark.unit
@_NEEDS_UNREADABLE_FILE
def test_unreadable_indexed_note_keeps_the_watchdog_modification(vault: Path) -> None:
    """A note the walk cannot read is present, not deleted: the watchdog's ``modified`` stands.

    Sabotage-proof: let ``_discover`` skip an unreadable file as absent (drop
    it from ``unreadable``); this test fails because the reconciler's false
    tombstone wins the merge and the note's indexed content is retired.
    """
    known = _hash_snapshot(vault)
    alpha = vault / "alpha.md"
    alpha.chmod(0o000)
    try:
        with _replaying_connector(vault, known, [FileModifiedEvent(str(alpha))]) as connector:
            events = list(connector.list_changes(cursor=None))
    finally:
        alpha.chmod(0o644)

    assert [(e.op, e.item_id) for e in events] == [("modified", "alpha.md")]


@pytest.mark.unit
@_NEEDS_UNREADABLE_FILE
def test_unreadable_indexed_note_is_never_tombstoned_by_reconciliation(vault: Path) -> None:
    """Reconciliation alone emits nothing for an indexed note it cannot read.

    Sabotage-proof: drop ``- unreadable`` from the ``deleted`` loop in
    ``FullScanReconciler.reconcile``; this test fails because ``deleted`` is
    emitted for a note that is still on disk.
    """
    known = _hash_snapshot(vault)
    alpha = vault / "alpha.md"
    alpha.chmod(0o000)
    try:
        with _reconciling_connector(vault, known) as connector:
            events = list(connector.list_changes(cursor=None))
    finally:
        alpha.chmod(0o644)

    assert events == []


@pytest.mark.unit
@_NEEDS_UNREADABLE_FILE
def test_slim_docs_enumerate_unreadable_notes(vault: Path) -> None:
    """The prune cycle's enumeration includes a note it cannot read, so it is not pruned.

    Sabotage-proof: return ``sorted(live)`` from ``FullScanReconciler.item_ids``;
    this test fails because ``alpha.md`` is missing and would be pruned.
    """
    alpha = vault / "alpha.md"
    alpha.chmod(0o000)
    try:
        with _reconciling_connector(vault, {}) as connector:
            slim = list(
                connector.retrieve_all_slim_docs(
                    Container(
                        cc_pair_id=1, container_id="", access_state="ACCESSIBLE", cursor_token=None, last_synced_at=None
                    )
                )
            )
    finally:
        alpha.chmod(0o644)

    assert slim == ["alpha.md", "bravo.md", "charlie.md"]


@pytest.mark.unit
def test_root_container_pass_compares_every_known_note(vault: Path) -> None:
    """The root container (``""``) keeps the whole known state: in-sync notes emit nothing.

    Sabotage-proof: return ``False`` for the root container in
    ``_in_container``; this test fails because the empty known state makes
    every note ``created`` and the deletion is lost.
    """
    known = _hash_snapshot(vault)
    (vault / "bravo.md").unlink()

    with _reconciling_connector(vault, known) as connector:
        events = list(
            connector.list_changes_for_container(
                Container(
                    cc_pair_id=1, container_id="", access_state="ACCESSIBLE", cursor_token=None, last_synced_at=None
                )
            )
        )

    assert [(e.op, e.item_id) for e in events] == [("deleted", "bravo.md")]


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
_NEEDS_UNREADABLE_DIRECTORY = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable directory",
)


@pytest.mark.unit
@_NEEDS_UNREADABLE_DIRECTORY
def test_unlistable_directory_does_not_tombstone_over_a_watchdog_modification(vault: Path) -> None:
    """A walk that cannot list a directory proves no deletion; the watchdog's ``modified`` stands.

    Sabotage-proof: return ``winner`` for a tombstone in ``_settle_reconciled``
    regardless of ``state``; this test fails because the reconciler's false
    ``deleted`` retires the note.
    """
    _seed_vault(vault, {"locked/note.md": "n"})
    known = _hash_snapshot(vault)
    observers: list[FakeWatchdogObserver] = []
    locked = vault / "locked"
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known,
        watcher_factory=fake_obsidian_watcher_factory(observers),
        reconcile_every=1,
    )
    with connector:
        assert list(connector.list_changes(cursor=None)) == []
        (locked / "note.md").write_text("edited", encoding="utf-8")
        observers[0].emit(FileModifiedEvent(str(locked / "note.md")))
        locked.chmod(0o000)
        try:
            events = list(connector.list_changes(cursor=_PAST_CURSOR))
        finally:
            locked.chmod(0o755)

    assert [(e.op, e.item_id) for e in events] == [("modified", "locked/note.md")]


@pytest.mark.unit
@_NEEDS_UNREADABLE_DIRECTORY
def test_unlistable_directory_alone_emits_no_tombstone(vault: Path) -> None:
    """With no watchdog verdict, an unconfirmed reconciler tombstone is dropped, not emitted.

    Sabotage-proof: return ``winner`` for a tombstone in ``_settle_reconciled``
    regardless of ``state``; this test fails because ``deleted`` is emitted.
    """
    _seed_vault(vault, {"locked/note.md": "n"})
    known = _hash_snapshot(vault)
    locked = vault / "locked"
    connector = ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: known,
        watcher_factory=fake_obsidian_watcher_factory([]),
    )
    locked.chmod(0o000)
    try:
        with connector:
            events = list(connector.list_changes(cursor=None))
    finally:
        locked.chmod(0o755)

    assert events == []


@pytest.mark.unit
def test_watchdog_tombstone_under_a_symlinked_directory_is_not_revived(vault: Path) -> None:
    """A queued delete stays ``deleted`` when the note's parent became an in-vault directory symlink.

    The resolved target is a regular file inside the vault, but the scan's
    walk does not traverse directory symlinks, so the note is not one the
    scan indexes any more.

    Sabotage-proof: revive on ``"present"`` too in ``_settle_watched``;
    this test fails because ``modified`` is emitted.
    """
    _seed_vault(vault, {"notes/deep.md": "d"})
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        (vault / "notes").rename(vault / "archive")
        observers[0].emit(FileDeletedEvent(str(vault / "notes" / "deep.md")))
        (vault / "notes").symlink_to(vault / "archive", target_is_directory=True)
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "notes/deep.md")]


@pytest.mark.unit
def test_watchdog_tombstone_for_a_note_replaced_by_a_directory_stays_deleted(vault: Path) -> None:
    """A queued delete for a note now replaced by a same-named directory is not revived.

    Sabotage-proof: drop the ``S_ISREG`` check in ``_liveness``; this test
    fails because the directory counts as a revivable file and ``modified``
    is emitted for a path ``fetch`` cannot read.
    """
    observers: list[FakeWatchdogObserver] = []
    with _watchdog_only_connector(vault, observers) as connector:
        assert list(connector.list_changes(cursor=None)) == []
        (vault / "alpha.md").unlink()
        observers[0].emit(FileDeletedEvent(str(vault / "alpha.md")))
        (vault / "alpha.md").mkdir()
        events = list(connector.list_changes(cursor=_PAST_CURSOR))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]


@pytest.mark.unit
def test_note_replaced_by_a_symlink_loop_is_tombstoned(vault: Path) -> None:
    """A note replaced by a symlink loop is gone for good: reconciliation tombstones it.

    The walk skips the loop, and the liveness check must call it missing —
    not indeterminate, which would drop the tombstone on every tick.

    Sabotage-proof: return ``"unknown"`` for every ``OSError`` and the
    ``RuntimeError`` in ``_liveness``; this test fails because no event is
    emitted.
    """
    known = _hash_snapshot(vault)
    (vault / "alpha.md").unlink()
    (vault / "alpha.md").symlink_to(vault / "loop.md")
    (vault / "loop.md").symlink_to(vault / "alpha.md")

    with _reconciling_connector(vault, known) as connector:
        events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "alpha.md")]
