"""Filesystem helpers shared by the Obsidian connector + reconciler.

Kept private (``_fs``) — these are implementation details that nudge
file walking + mime sniffing into one place so the connector and
reconciler don't grow parallel scanners. Public ``__init__.py`` does
not re-export anything from this module.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterable, Iterator
from functools import cache, lru_cache
from pathlib import Path, PurePath, PureWindowsPath

# Extension → mime mapping for the file families an Obsidian vault
# typically holds. ``.md`` is the canonical Obsidian note; the rest are
# documents the operator may have dropped into the vault for the
# pipeline to ingest. Per the SC-3 split, the extractor selection
# happens upstream — the connector only provides a mime hint.
_EXT_TO_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}

# Magic-byte signatures used as a cheap sanity check on mime detection.
# Per the spec, the extractor selection happens upstream — this is just
# the connector's hint. We surface PDF / PNG / JPEG / ZIP-container
# (the office formats) and fall back to the extension map.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

DEFAULT_MIME = "application/octet-stream"


def mime_for_path(path: Path) -> str:
    """Resolve the mime hint for a vault file.

    Extension-first (cheap, covers 99% of vault content); the caller
    may layer magic-byte detection on top via :func:`mime_for_bytes`
    when the extension is missing or generic.
    """
    suffix = path.suffix.lower()
    return _EXT_TO_MIME.get(suffix, DEFAULT_MIME)


def mime_for_bytes(raw: bytes, fallback: str = DEFAULT_MIME) -> str:
    """Sniff a mime from the first bytes of a payload.

    Returns ``fallback`` when none of the configured magic signatures
    match. Reserved for the small set of cases where the path lacks a
    useful extension; the connector defaults to the extension-based
    mime.
    """
    for sig, mime in _MAGIC_SIGNATURES:
        if raw.startswith(sig):
            return mime
    return fallback


def iter_collection_files(
    *,
    vault_root: Path,
    collection_path: str,
    glob: str,
    exclude: Iterable[str],
) -> Iterator[Path]:
    """Yield absolute file paths under one configured collection.

    ``collection_path`` is vault-root-relative; an empty string OR
    ``"."`` means "the whole vault". Files matching any string in
    ``exclude`` (substring match against the relative path) are
    skipped — this is the Obsidian convention for hiding work-in-
    progress directories from indexing.
    """
    base = vault_root if collection_path in ("", ".") else vault_root / collection_path
    if not base.exists() or not base.is_dir():
        return
    exclude_tuple = tuple(exclude)
    for abs_path in sorted(base.glob(glob)):
        if not abs_path.is_file():
            continue
        rel_str = abs_path.relative_to(vault_root).as_posix()
        if any(token and token in rel_str for token in exclude_tuple):
            continue
        yield abs_path


# The host's path flavour — ``PurePosixPath`` or ``PureWindowsPath`` — whose
# separator and case rules ``pathlib.Path.glob`` applies on this platform.
NATIVE_FLAVOUR: type[PurePath] = type(PurePath())

# Python 3.13 changed ``Path.glob``: a pattern ending in ``**`` now yields
# files as well as directories (3.12 yields directories only). The rule
# follows the running interpreter so the predicate agrees with the
# ``Path.glob`` that drives :func:`iter_collection_files`.
TRAILING_RECURSIVE_MATCHES_FILES: bool = sys.version_info >= (3, 13)


def collection_accepts(
    rel_path: str,
    *,
    collection_path: str,
    glob: str,
    exclude: Iterable[str],
    flavour: type[PurePath] = NATIVE_FLAVOUR,
) -> bool:
    """True when one configured collection indexes the file at ``rel_path``.

    The predicate twin of :func:`iter_collection_files`, for watchdog events
    whose path may no longer exist: no filesystem access, same answer as the
    walk. ``rel_path`` is the file's vault-root-relative POSIX path. It is
    accepted when it sits under ``collection_path`` (``""`` or ``"."`` is the
    whole vault; ``./notes`` and ``notes/.`` normalise to ``notes``), the part
    below the collection matches ``glob`` as :meth:`pathlib.Path.glob` would,
    and no non-empty ``exclude`` token is a substring of ``rel_path``. A glob
    ending in a separator (``*.md/``) selects directories only, so it accepts
    no file. ``flavour`` sets the separator and case rules; it defaults to the
    host's, which is what ``Path.glob`` uses.
    """
    if _directory_only(glob, flavour) or any(token and token in rel_path for token in exclude):
        return False
    parts = _parts(rel_path, flavour)
    base = _parts(collection_path, flavour)
    if not _parts_under(parts, base, flavour):
        return False
    return _parts_match(parts[len(base) :], _parts(glob, flavour), flavour)


def _directory_only(glob: str, flavour: type[PurePath]) -> bool:
    """True when ``glob`` ends in a separator, so ``Path.glob`` selects directories only."""
    separators = ("/", "\\") if issubclass(flavour, PureWindowsPath) else ("/",)
    return glob.endswith(separators)


@lru_cache(maxsize=128)
def _parts(text: str, flavour: type[PurePath]) -> tuple[str, ...]:
    """Split a path or glob into components under ``flavour``'s separator rules.

    ``pathlib`` drops ``.`` and empty components, so ``""``, ``"."``,
    ``"./notes"`` and ``"notes/."`` normalise to ``()``, ``()``, ``("notes",)``
    and ``("notes",)`` — the same spellings ``Path.glob`` accepts.
    """
    return flavour(text).parts


def _parts_under(child: tuple[str, ...], parent: tuple[str, ...], flavour: type[PurePath]) -> bool:
    """True when ``child`` is strictly beneath ``parent``, comparing with ``flavour``'s case rules.

    Strictly: a file is never its own collection directory, which
    :func:`iter_collection_files` walks as a directory.
    """
    return len(child) > len(parent) and flavour(*child[: len(parent)]) == flavour(*parent)


def _parts_match(parts: tuple[str, ...], patterns: tuple[str, ...], flavour: type[PurePath]) -> bool:
    """Match path components against glob components, ``pathlib.Path.glob`` style.

    ``*`` and ``?`` stay inside one component and match dotfiles. A ``**``
    component matches zero or more whole directories, so a segment before it
    must name a directory: ``*/**`` never matches a root-level file. A
    trailing ``**`` selects directories only on Python 3.12, so it matches no
    file; from 3.13 it also selects every file beneath
    (:data:`TRAILING_RECURSIVE_MATCHES_FILES`).

    Memoised on ``(path index, pattern index)``, so repeated ``**``
    components cost O(len(parts)² · len(patterns)) rather than branching
    combinatorially on a deep non-matching path.
    """

    @cache
    def match(i: int, j: int) -> bool:
        if j == len(patterns):
            return i == len(parts)
        head = patterns[j]
        if head == "**":
            if j + 1 == len(patterns):
                return TRAILING_RECURSIVE_MATCHES_FILES and i < len(parts)
            return any(match(k, j + 1) for k in range(i, len(parts)))
        return i < len(parts) and flavour(parts[i]).match(head) and match(i + 1, j + 1)

    return match(0, 0)


# ``os.open`` flags for a read that will not follow a final symlink, block on a
# FIFO or translate bytes; each is 0 where the host lacks it.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


# ``fcntl`` command that reports an open descriptor's path (macOS); Linux
# answers through ``/proc/self/fd`` instead, and Windows through neither.
if sys.platform == "win32":
    _F_GETPATH: int | None = None
else:
    import fcntl

    _F_GETPATH = getattr(fcntl, "F_GETPATH", None)
_PATH_MAX = 1024


def _descriptor_path(fd: int) -> Path | None:
    """The path the OS reports for an open descriptor — the name it was opened by.

    Call it straight after the open: macOS reports the latest name looked
    up for the file. ``None`` where the platform cannot say (Windows).
    """
    if _F_GETPATH is not None:
        return Path(os.fsdecode(fcntl.fcntl(fd, _F_GETPATH, bytes(_PATH_MAX)).rstrip(b"\0")))
    proc = f"/proc/self/fd/{fd}"
    if os.path.islink(proc):
        return Path(os.readlink(proc))
    return None


@lru_cache(maxsize=16)
def _descriptor_root(vault_root: Path) -> Path:
    """``vault_root`` as descriptor paths spell it.

    A case-insensitive volume (macOS) reports the on-disk casing, which may
    differ from the configured root; asking for the root's own descriptor
    path compares like with like.
    """
    fd = os.open(vault_root, os.O_RDONLY)
    try:
        return _descriptor_path(fd) or vault_root
    finally:
        os.close(fd)


class OutsideVaultError(ValueError):
    """A path resolves outside the vault, so the connector will not read it."""


def _resolved_inside(abs_path: Path, vault_root: Path) -> Path:
    real = Path(os.path.realpath(abs_path))
    if not real.is_relative_to(vault_root):
        raise OutsideVaultError(f"{abs_path} resolves outside the vault")
    return real


def read_at(real: Path, abs_path: Path, vault_root: Path, size: int = -1) -> tuple[os.stat_result, bytes]:
    """Stat and read ``real`` — ``abs_path`` as resolved earlier — only while it is still that file.

    ``real`` is opened without following a final symlink, then, before a
    byte is read: the opened file must be a regular file (a FIFO or device
    swapped in never reads as empty content); ``abs_path`` is resolved
    again and must still land inside the vault on the very file opened;
    and the OS must report the descriptor itself under the vault — the
    name it was opened by, which no later swap of a parent directory can
    change. So no byte outside the vault is ever read. (Windows reports no
    descriptor path; there the re-resolve alone guards, and creating
    symlinks needs privilege.) Opening needs no directory listing
    permission and works on every platform. Returns the opened file's
    ``fstat`` with up to ``size`` bytes (all of them by default), so
    metadata and content come from the same verified file.

    Raises:
        OutsideVaultError: ``abs_path`` now resolves outside the vault, or to
            a file other than the one opened.
        OSError: the path is gone, ``real`` is now a symlink, or it is not a
            regular file.
    """
    with os.fdopen(os.open(real, _READ_FLAGS), "rb") as fh:
        opened = os.fstat(fh.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(abs_path))
        # Ask before any lookup by name: macOS reports the latest name used.
        held = _descriptor_path(fh.fileno())
        if held is not None and not held.is_relative_to(_descriptor_root(vault_root)):
            raise OutsideVaultError(f"{abs_path} was opened outside the vault")
        now = os.stat(_resolved_inside(abs_path, vault_root))
        if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
            raise OutsideVaultError(f"{abs_path} changed while it was opened")
        return opened, fh.read(size)


def read_inside_vault(abs_path: Path, vault_root: Path, size: int = -1) -> tuple[os.stat_result, bytes]:
    """Stat and read ``abs_path`` only if it resolves inside ``vault_root``, with no symlink race.

    ``vault_root`` must already be resolved. See :func:`read_at`.

    Raises:
        OutsideVaultError: ``abs_path`` resolves outside ``vault_root``.
        OSError: the path is gone, is not readable, or changed during the read.
    """
    return read_at(_resolved_inside(abs_path, vault_root), abs_path, vault_root, size)


def read_bytes_inside_vault(abs_path: Path, vault_root: Path) -> bytes:
    """Every byte of ``abs_path``, read as :func:`read_inside_vault` does."""
    return read_inside_vault(abs_path, vault_root)[1]


def read_text_for_hash(abs_path: Path, vault_root: Path) -> str:
    """Read a file inside the vault as UTF-8 for content-hashing.

    Reads through :func:`read_bytes_inside_vault`, so it never reads
    outside the vault. Binary files (PDF / DOCX / etc.) round-trip through
    ``utf-8 + errors="ignore"`` — the hash is over whatever bytes the
    file contained, the loss of round-trippability is acceptable
    because reconciliation only needs the hash to detect drift, not
    to reconstruct the file. Line endings are normalised to ``\\n``, as
    a text-mode read does, so the hash of a CRLF note does not change.
    """
    text = read_bytes_inside_vault(abs_path, vault_root).decode("utf-8", errors="ignore")
    return text.replace("\r\n", "\n").replace("\r", "\n")
