"""``ObsidianConnector`` — SourceConnector for filesystem-backed markdown vaults.

Implements the :class:`kairix.core.protocols.SourceConnector` Protocol
for an Obsidian-style knowledge store. Two change-detection strategies
combine, exactly as KAIRIX-VISION-LANDSCAPE-AND-ROADMAP §4.4 names:

  * **Watchdog events** since the last cursor — tail a thread-safe
    queue populated by :class:`WatchdogSource`. Fires on edits the
    worker is awake to see.
  * **Full-scan reconciliation** every Nth call, or whenever the
    cursor is ``None`` — walks the vault, compares file hashes to a
    caller-supplied snapshot, emits ``created`` / ``modified`` /
    ``deleted`` events for any drift. Catches the events that fired
    while the worker was paused (process restart, suspend/resume,
    hibernation).

The cursor token is the ISO-8601 timestamp of the last successfully-
processed event. New events have ``modified_at > cursor`` per the
spec.

The connector deliberately keeps the watchdog observer running across
``list_changes`` calls — start-on-first-use, stop on
:meth:`close` / context-manager exit. Stopping and restarting between
calls would race with concurrent edits and force every reconciliation
to repopulate the queue. Process exit without an explicit ``close`` is
acceptable: the observer thread is a daemon (watchdog's default) and
dies with the process. Long-lived workers should call ``close()`` (or
use the connector as a context manager) for clean shutdown.

Per F35, this module only imports from ``kairix.connectors.obsidian.*``
(same plugin) and ``kairix.core.*`` (the Protocol surface). No reach
into other connectors, no reach into the extractor layer.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kairix.connectors.obsidian.fs import (
    DEFAULT_MIME,
    mime_for_bytes,
    mime_for_path,
)
from kairix.connectors.obsidian.reconciler import (
    CollectionScanSpec,
    FullScanReconciler,
)
from kairix.connectors.obsidian.watcher import FileChange, WatchdogSource
from kairix.core.db.scanner import CollectionConfig
from kairix.core.protocols import (
    ChangeEvent,
    Container,
    Cursor,
    HierarchyNode,
    RawArtefact,
    Sensitivity,
    SourceMetadata,
)

CONNECTOR_NAME = "obsidian"

# Default reconciliation cadence: every Nth ``list_changes`` call runs
# a full-scan pass in addition to draining the watchdog queue. Picked
# so a worker on a 10-second tick reconciles roughly every two minutes
# — short enough to catch a paused-worker miss inside one shift,
# long enough to amortise the walk on a large vault.
DEFAULT_RECONCILE_EVERY = 10


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso_z(datetime.now(timezone.utc))


def _default_known_state(_cursor: Cursor | None) -> Mapping[str, str]:
    """Production callers override this with a real document-store
    snapshot lookup. The default is empty — the connector then treats
    every file in the vault as ``created`` on first sync, which is the
    correct behaviour but slow for a large vault.

    The orchestration layer (``kairix/core/connectors/``) passes a real
    resolver against the ``documents`` table; tests pass a dict
    literal. F6-clean: the default is a real callable, not ``None``.
    """
    return {}


class ObsidianConnector:
    """SourceConnector implementation for an Obsidian markdown vault.

    Construction is cheap (no I/O, no thread started). The first
    :meth:`list_changes` call starts the watchdog observer; subsequent
    calls drain its queue. :meth:`close` stops the observer cleanly.

    DI seams:

      * ``known_state_resolver`` — production callers pass a resolver
        that queries the documents table for ``{item_id: hash}``;
        tests pass a dict literal. Default is empty mapping, which
        makes the first sync emit ``created`` for every file (correct,
        but slow).
      * ``watcher_factory`` — constructs the :class:`WatchdogSource`.
        Tests pass a factory that returns a recording stand-in so we
        never start a real OS-level watcher under pytest.
      * ``reconcile_every`` — controls how often a full scan runs on
        top of the watchdog drain.
    """

    name: str = CONNECTOR_NAME
    per_tick_max_items: int = 500
    # F66-watermark-exempt: reads local FS only; no remote-fetch disk pressure
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        vault_root: Path,
        collections: list[CollectionConfig] | None = None,
        sensitivity: Sensitivity = "internal",
        *,
        known_state_resolver: Callable[[Cursor | None], Mapping[str, str]] = _default_known_state,
        watcher_factory: Callable[[Path], WatchdogSource] | None = None,
        reconcile_every: int = DEFAULT_RECONCILE_EVERY,
    ) -> None:
        self._vault_root = vault_root.resolve()
        self._collections: tuple[CollectionConfig, ...] = tuple(
            collections if collections is not None else [CollectionConfig(name=CONNECTOR_NAME, path=".")]
        )
        self._sensitivity: Sensitivity = sensitivity
        self._known_state_resolver = known_state_resolver
        self._watcher_factory: Callable[[Path], WatchdogSource] = watcher_factory or WatchdogSource
        self._reconcile_every = max(1, reconcile_every)

        self._call_count = 0
        self._watcher: WatchdogSource | None = None
        self._reconciler = FullScanReconciler(
            vault_root=self._vault_root,
            collections=_to_scan_specs(self._collections),
        )
        # Tracks the max ``modified_at`` observed across all events
        # emitted from the most recent :meth:`list_changes` drain. The
        # orchestrator reads this via :meth:`next_cursor` to persist
        # the cursor after each chunk-commit. Obsidian's cursor IS an
        # ISO-8601 timestamp (see :meth:`list_changes` docstring), so
        # the per-drain high-water-mark is the correct token.
        self._last_max_modified_at: str | None = None

    # ------------------------------------------------------------------
    # SourceConnector Protocol surface
    # ------------------------------------------------------------------

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream changes since ``cursor``.

        ``cursor`` is an ISO-8601 timestamp; events with
        ``modified_at <= cursor`` are filtered out. First-call
        behaviour (``cursor is None``) ALWAYS runs the reconciler so
        the worker boots into a known-good state without needing the
        watchdog to fire.
        """
        self._call_count += 1
        self._ensure_watcher_started()

        # Drain whatever the watchdog observer pushed since the last call.
        drained = self._watcher.drain() if self._watcher is not None else []
        watchdog_events = _to_change_events(drained)

        # Reconcile on cold-start AND every Nth call.
        reconcile_events: list[ChangeEvent] = []
        if cursor is None or self._call_count % self._reconcile_every == 0:
            known = self._known_state_resolver(cursor)
            reconcile_events = self._reconciler.reconcile(known)

        # De-duplicate by item_id so a watchdog "created" and a
        # reconciler "created" for the same path don't emit twice.
        # Watchdog wins when both fire — its timestamp is closer to
        # the actual edit.
        seen: set[str] = set()
        merged: list[ChangeEvent] = []
        for ev in watchdog_events + reconcile_events:
            if ev.item_id in seen:
                continue
            seen.add(ev.item_id)
            if cursor is not None and ev.modified_at <= cursor:
                continue
            merged.append(ev)
        # Track the max modified_at observed so next_cursor() can return
        # the high-water-mark to persist. When the drain yields no
        # events but cursor was non-None, preserve cursor so the next
        # tick doesn't regress to a full scan.
        self._last_max_modified_at = _max_modified_at(merged, fallback=cursor)
        return iter(merged)

    def next_cursor(self) -> str | None:
        """Return the ISO-8601 high-water-mark from the most recent drain.

        Obsidian's cursor IS an ISO-8601 timestamp — the orchestrator
        persists this between ticks so the next :meth:`list_changes`
        call filters events with ``modified_at <= cursor``. ``None``
        before the first :meth:`list_changes` call.
        """
        return self._last_max_modified_at

    def fetch(self, item_id: str) -> RawArtefact:
        """Read the file at ``vault_root / item_id``.

        Mime detection is extension-first, with a magic-byte fallback
        when the extension is generic. The extractor selection
        (passthrough vs markitdown vs ocr) happens upstream in the
        pipeline, per the SC-3 separation of concerns.
        """
        abs_path = self._safe_resolve(item_id)
        raw = abs_path.read_bytes()
        mime = mime_for_path(abs_path)
        if mime == DEFAULT_MIME:
            mime = mime_for_bytes(raw, fallback=DEFAULT_MIME)
        return RawArtefact(raw=raw, mime=mime, fetched_at=_now_iso())

    def source_link(self, item_id: str) -> str:
        """``obsidian://open?vault=<vault-name>&file=<item_id>``.

        The vault name is the basename of the vault root (Obsidian's
        own convention for vault identity); ``item_id`` is the
        vault-root-relative POSIX path. Both components are URL-
        encoded so spaces and unicode pass through to the editor
        cleanly.
        """
        vault_name = self._vault_root.name
        return f"obsidian://open?vault={quote(vault_name, safe='')}&file={quote(item_id, safe='/')}"

    def sensitivity_for(self, _item_id: str) -> Sensitivity:
        """Return the connector's configured sensitivity tier.

        v1 has no per-item overrides — the operator picks one tier per
        connector config block. A future ADR can add path-pattern
        overrides without breaking the Protocol.
        """
        return self._sensitivity

    # ------------------------------------------------------------------
    # Topology Wave E — per-connector multi-container pilot
    # ------------------------------------------------------------------
    # Wave B landed shim implementations of the capability Protocols
    # (PollConnector / SlimConnector / HierarchyConnector). Wave E adds
    # real implementations behind the ``topology_obsidian`` flag:
    #
    #   * :meth:`iter_containers` — one :class:`Container` per top-level
    #     vault folder, each with its own per-cc_pair delta cursor.
    #   * :meth:`list_changes_for_container` — when flag ON, scopes the
    #     watchdog drain + reconciler walk to the container's subtree
    #     and uses ``container.cursor_token`` as the per-container ISO
    #     timestamp cursor. When flag OFF, retains the Wave B shim
    #     behaviour (delegate to :meth:`list_changes`).
    #   * :meth:`load_hierarchy` — when flag ON, walks the vault
    #     filesystem emitting one FOLDER node per directory parent-
    #     before-child (per F58). When flag OFF, retains the Wave B
    #     shim behaviour (one root FOLDER node).
    #
    # The flag defaults OFF so existing operators see bit-for-bit
    # current behaviour. The ON branch is the per-container pattern
    # that subagents follow for dex_crm / m365_* / sharepoint / notion /
    # slack / github wave-E adoption.

    def iter_containers(self, cc_pair_id: int) -> Iterator[Container]:
        """Yield one :class:`Container` per top-level vault folder.

        Topology §4: each Container has its own delta cursor — the
        Wave E pilot maps each top-level folder of the vault to its own
        Container so the operator can sync different folders at
        different cadences and scope retrieval per-folder via the
        topology collection mapping.

        Calling convention: the framework's lifecycle layer (see
        ``kairix/core/connectors/cc_pair.py``) passes ``cc_pair_id`` so
        the connector can construct the Container without reaching back
        into the cc_pair store. Mirrors the dispatch shape the
        ``HierarchyConnector.load_hierarchy(cc_pair_id)`` Protocol
        method already uses.

        ``access_state`` is always ``ACCESSIBLE`` — Obsidian vaults are
        single-user filesystem mounts with no per-folder permission
        story. ``cursor_token`` and ``last_synced_at`` start ``None``;
        the framework persists subsequent values to the
        ``topology_containers`` table.

        Empty-vault fallback: if no top-level folders exist, yield one
        Container with ``container_id=""`` representing the root itself
        so the connector still works on a flat vault.
        """
        top_level = _top_level_folders(self._vault_root)
        if not top_level:
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id="",
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )
            return
        for folder in top_level:
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id=folder,
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )

    def list_changes_for_container(self, container: Container) -> Iterator[ChangeEvent]:
        """Stream changes for one Container's subtree.

        Scopes the watchdog drain + reconciler walk to ``vault_root /
        container.container_id`` and uses ``container.cursor_token`` as
        the per-container ISO timestamp cursor. Files outside the
        container's subtree are filtered out so a per-folder cc_pair
        only sees its own changes.

        ``topology_obsidian`` retired post-cutover (task #132); the
        per-container path is now the only behaviour.
        """
        return self._list_changes_scoped(container)

    def retrieve_all_slim_docs(self, _container: Container) -> Iterator[str]:
        """SlimConnector shim — enumerate item_ids only via the reconciler walk.

        Drives the same :class:`FullScanReconciler` the change-detection
        path uses, but extracts only ``item_id`` (no hash compare, no
        change-event construction). Used by the prune cycle to diff
        against ``documents.item_id`` and stage tombstones.
        """
        # An empty known-state mapping forces the reconciler to emit one
        # ChangeEvent per file; we strip down to item_ids only so the
        # caller pays no chunk-construction cost. Behavioural shape:
        # same enumeration order as the change-detection path.
        events = self._reconciler.reconcile({})
        return iter(ev.item_id for ev in events)

    def load_hierarchy(self, cc_pair_id: int) -> Iterator[HierarchyNode]:
        """HierarchyConnector — emit FOLDER nodes parent-before-child.

        Walks the vault filesystem with :func:`os.walk` and emits one
        FOLDER node per directory, parent-before-child per F58. The root
        folder is emitted first (``raw_parent_id=None``), then every
        descendant directory with ``raw_parent_id`` referencing the
        previously-emitted parent's ``raw_node_id``.

        ``raw_node_id`` is the vault-root-relative POSIX path of the
        directory (e.g. ``"02-Areas/00-Clients/Inpex"``); for the root
        it is the vault basename. ``link`` is an ``obsidian://`` deep
        link to the folder so the search layer can surface a clickable
        affordance. ``sensitivity_hint`` is ``None`` — the operator
        overrides per-folder via the collection mapping.

        ``topology_obsidian`` retired post-cutover (task #132); the
        per-directory walk is now the only behaviour.
        """
        yield from _walk_hierarchy(
            vault_root=self._vault_root,
            cc_pair_id=cc_pair_id,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the watchdog observer thread cleanly.

        Safe to call from any thread; idempotent. Process exit without
        an explicit ``close`` works because watchdog's observer thread
        is a daemon, but long-lived workers should call this in their
        shutdown path.
        """
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def __enter__(self) -> ObsidianConnector:
        self._ensure_watcher_started()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_watcher_started(self) -> None:
        if self._watcher is not None and self._watcher.is_running:
            return
        if self._watcher is None:
            self._watcher = self._watcher_factory(self._vault_root)
        self._watcher.start()

    def _list_changes_scoped(self, container: Container) -> Iterator[ChangeEvent]:
        """Wave E ON-branch: scope change detection to one Container's subtree.

        Drains the watchdog queue but filters to file events whose
        ``item_id`` is under the container's path. Reconciles by
        constructing a per-container :class:`FullScanReconciler` rooted
        at the configured collection narrowed to the container's
        subtree. The cursor is ``container.cursor_token`` so each
        per-container cc_pair gets its own delta horizon.

        Note: drains and reconciler walks happen on every call (no
        ``reconcile_every`` cadence) because the per-container pilot
        owns its own reconciliation schedule via the framework's
        cc_pair lifecycle — the connector itself stays stateless
        across containers within a single call.
        """
        self._ensure_watcher_started()
        cursor = container.cursor_token
        container_prefix = container.container_id

        # Drain watchdog events; keep only those under this container.
        drained = self._watcher.drain() if self._watcher is not None else []
        watchdog_events = _to_change_events(_filter_to_container(drained, container_prefix))

        # Build a per-container reconciler scoped to this container's
        # subtree only. Empty known-state forces the reconciler to emit
        # one event per file under the container; the framework's
        # known-state resolver populates this from the documents table.
        container_specs = _scoped_specs_for_container(self._collections, container_prefix)
        reconciler = FullScanReconciler(
            vault_root=self._vault_root,
            collections=container_specs,
        )
        known = self._known_state_resolver(cursor)
        reconcile_events = reconciler.reconcile(known)

        # De-duplicate by item_id; watchdog wins when both fire.
        seen: set[str] = set()
        merged: list[ChangeEvent] = []
        for ev in watchdog_events + reconcile_events:
            if ev.item_id in seen:
                continue
            seen.add(ev.item_id)
            if cursor is not None and ev.modified_at <= cursor:
                continue
            merged.append(ev)
        # High-water-mark tracking matches the legacy list_changes path
        # so next_cursor() returns the correct token regardless of
        # which path was taken last.
        self._last_max_modified_at = _max_modified_at(merged, fallback=cursor)
        return iter(merged)

    def _safe_resolve(self, item_id: str) -> Path:
        """Resolve ``vault_root / item_id`` and reject path traversal.

        ``item_id`` should always be a vault-root-relative POSIX
        string (the connector's own canonical form). Reject absolute
        paths and ``..`` segments that would escape the vault.
        """
        if os.path.isabs(item_id):
            raise ValueError(
                f"Obsidian item_id must be vault-relative, got absolute path {item_id!r}. "
                "fix: pass the path as emitted by list_changes (vault-root-relative POSIX). "
                "next: see kairix/connectors/obsidian/connector.py for the item_id contract."
            )
        candidate = (self._vault_root / item_id).resolve()
        try:
            candidate.relative_to(self._vault_root)
        except ValueError as exc:
            raise ValueError(
                f"Obsidian item_id {item_id!r} resolved outside vault_root {self._vault_root!s}. "
                "fix: emit item_ids only via list_changes() so they round-trip cleanly. "
                "next: investigate the upstream call that produced this id."
            ) from exc
        return candidate

    # ------------------------------------------------------------------
    # ADR-021 (Wave E.5) — per-source envelope metadata
    # ------------------------------------------------------------------

    def metadata_for(self, item_id: str) -> SourceMetadata:
        """Return file-stat + frontmatter envelope metadata for ``item_id``.

        ADR-021: Obsidian's envelope is the filesystem mtime + ctime
        plus the ``---``-delimited YAML frontmatter at the top of the
        markdown file. Tags come from the ``tags:`` frontmatter key
        (string or list). Author comes from the ``author:`` frontmatter
        key. Missing files / unreadable files / parse failures collapse
        to an empty :class:`SourceMetadata` so the pipeline keeps
        running on a corrupt vault entry.
        """
        try:
            abs_path = self._safe_resolve(item_id)
        except ValueError:
            return SourceMetadata()
        if not abs_path.is_file():
            return SourceMetadata()
        try:
            stat = abs_path.stat()
        except OSError:
            return SourceMetadata()
        modified_at = _iso_z(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
        created_at = _iso_z(datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc))
        try:
            head_bytes = abs_path.read_bytes()[:4096]
            head = head_bytes.decode("utf-8", errors="replace")
            front = _parse_frontmatter(head)
        except OSError:
            front = {}
        author, tags = _frontmatter_author_tags(front)
        return SourceMetadata(
            modified_at=modified_at,
            created_at=created_at,
            author=author,
            tags=tags,
        )


def _frontmatter_author_tags(front: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """Extract ``(author, tags)`` from parsed frontmatter.

    ``author:`` must be a non-blank string; ``tags:`` may be a list of
    strings (blank entries dropped) or a single non-blank string. Any
    other shape yields ``None`` / ``()``.
    """
    author: str | None = None
    raw_author = front.get("author")
    if isinstance(raw_author, str) and raw_author.strip():
        author = raw_author.strip()
    tags: tuple[str, ...] = ()
    raw_tags = front.get("tags")
    if isinstance(raw_tags, list):
        tags = tuple(str(t) for t in raw_tags if isinstance(t, str) and t.strip())
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags = (raw_tags.strip(),)
    return author, tags


def _parse_frontmatter(head: str) -> dict[str, Any]:
    """Parse the leading ``---`` YAML frontmatter block of ``head``.

    Returns an empty dict on missing block or parse failure. Tolerant
    of trailing newlines and the optional BOM. Frontmatter parsing is
    best-effort — corrupt YAML never raises out of
    :meth:`ObsidianConnector.metadata_for`.
    """
    text = head.lstrip("﻿").lstrip()
    if not text.startswith("---"):
        return {}
    after = text[3:]
    end_marker = after.find("\n---")
    if end_marker == -1:
        return {}
    block = after[:end_marker]
    try:
        import yaml

        parsed = yaml.safe_load(block)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _max_modified_at(events: list[ChangeEvent], *, fallback: str | None) -> str | None:
    """Return the max ``modified_at`` across ``events``, or ``fallback`` if empty.

    Used by :meth:`ObsidianConnector.list_changes` to track the cursor
    high-water-mark per drain. On a zero-event drain the prior cursor
    is preserved so the orchestrator doesn't clobber a real cursor
    position with ``None``.
    """
    if not events:
        return fallback
    return max(ev.modified_at for ev in events)


def _to_scan_specs(collections: Iterable[CollectionConfig]) -> list[CollectionScanSpec]:
    """Translate scanner CollectionConfig to the reconciler's narrower spec."""
    return [CollectionScanSpec(path=c.path, glob=c.glob, exclude=tuple(c.exclude)) for c in collections]


def _to_change_events(changes: list[FileChange]) -> list[ChangeEvent]:
    """Convert watchdog drain output to the connector boundary's
    :class:`ChangeEvent` shape.
    """
    return [ChangeEvent(op=c.op, item_id=c.item_id, modified_at=c.observed_at) for c in changes]


def _top_level_folders(vault_root: Path) -> list[str]:
    """Return sorted vault-root-relative names of top-level directories.

    Used by :meth:`ObsidianConnector.iter_containers` to map each
    top-level folder of the vault to its own :class:`Container`.
    Hidden directories (``.`` prefix — ``.obsidian/``, ``.git/``,
    ``.trash/``) are skipped because they're either editor state or
    operator-private and never represent indexable content.

    Returns an empty list when the vault has no top-level directories
    (a flat vault); the caller handles that as the root-Container
    fallback.
    """
    if not vault_root.exists() or not vault_root.is_dir():
        return []
    folders: list[str] = []
    for entry in sorted(vault_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        folders.append(entry.name)
    return folders


def _filter_to_container(changes: list[FileChange], container_prefix: str) -> list[FileChange]:
    """Keep only the file changes whose item_id is under ``container_prefix``.

    Empty ``container_prefix`` means "root container" — every change
    passes through. Non-empty prefix matches the vault-root-relative
    POSIX-path prefix; the trailing-slash check stops
    ``"01-Projects-Old/"`` matching when the container is
    ``"01-Projects"``.
    """
    if container_prefix == "":
        return list(changes)
    prefix_with_slash = container_prefix.rstrip("/") + "/"
    return [c for c in changes if c.item_id.startswith(prefix_with_slash)]


def _scoped_specs_for_container(
    collections: tuple[CollectionConfig, ...],
    container_prefix: str,
) -> list[CollectionScanSpec]:
    """Narrow the configured collections to the container's subtree.

    For each :class:`CollectionConfig`, if its ``path`` is already
    inside the container (or the container is the root, in which case
    every collection passes through) keep it as-is. Otherwise, re-root
    its ``path`` at the container so the reconciler walks only the
    container's subtree.

    Falls back to a single spec covering the container's path when no
    configured collection overlaps the container — this keeps the
    Wave E ON branch working on a vault that has only the default
    "whole vault" collection.
    """
    if container_prefix == "":
        return _to_scan_specs(collections)
    container_norm = container_prefix.rstrip("/")
    specs: list[CollectionScanSpec] = []
    for c in collections:
        collection_norm = c.path.rstrip("/") if c.path not in ("", ".") else ""
        is_root = collection_norm == ""
        equals_container = container_norm == collection_norm
        inside_container = _path_is_under(collection_norm, container_norm)
        if is_root or equals_container or inside_container:
            # Collection already inside (or equal to) container — keep as-is when inside;
            # narrow to container when at-or-above container root.
            scoped_path = collection_norm if inside_container else container_norm
            specs.append(CollectionScanSpec(path=scoped_path, glob=c.glob, exclude=tuple(c.exclude)))
    if not specs:
        # No configured collection overlaps the container. Default to a
        # whole-container walk with the standard ``**/*.md`` glob.
        specs.append(CollectionScanSpec(path=container_norm, glob="**/*.md"))
    return specs


def _path_is_under(child: str, parent: str) -> bool:
    """True when ``child`` is at or beneath ``parent`` in the vault tree."""
    if parent == "" or child == parent:
        return True
    return child.startswith(parent.rstrip("/") + "/")


def _walk_hierarchy(*, vault_root: Path, cc_pair_id: int) -> Iterator[HierarchyNode]:
    """Walk the vault filesystem emitting one FOLDER node per directory.

    Emission order is parent-before-child per F58: the root folder is
    yielded first, then every descendant directory in
    breadth-first-friendly :func:`os.walk` order — :func:`os.walk` with
    ``topdown=True`` always visits a directory before its children.
    Hidden directories (``.obsidian/``, ``.git/``, ``.trash/``) are
    pruned from the walk so editor state doesn't pollute the hierarchy.

    Each emitted :class:`HierarchyNode` carries:

    * ``raw_node_id`` — the vault-root-relative POSIX path of the
      directory; the root uses the vault's basename.
    * ``raw_parent_id`` — ``None`` for the root, else the parent
      directory's ``raw_node_id``.
    * ``link`` — an ``obsidian://`` deep link to the folder.
    * ``sensitivity_hint`` — ``None``; operators override per-folder
      via the topology collection mapping.
    """
    vault_name = vault_root.name
    # Root node first — F58 parent-before-child invariant.
    yield HierarchyNode(
        cc_pair_id=cc_pair_id,
        raw_node_id=vault_name,
        raw_parent_id=None,
        display_name=vault_name,
        link=_obsidian_folder_link(vault_name=vault_name, folder_rel=""),
        node_type="FOLDER",
        external_access_json=None,
        sensitivity_hint=None,
    )
    for dirpath, dirnames, _filenames in os.walk(vault_root, topdown=True):
        # Prune hidden directories in-place so os.walk doesn't descend.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for dirname in dirnames:
            abs_child = Path(dirpath) / dirname
            rel_child = abs_child.relative_to(vault_root).as_posix()
            rel_parent = Path(dirpath).relative_to(vault_root).as_posix()
            parent_id = vault_name if rel_parent in (".", "") else rel_parent
            yield HierarchyNode(
                cc_pair_id=cc_pair_id,
                raw_node_id=rel_child,
                raw_parent_id=parent_id,
                display_name=dirname,
                link=_obsidian_folder_link(vault_name=vault_name, folder_rel=rel_child),
                node_type="FOLDER",
                external_access_json=None,
                sensitivity_hint=None,
            )


def _obsidian_folder_link(*, vault_name: str, folder_rel: str) -> str:
    """Build an ``obsidian://`` deep link for a folder.

    The vault name and folder path are URL-encoded so spaces and
    unicode pass through to the editor cleanly. Empty ``folder_rel``
    addresses the vault root.
    """
    encoded_vault = quote(vault_name, safe="")
    encoded_folder = quote(folder_rel, safe="/")
    return f"obsidian://open?vault={encoded_vault}&file={encoded_folder}"


def make_connector(config: Mapping[str, Any]) -> ObsidianConnector:
    """Construct an :class:`ObsidianConnector` from a config mapping.

    Expected keys:

      * ``vault_root`` (required) — path string or :class:`Path`.
      * ``collections`` (optional) — sequence of either
        :class:`CollectionConfig` instances OR dicts with at least
        ``name`` + ``path`` (other keys: ``glob`` / ``exclude``).
        Defaults to one collection covering the entire vault.
      * ``sensitivity`` (optional) — one of the F39 sensitivity
        literals; defaults to ``"internal"``.

    Registered via ``[project.entry-points."kairix.connectors"]`` in
    kairix's ``pyproject.toml`` so the orchestration layer can resolve
    ``obsidian`` to this factory by name.
    """
    raw_root = config.get("vault_root")
    if raw_root is None:
        raise ValueError(
            "obsidian: config is missing 'vault_root'. "
            "fix: add vault_root: /path/to/your/vault under the obsidian connector block in kairix.config.yaml. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8 for the config shape."
        )
    vault_root = Path(raw_root).expanduser()

    raw_collections = config.get("collections")
    collections: list[CollectionConfig] | None
    if raw_collections is None:
        collections = None
    else:
        collections = [_collection_from(item) for item in raw_collections]

    sensitivity: Sensitivity = config.get("sensitivity", "internal")
    return ObsidianConnector(
        vault_root=vault_root,
        collections=collections,
        sensitivity=sensitivity,
    )


def _collection_from(item: CollectionConfig | Mapping[str, Any]) -> CollectionConfig:
    """Accept either a CollectionConfig or a config-mapping shape."""
    if isinstance(item, CollectionConfig):
        return item
    name = item.get("name")
    path = item.get("path")
    if not isinstance(name, str) or not isinstance(path, str):
        raise ValueError(
            "obsidian: collection entry must declare 'name' and 'path' strings. "
            "fix: each collection block needs name + path; optional glob + exclude. "
            "next: see kairix/core/db/scanner.py:CollectionConfig for the dataclass shape."
        )
    glob = item.get("glob", "**/*.md")
    exclude = list(item.get("exclude", []))
    return CollectionConfig(name=name, path=path, glob=glob, exclude=exclude)
