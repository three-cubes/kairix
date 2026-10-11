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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kairix.connectors.obsidian.fs import (
    iter_collection_files,
    read_text_for_hash,
)
from kairix.core.protocols import ChangeEvent
from kairix.knowledge.reflib.dedup import hash_content


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

    def __init__(self, vault_root: Path, collections: Iterable[CollectionScanSpec]) -> None:
        self._vault_root = vault_root.resolve()
        self._collections = tuple(collections)

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _discover(self) -> tuple[dict[str, str], frozenset[str]]:
        """Walk the configured collections; return ``({item_id: hash}, unreadable)``.

        ``item_id`` is the vault-root-relative POSIX path (Obsidian's
        canonical identifier). Files that disappear mid-scan (race
        with a delete) are silently skipped — the next reconciliation
        will surface the delete on its own. A file the walk finds but
        cannot read (``PermissionError`` or another ``OSError``) is in
        ``unreadable``: present, content unknown — never a deletion.
        """
        out: dict[str, str] = {}
        unreadable: set[str] = set()
        for spec in self._collections:
            for abs_path in iter_collection_files(
                vault_root=self._vault_root,
                collection_path=spec.path,
                glob=spec.glob,
                exclude=spec.exclude,
            ):
                item_id = abs_path.relative_to(self._vault_root).as_posix()
                try:
                    text = read_text_for_hash(abs_path)
                except FileNotFoundError:
                    continue
                except OSError:
                    unreadable.add(item_id)
                    continue
                out[item_id] = hash_content(text)
        return out, frozenset(unreadable)

    def item_ids(self) -> list[str]:
        """Every file the walk finds, readable or not, sorted.

        The enumeration the prune cycle diffs against the store: an
        unreadable file is still present, so it must not be pruned.
        """
        live, unreadable = self._discover()
        return sorted(live.keys() | unreadable)

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
        live, unreadable = self._discover()
        live_ids = set(live.keys())
        known_ids = set(known_state.keys())

        now = self._now_iso()
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
            deleted.append(ChangeEvent(op="deleted", item_id=item_id, modified_at=now))

        return [*created, *modified, *deleted]
