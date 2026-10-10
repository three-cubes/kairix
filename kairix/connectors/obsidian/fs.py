"""Filesystem helpers shared by the Obsidian connector + reconciler.

Kept private (``_fs``) — these are implementation details that nudge
file walking + mime sniffing into one place so the connector and
reconciler don't grow parallel scanners. Public ``__init__.py`` does
not re-export anything from this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from fnmatch import fnmatchcase
from pathlib import Path

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


def matches_collection(
    rel_path: str,
    *,
    collection_path: str,
    glob: str,
    exclude: Iterable[str],
) -> bool:
    """Return whether vault-relative ``rel_path`` is in one configured collection.

    The predicate twin of :func:`iter_collection_files`, for paths that
    may no longer exist (a watchdog ``deleted`` event): same base-path,
    glob and substring-exclude semantics, no filesystem access. The glob
    is matched segment-wise, ``**`` spanning zero or more directories,
    as :meth:`pathlib.Path.glob` does.
    """
    if any(token and token in rel_path for token in exclude):
        return False
    base = collection_path.strip("/")
    if base in ("", "."):
        rest = rel_path
    elif rel_path.startswith(base + "/"):
        rest = rel_path[len(base) + 1 :]
    else:
        return False
    return _glob_segments_match(glob.split("/"), rest.split("/"))


def _glob_segments_match(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    head, tail = pattern[0], pattern[1:]
    if head == "**":
        return any(_glob_segments_match(tail, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], head) and _glob_segments_match(tail, parts[1:])


def read_text_for_hash(abs_path: Path) -> str:
    """Read a file as UTF-8 for content-hashing.

    Binary files (PDF / DOCX / etc.) round-trip through
    ``utf-8 + errors="ignore"`` — the hash is over whatever bytes the
    file contained, the loss of round-trippability is acceptable
    because reconciliation only needs the hash to detect drift, not
    to reconstruct the file.
    """
    return abs_path.read_text(encoding="utf-8", errors="ignore")
