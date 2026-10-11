"""Unit tests for :class:`kairix.connectors.obsidian.reconciler.FullScanReconciler`.

Pins when the reconciler tombstones a known note its walk did not find:
only when the note's absence is proven. A note that is in scope and a
regular file at its own path was missed because its directory could not
be listed, so it is not tombstoned; a note moved out of scope, reachable
only through a symlink, or really gone, is.

F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kairix.connectors.obsidian.reconciler import CollectionScanSpec, FullScanReconciler
from kairix.knowledge.reflib.dedup import hash_content

pytestmark = pytest.mark.unit


def _vault(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "vault"
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root.resolve()


def _known(files: dict[str, str]) -> dict[str, str]:
    return {rel: hash_content(body) for rel, body in files.items()}


def _ops(reconciler: FullScanReconciler, known: dict[str, str]) -> list[tuple[str, str]]:
    return [(e.op, e.item_id) for e in reconciler.reconcile(known)]


def test_a_note_that_is_gone_is_tombstoned(tmp_path: Path) -> None:
    """Sabotage-proof: return ``True`` from the ``except`` in ``_present_but_unlisted``;
    this test fails because the missing note is never tombstoned."""
    files = {"alpha.md": "a", "bravo.md": "b"}
    vault = _vault(tmp_path, files)
    (vault / "bravo.md").unlink()

    assert _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), _known(files)) == [("deleted", "bravo.md")]


def test_a_note_moved_out_of_scope_is_tombstoned(tmp_path: Path) -> None:
    """Sabotage-proof: return ``True`` when ``accepts`` rejects the note; this test
    fails because the excluded note, still on disk, is not tombstoned."""
    files = {"notes/kept.md": "k", "notes/wip/draft.md": "d"}
    vault = _vault(tmp_path, files)
    spec = CollectionScanSpec(path="notes", exclude=("wip/",))

    assert _ops(FullScanReconciler(vault, [spec]), _known(files)) == [("deleted", "notes/wip/draft.md")]


def test_a_note_reachable_only_through_a_directory_symlink_is_tombstoned(tmp_path: Path) -> None:
    """Sabotage-proof: drop ``path.resolve() == path`` (or turn its ``and`` into
    ``or``) in ``_present_but_unlisted``; this test fails because the note under a
    symlinked directory, which the walk does not traverse, is not tombstoned."""
    files = {"notes/deep.md": "d"}
    vault = _vault(tmp_path, files)
    (vault / "notes").rename(vault / "archive")
    (vault / "notes").symlink_to(vault / "archive", target_is_directory=True)

    ops = _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), _known(files))

    assert sorted(ops) == [("created", "archive/deep.md"), ("deleted", "notes/deep.md")]


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unlistable directory",
)
def test_a_readable_note_in_an_unlistable_directory_is_not_tombstoned(tmp_path: Path) -> None:
    """Sabotage-proof: make ``_present_but_unlisted`` return ``False``; this test fails
    because the readable note in the mode-0111 directory is tombstoned."""
    files = {"listless/note.md": "n", "alpha.md": "a"}
    vault = _vault(tmp_path, files)
    listless = vault / "listless"
    listless.chmod(0o111)
    try:
        ops = _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), _known(files))
    finally:
        listless.chmod(0o755)

    assert ops == []


# Root reads any file regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable file",
)
def test_an_unreadable_symlink_outside_the_vault_is_tombstoned(tmp_path: Path) -> None:
    """A note replaced by a symlink to an unreadable file outside the vault is absent, not unreadable.

    Sabotage-proof: add every read failure to ``unreadable`` (drop the
    ``_inside_vault`` check in ``_discover``); this test fails because the
    note ``fetch`` refuses is never tombstoned.
    """
    files = {"alpha.md": "a"}
    vault = _vault(tmp_path, files)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (vault / "alpha.md").unlink()
    (vault / "alpha.md").symlink_to(outside)
    outside.chmod(0o000)
    try:
        ops = _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), _known(files))
    finally:
        outside.chmod(0o644)

    assert ops == [("deleted", "alpha.md")]


# Root reads any file regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable file",
)
def test_an_unreadable_note_inside_the_vault_is_not_tombstoned(tmp_path: Path) -> None:
    """A note the walk finds but cannot read is present with unknown content.

    No event, and it stays in the prune cycle's enumeration so it is not
    pruned either.

    Sabotage-proof: make ``_inside_vault`` return ``False``; this test fails
    because ``alpha.md`` drops out of ``item_ids`` and would be pruned.
    """
    files = {"alpha.md": "a", "bravo.md": "b"}
    vault = _vault(tmp_path, files)
    reconciler = FullScanReconciler(vault, [CollectionScanSpec(path=".")])
    (vault / "alpha.md").chmod(0o000)
    try:
        ops = _ops(reconciler, _known(files))
        enumerated = reconciler.item_ids()
    finally:
        (vault / "alpha.md").chmod(0o644)

    assert ops == []
    assert enumerated == ["alpha.md", "bravo.md"]
