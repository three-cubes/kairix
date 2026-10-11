"""Unit tests for :mod:`kairix.connectors.obsidian._fs`.

Covers the filesystem helpers that back the Obsidian connector +
reconciler: mime resolution, collection walking, and the
hash-friendly file-read path.

F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from kairix.connectors.obsidian.fs import (
    DEFAULT_MIME,
    collection_accepts,
    glob_selects_files_under,
    iter_collection_files,
    mime_for_bytes,
    mime_for_path,
    read_text_for_hash,
    rebase_glob,
)

# ---------------------------------------------------------------------------
# mime_for_path — extension-first dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("note.md", "text/markdown"),
        ("note.markdown", "text/markdown"),
        ("plain.txt", "text/plain"),
        ("paper.pdf", "application/pdf"),
        ("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("doc.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("page.html", "text/html"),
        ("page.HTM", "text/html"),
        ("data.json", "application/json"),
        ("config.yml", "application/x-yaml"),
        ("rows.csv", "text/csv"),
        ("unknown.xyz", DEFAULT_MIME),
        ("noext", DEFAULT_MIME),
    ],
)
def test_mime_for_path_extension_dispatch(name: str, expected: str) -> None:
    """Extension dispatch resolves the documented set of vault mimes.

    Sabotage-proof: empty the ``_EXT_TO_MIME`` dict; every parametrised
    case (except the two ``DEFAULT_MIME`` rows) flips to the default
    and the equality assertion fails.
    """
    assert mime_for_path(Path(name)) == expected


# ---------------------------------------------------------------------------
# mime_for_bytes — magic-byte fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mime_for_bytes_detects_pdf_signature() -> None:
    """The classic ``%PDF-`` signature resolves to ``application/pdf``.

    Sabotage-proof: drop the PDF signature from ``_MAGIC_SIGNATURES``;
    the fallback then returns the default.
    """
    assert mime_for_bytes(b"%PDF-1.7\nfake body") == "application/pdf"


@pytest.mark.unit
def test_mime_for_bytes_detects_png_signature() -> None:
    """The PNG header byte sequence resolves to ``image/png``.

    Sabotage-proof: drop the PNG signature row; the fallback returns
    the default.
    """
    assert mime_for_bytes(b"\x89PNG\r\n\x1a\nfake body") == "image/png"


@pytest.mark.unit
def test_mime_for_bytes_detects_jpeg_signature() -> None:
    """The JPEG ``\\xff\\xd8\\xff`` prefix resolves to ``image/jpeg``.

    Sabotage-proof: drop the JPEG signature row; the fallback returns
    the default.
    """
    assert mime_for_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"


@pytest.mark.unit
def test_mime_for_bytes_falls_back_to_default_on_unknown_payload() -> None:
    """Bytes that match no signature surface the fallback verbatim.

    Sabotage-proof: change the function to always return ``"x"``; the
    fallback assertion fails.
    """
    assert mime_for_bytes(b"random noise", fallback="text/markdown") == "text/markdown"
    assert mime_for_bytes(b"random noise") == DEFAULT_MIME


# ---------------------------------------------------------------------------
# iter_collection_files — vault walking
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_iter_collection_files_returns_empty_when_collection_missing(tmp_path: Path) -> None:
    """A non-existent collection path yields no files.

    Sabotage-proof: drop the ``if not base.exists()`` guard; the
    function then raises instead of yielding nothing.
    """
    out = list(
        iter_collection_files(
            vault_root=tmp_path,
            collection_path="missing-subdir",
            glob="**/*.md",
            exclude=(),
        )
    )
    assert out == []


@pytest.mark.unit
def test_iter_collection_files_yields_matching_files(tmp_path: Path) -> None:
    """The glob picks up matching files; non-matching are skipped.

    Sabotage-proof: change the glob inside the function to ``*``; the
    test then sees the ignored ``notes.txt``.
    """
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("body", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("body", encoding="utf-8")
    out = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in iter_collection_files(
            vault_root=tmp_path,
            collection_path=".",
            glob="**/*.md",
            exclude=(),
        )
    )
    assert out == ["a.md", "nested/b.md"]


@pytest.mark.unit
def test_iter_collection_files_honours_exclude_substring(tmp_path: Path) -> None:
    """A relative-path substring in ``exclude`` removes matching files.

    Sabotage-proof: drop the exclude-check in the loop; the result then
    includes the WIP file.
    """
    (tmp_path / "live.md").write_text("body", encoding="utf-8")
    wip = tmp_path / "WIP"
    wip.mkdir()
    (wip / "draft.md").write_text("body", encoding="utf-8")
    out = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in iter_collection_files(
            vault_root=tmp_path,
            collection_path=".",
            glob="**/*.md",
            exclude=("WIP/",),
        )
    )
    assert out == ["live.md"]


@pytest.mark.unit
def test_iter_collection_files_skips_directories(tmp_path: Path) -> None:
    """Directory matches inside the glob are filtered out.

    Sabotage-proof: drop the ``if not abs_path.is_file()`` check; the
    function then yields the directory and the read_text-for-hash
    test below would crash on a directory.
    """
    (tmp_path / "a.md").write_text("body", encoding="utf-8")
    # Use ``*`` glob so subdirectories appear in raw iteration.
    sub = tmp_path / "subdir"
    sub.mkdir()
    out = sorted(p.name for p in iter_collection_files(vault_root=tmp_path, collection_path=".", glob="*", exclude=()))
    assert "a.md" in out
    assert "subdir" not in out


@pytest.mark.unit
def test_iter_collection_files_handles_explicit_subdirectory(tmp_path: Path) -> None:
    """``collection_path`` may be a subdirectory; the walk starts there.

    Sabotage-proof: change ``base = vault_root / collection_path`` to
    always use the vault root; the test then sees the file outside
    the subdirectory.
    """
    (tmp_path / "outside.md").write_text("body", encoding="utf-8")
    sub = tmp_path / "notes"
    sub.mkdir()
    (sub / "inside.md").write_text("body", encoding="utf-8")
    out = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in iter_collection_files(
            vault_root=tmp_path,
            collection_path="notes",
            glob="**/*.md",
            exclude=(),
        )
    )
    assert out == ["notes/inside.md"]


# ---------------------------------------------------------------------------
# read_text_for_hash — binary-safe fallthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_text_for_hash_roundtrips_utf8(tmp_path: Path) -> None:
    """UTF-8 markdown files round-trip cleanly.

    Sabotage-proof: change the encoding to ``"ascii"``; the unicode
    em-dash below crashes the read.
    """
    target = tmp_path / "note.md"
    body = "# Alpha — first note\n"
    target.write_text(body, encoding="utf-8")
    assert read_text_for_hash(target) == body


@pytest.mark.unit
def test_read_text_for_hash_handles_binary_payload(tmp_path: Path) -> None:
    """A binary file's bytes round-trip with ``errors='ignore'``.

    Sabotage-proof: drop the ``errors='ignore'`` argument; the read
    raises ``UnicodeDecodeError`` on the PDF bytes.
    """
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.7\n\x00\x01\xffbody")
    out = read_text_for_hash(target)
    # The function only needs to return something hashable + stable;
    # the exact decoded form is implementation-defined.
    assert isinstance(out, str)
    assert "PDF-1.7" in out


# ---------------------------------------------------------------------------
# collection_accepts — predicate twin of iter_collection_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "rel_path,collection_path,glob,exclude,expected",
    [
        ("a.md", ".", "**/*.md", (), True),
        ("x/y/a.md", "", "**/*.md", (), True),
        ("a.json", ".", "**/*.md", (), False),
        (".obsidian/workspace.json", ".", "**/*.md", (), False),
        ("notes/a.md", "notes", "**/*.md", (), True),
        ("notes/deep/a.md", "notes/", "**/*.md", (), True),
        ("notes-old/a.md", "notes", "**/*.md", (), False),
        ("other/a.md", "notes", "**/*.md", (), False),
        ("drafts/a.md", ".", "**/*.md", ("drafts",), False),
        ("x/a.md", ".", "*.md", (), False),
        ("a.md", ".", "*.md", (), True),
        ("x/a.md", ".", "x/*.md", (), True),
        ("y/a.md", ".", "x/*.md", (), False),
        ("x/a.md", ".", "x/**/a.md", (), True),
        ("a.md", ".", "**/*.md", ("",), True),
        ("notes/a.md", "./notes", "**/*.md", (), True),
        ("notes/a.md", "notes/.", "**/*.md", (), True),
        ("x/a.md", ".", "./**/*.md", (), True),
        ("secret.md", ".", "*/**", (), False),
        ("a.md", ".", "*.md/", (), False),
        ("notes/a.md", "notes/a.md", ".", (), False),
        ("x/a.md", ".", "x/a.md/b", (), False),
        ("Notes/a.md", "notes", "**/*.md", (), False),
    ],
)
def test_collection_accepts_cases(
    rel_path: str, collection_path: str, glob: str, exclude: tuple[str, ...], expected: bool
) -> None:
    """Base path, glob (``**`` spans directories) and substring exclude decide membership.

    Sabotage-proof: make the non-trailing ``**`` branch of ``_parts_match`` try
    ``range(i + 1, ...)`` (one-or-more directories); the ``a.md`` / ``**/*.md``
    row fails. Let a trailing ``**`` match zero components; the ``*/**`` row
    fails. Split ``_parts`` with ``text.split("/")``; the ``./notes`` rows fail.
    Let ``_parts_under`` accept ``len(child) == len(parent)``; the row whose
    path is its own collection fails.
    Drop the exclude check; the ``drafts`` row fails. Drop the trailing
    ``/`` in the base-prefix check; the ``notes-old`` row fails.
    """
    assert collection_accepts(rel_path, collection_path=collection_path, glob=glob, exclude=exclude) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "collection_path,glob,exclude",
    [
        (".", "**/*.md", ()),
        ("notes", "**/*.md", ("wip",)),
        (".", "*.md", ()),
        ("notes", "sub/*.txt", ()),
        ("./notes", "**/*.md", ()),
        ("notes/.", "**/*.md", ()),
        (".", "./**/*.md", ()),
        (".", "*/**", ()),
        (".", "*.md/**", ()),
        ("notes", "**", ()),
        (".", "*.md/", ()),
    ],
)
def test_collection_accepts_agrees_with_iter_collection_files(
    tmp_path: Path, collection_path: str, glob: str, exclude: tuple[str, ...]
) -> None:
    """Every file in a mixed tree is matched exactly when the walk yields it.

    Sabotage-proof: make ``collection_accepts`` ignore ``collection_path``;
    the ``notes`` rows fail because root-level files now match.
    """
    rels = [
        "top.md",
        "top.txt",
        ".obsidian/workspace.json",
        ".obsidian/hidden.md",
        "notes/a.md",
        "notes/sub/b.md",
        "notes/sub/c.txt",
        "notes/wip/d.md",
        "notes-old/e.md",
        "dir.md/inner.md",
    ]
    for rel in rels:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    walked = {
        p.relative_to(tmp_path).as_posix()
        for p in iter_collection_files(vault_root=tmp_path, collection_path=collection_path, glob=glob, exclude=exclude)
    }
    matched = {
        rel for rel in rels if collection_accepts(rel, collection_path=collection_path, glob=glob, exclude=exclude)
    }
    assert matched == walked


# ---------------------------------------------------------------------------
# rebase_glob — the scanner's rule for what a glob selects under a subdirectory
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("glob", "prefix", "expected"),
    [
        ("notes/**/*.md", "notes", ("**/*.md",)),
        ("**/*.md", "notes", ("**/*.md",)),
        ("*/deep/*.md", "notes", ("deep/*.md",)),
        ("**/deep/*.md", "notes", ("**/deep/*.md",)),
        ("**/notes/**/*.md", "notes", ("**/*.md", "**/notes/**/*.md")),
        ("notes/*.md", "notes/deep", ()),
        ("archive/**/*.md", "notes", ()),
        ("notes", "notes", ()),
        ("daily/*.md/", "daily", ()),
        ("*.md", "", ("*.md",)),
    ],
)
def test_rebase_glob_re_roots_a_glob_at_a_subdirectory(glob: str, prefix: str, expected: tuple[str, ...]) -> None:
    """Sabotage-proof: drop the ``**`` absorb branch; the ``**`` rows fail. Drop the
    directory-only guard; the ``daily/*.md/`` row fails."""
    assert rebase_glob(glob, prefix, flavour=PurePosixPath) == expected


@pytest.mark.unit
def test_rebase_glob_follows_the_platform_flavour() -> None:
    """Windows matches case-insensitively, POSIX does not."""
    assert rebase_glob("Notes/**/*.md", "notes", flavour=PureWindowsPath) == ("**/*.md",)
    assert rebase_glob("Notes/**/*.md", "notes", flavour=PurePosixPath) == ()


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_rebasing_repeated_recursive_globs_onto_a_deep_prefix_stays_fast() -> None:
    """Each (pattern, prefix) state is derived once.

    Sabotage-proof: remove the ``@cache`` on ``derive``; this test runs past
    the timeout.
    """
    glob = "/".join(["**"] * 14 + ["*.md"])
    prefix = "/".join(f"d{i}" for i in range(24))
    assert rebase_glob(glob, prefix, flavour=PurePosixPath) == tuple(
        sorted("/".join(["**"] * k + ["*.md"]) for k in range(1, 15))
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("glob", "prefix", "trailing", "expected"),
    [
        ("**/*.md", "", False, True),
        ("**/*.md", "notes", False, True),
        ("**", "", False, False),
        ("**", "", True, True),
        ("daily/**", "daily", False, False),
        ("daily/**", "daily", True, True),
        ("*.md/", "", True, False),
        ("*.md", "sub", True, False),
    ],
)
def test_glob_selects_files_under_follows_the_interpreters_trailing_rule(
    glob: str, prefix: str, trailing: bool, expected: bool
) -> None:
    """Sabotage-proof: drop the trailing-``**`` check; the ``False`` trailing rows
    for ``**`` and ``daily/**`` fail. Ignore ``trailing_recursive_matches_files``
    (treat it as ``False``); the ``True`` trailing rows fail."""
    assert (
        glob_selects_files_under(glob, prefix, flavour=PurePosixPath, trailing_recursive_matches_files=trailing)
        is expected
    )
