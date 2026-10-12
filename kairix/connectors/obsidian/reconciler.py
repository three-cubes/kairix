"""Full-scan reconciliation for the Obsidian connector.

Walks the vault tree, hashes every file the connector is configured to
index, compares against a caller-supplied "known state" (hash of the
last successfully-ingested copy keyed by ``item_id``) and emits
:class:`~kairix.core.protocols.ChangeEvent` records for the drift.

This catches the case watchdog events miss: every change that fired
while the worker was paused (process restart, suspend/resume, machine
hibernation). The connector pipeline runs the reconciler every Nth
``list_changes`` call AND whenever the cursor is ``None`` (the
first-ever sync).

Per the layered design (F35), the reconciler does NOT reach into the
``documents`` table directly — instead it accepts a ``known_state``
mapping that the orchestration layer (``kairix/core/connectors/``)
populates from whatever store it owns. Tests pass a dict; production
passes the result of a SQLite ``SELECT path, hash FROM documents``.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kairix.connectors.obsidian.fs import (
    OutsideVaultError,
    collection_accepts,
    iter_collection_files,
    read_text_for_hash,
)
from kairix.core.protocols import ChangeEvent
from kairix.knowledge.reflib.dedup import hash_content

# ``stat`` errors that prove a walked path no longer holds a note.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


@dataclass(frozen=True)
class CollectionScanSpec:
    """One subdirectory of the vault to walk.

    Mirrors the shape of :class:`kairix.core.db.scanner.CollectionConfig`
    but stays narrow — the connector only needs root + glob + exclude
    to discover files. Frozen for F42.
    """

    path: str  # relative to vault_root
    glob: str = "**/*.md"
    exclude: tuple[str, ...] = ()


class FullScanReconciler:
    """Walks the vault and emits ChangeEvents for drift.

    Construction is cheap (no I/O). All work happens inside
    :meth:`reconcile`. The reconciler keeps no state across calls — the
    orchestration layer owns "what was the last seen state", per the
    documented narrow Protocol surface in
    ``docs/architecture/connector-ingestion-architecture.md`` §3.
    """

    def __init__(
        self,
        vault_root: Path,
        collections: Iterable[CollectionScanSpec],
        *,
        read_text: Callable[[Path, Path], str] = read_text_for_hash,
    ) -> None:
        """``read_text(path, vault_root)`` reads one walked file for hashing; tests
        inject a reader that changes the filesystem between the walk and the read."""
        self._vault_root = vault_root.resolve()
        self._collections = tuple(collections)
        self._read_text = read_text

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    def accepts(self, item_id: str) -> bool:
        """True when any configured collection indexes ``item_id`` — the rule :meth:`_discover` walks with."""
        return any(
            collection_accepts(item_id, collection_path=spec.path, glob=spec.glob, exclude=spec.exclude)
            for spec in self._collections
        )

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _discover(self) -> tuple[dict[str, str], frozenset[str]]:
        """Walk the configured collections; return ``({item_id: hash}, unreadable)``.

        ``item_id`` is the vault-root-relative POSIX path (Obsidian's
        canonical identifier). Files that disappear mid-scan (race
        with a delete) are silently skipped — the next reconciliation
        will surface the delete on its own. A file the walk finds but
        cannot read (``PermissionError`` or another ``OSError``) is in
        ``unreadable``: present, content unknown — never a deletion. A
        symlink that resolves outside the vault is never read — not even one
        swapped in between the check and the read, which ``read_text`` refuses:
        ``fetch`` refuses it too, so it counts as absent and an indexed note
        there is tombstoned, even when the outside target hashes the same.
        """
        out: dict[str, str] = {}
        unreadable: set[str] = set()
        for abs_path in self._walk():
            item_id = abs_path.relative_to(self._vault_root).as_posix()
            if not self._inside_vault(abs_path):
                # ``fetch`` refuses it, so it is absent whatever its content.
                continue
            try:
                text = self._read_text(abs_path, self._vault_root)
            except FileNotFoundError:
                continue
            except (OSError, OutsideVaultError):
                # Swapped for an outside symlink after the check above: absent.
                if self._still_present(abs_path):
                    unreadable.add(item_id)
                continue
            out[item_id] = hash_content(text)
        return out, frozenset(unreadable)

    def _walk(self) -> Iterator[Path]:
        """Every file the configured collections select, in collection order."""
        for spec in self._collections:
            yield from iter_collection_files(
                vault_root=self._vault_root,
                collection_path=spec.path,
                glob=spec.glob,
                exclude=spec.exclude,
            )

    def _still_present(self, abs_path: Path) -> bool:
        """After a failed read: whether the walked path still credibly holds a note.

        A regular file inside the vault is present (content unknown). A path
        that is gone, sits under a non-directory or loops (``ENOENT``,
        ``ENOTDIR``, ``ELOOP``) or is no longer a regular file (replaced by a
        directory) is absent, and is tombstoned. Any other ``stat`` failure
        (a parent that became unreadable) proves nothing, so the note is kept
        — unless the path resolves outside the vault, which ``fetch`` refuses.
        """
        try:
            mode = abs_path.stat().st_mode
        except OSError as exc:
            return exc.errno not in _ABSENT_ERRNOS and self._inside_vault(abs_path)
        return stat.S_ISREG(mode) and self._inside_vault(abs_path)

    def _inside_vault(self, abs_path: Path) -> bool:
        """True when ``abs_path`` resolves inside the vault — the boundary ``fetch`` applies.

        Only a proven outside target is outside: non-strict ``realpath`` never
        raises, so a resolution it cannot complete (a parent that became
        unreadable) leaves the path inside the vault and the entry is kept.
        Called only for paths the walk saw as files, so a symlink loop
        (which ``is_file`` rejects) never reaches it.
        """
        return Path(os.path.realpath(abs_path)).is_relative_to(self._vault_root)

    def _present_but_unlisted(self, item_id: str) -> bool:
        """A known file the walk should have found but did not: absence unproven.

        In scope, a regular file inside the vault, and in a directory that
        resolves to itself (``Path.glob`` lists a file symlink but does not
        traverse a directory symlink) — so the walk missed it
        only because it could not list a directory (a non-root worker on a
        mode-``0111`` directory can read the file but not enumerate it). Such
        a note is not tombstoned; a note moved out of scope, or reachable
        only through a symlink, still is.
        """
        if not self.accepts(item_id):
            return False
        path = self._vault_root / item_id
        try:
            return (
                path.parent.resolve() == path.parent and self._inside_vault(path) and stat.S_ISREG(path.stat().st_mode)
            )
        except (OSError, RuntimeError):  # missing, unreadable parent, or a symlink loop
            return False

    def reconcile(self, known_state: Mapping[str, str]) -> list[ChangeEvent]:
        """Compare the live vault against ``known_state``.

        Args:
            known_state: ``{item_id: content_hash}`` for every file the
                ingest pipeline currently has in its store. Empty on
                the first-ever sync (``cursor is None``).

        Returns:
            One :class:`ChangeEvent` per drift, ordered ``created`` /
            ``modified`` first (the cheap-to-process cases), ``deleted``
            last (so a worker that dies mid-batch still records new
            content before tombstones).
        """
        # Stamp events with the walk's START: a watchdog event queued during
        # or after the walk (a note recreated mid-tick) must stay after the
        # cursor these events advance, or the next tick would filter it out.
        now = self._now_iso()
        live, unreadable = self._discover()
        live_ids = set(live.keys())
        known_ids = set(known_state.keys())

        created: list[ChangeEvent] = []
        modified: list[ChangeEvent] = []
        deleted: list[ChangeEvent] = []

        for item_id in sorted(live_ids - known_ids):
            created.append(ChangeEvent(op="created", item_id=item_id, modified_at=now))

        for item_id in sorted(live_ids & known_ids):
            if live[item_id] != known_state[item_id]:
                modified.append(ChangeEvent(op="modified", item_id=item_id, modified_at=now))

        # An unreadable file is present with unknown content: no event, so a
        # read failure never retires indexed content.
        for item_id in sorted(known_ids - live_ids - unreadable):
            if self._present_but_unlisted(item_id):
                continue
            deleted.append(ChangeEvent(op="deleted", item_id=item_id, modified_at=now))

        return [*created, *modified, *deleted]
