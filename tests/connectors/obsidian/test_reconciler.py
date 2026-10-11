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
from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.connectors.obsidian.fs import read_text_for_hash
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


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unlistable directory",
)
def test_an_in_vault_file_symlink_in_an_unlistable_directory_is_not_tombstoned(tmp_path: Path) -> None:
    """A file symlink to an in-vault note, in a directory the walk cannot list, keeps its index.

    Sabotage-proof: require the note's own path to resolve to itself in
    ``_present_but_unlisted`` (``path.resolve() == path``); this test fails
    because the alias the walk would list is tombstoned.
    """
    files = {"alpha.md": "a"}
    vault = _vault(tmp_path, files)
    listless = vault / "listless"
    listless.mkdir()
    (listless / "alias.md").symlink_to(vault / "alpha.md")
    known = {**_known(files), "listless/alias.md": hash_content("a")}
    listless.chmod(0o111)
    try:
        ops = _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), known)
    finally:
        listless.chmod(0o755)

    assert ops == []


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unlistable directory",
)
def test_an_out_of_vault_file_symlink_in_an_unlistable_directory_is_tombstoned(tmp_path: Path) -> None:
    """A file symlink pointing outside the vault, in a directory the walk cannot list, is tombstoned.

    ``fetch`` refuses the path, so its absence from the walk stands.

    Sabotage-proof: drop ``self._inside_vault(path)`` from
    ``_present_but_unlisted``; this test fails because the out-of-vault
    alias keeps its stale index.
    """
    vault = _vault(tmp_path, {"alpha.md": "a"})
    outside = tmp_path / "outside.md"
    outside.write_text("o", encoding="utf-8")
    listless = vault / "listless"
    listless.mkdir()
    (listless / "out.md").symlink_to(outside)
    known = {**_known({"alpha.md": "a"}), "listless/out.md": hash_content("o")}
    listless.chmod(0o111)
    try:
        ops = _ops(FullScanReconciler(vault, [CollectionScanSpec(path=".")]), known)
    finally:
        listless.chmod(0o755)

    assert ops == [("deleted", "listless/out.md")]


def _replacing_reader(target: Path, replace: Callable[[Path], None]) -> Callable[[Path], str]:
    """A reader that changes ``target`` on disk just before reading it — between walk and read."""

    def _read(path: Path) -> str:
        if path == target:
            replace(path)
        return read_text_for_hash(path)

    return _read


def _to_directory(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _to_loop(path: Path) -> None:
    path.unlink()
    path.symlink_to(path)


@pytest.mark.parametrize("replace", [_to_directory, _to_loop], ids=["directory", "symlink-loop"])
def test_a_note_replaced_during_read_is_tombstoned(tmp_path: Path, replace: Callable[[Path], None]) -> None:
    """A note replaced between the walk and the read by a directory or a loop is absent.

    Sabotage-proof: return ``True`` from ``_still_present`` whenever ``stat``
    succeeds (drop the ``S_ISREG`` check); the directory case fails. Drop
    ``ELOOP`` from ``_ABSENT_ERRNOS``; the loop case fails. Either way the
    note keeps its stale index.
    """
    files = {"alpha.md": "a", "bravo.md": "b"}
    vault = _vault(tmp_path, files)
    reconciler = FullScanReconciler(
        vault,
        [CollectionScanSpec(path=".")],
        read_text=_replacing_reader(vault / "bravo.md", replace),
    )

    assert _ops(reconciler, _known(files)) == [("deleted", "bravo.md")]


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable directory",
)
def test_a_note_whose_directory_becomes_unreadable_during_read_is_kept(tmp_path: Path) -> None:
    """A note whose parent becomes unreadable between the walk and the read proves nothing: no event.

    Sabotage-proof: treat every ``stat`` failure as absent in
    ``_still_present`` (``return False``); this test fails because the note
    is tombstoned on an indeterminate error.
    """
    files = {"locked/note.md": "n"}
    vault = _vault(tmp_path, files)
    locked = vault / "locked"
    reconciler = FullScanReconciler(
        vault,
        [CollectionScanSpec(path=".")],
        read_text=_replacing_reader(locked / "note.md", lambda _p: locked.chmod(0o000)),
    )
    try:
        ops = _ops(reconciler, _known(files))
    finally:
        locked.chmod(0o755)

    assert ops == []


# Root lists any directory regardless of mode bits; Windows has no POSIX mode bits.
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user for an unreadable directory",
)
def test_an_out_of_vault_alias_whose_target_becomes_unreadable_during_read_is_tombstoned(tmp_path: Path) -> None:
    """An indeterminate ``stat`` failure keeps a note only inside the vault.

    The alias points outside the vault, whose directory becomes unreadable
    between the walk and the read; ``fetch`` refuses the path, so it is
    absent.

    Sabotage-proof: drop ``and self._inside_vault(abs_path)`` from the
    ``except`` branch of ``_still_present``; this test fails because the
    out-of-vault alias keeps its stale index.
    """
    vault = _vault(tmp_path, {"alpha.md": "a"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "target.md").write_text("t", encoding="utf-8")
    (vault / "alias.md").symlink_to(elsewhere / "target.md")
    known = {**_known({"alpha.md": "a"}), "alias.md": hash_content("t")}
    reconciler = FullScanReconciler(
        vault,
        [CollectionScanSpec(path=".")],
        read_text=_replacing_reader(vault / "alias.md", lambda _p: elsewhere.chmod(0o000)),
    )
    try:
        ops = _ops(reconciler, known)
    finally:
        elsewhere.chmod(0o755)

    assert ops == [("deleted", "alias.md")]
