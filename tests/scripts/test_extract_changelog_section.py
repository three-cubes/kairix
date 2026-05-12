"""Unit tests for ``scripts/extract_changelog_section.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the extractor module directly — it lives outside the kairix package.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "extract_changelog_section.py"
_spec = importlib.util.spec_from_file_location("_extractor", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_extractor = importlib.util.module_from_spec(_spec)
sys.modules["_extractor"] = _extractor
_spec.loader.exec_module(_extractor)


pytestmark = pytest.mark.unit


_SAMPLE = """# Changelog

## [Unreleased]

### Added

- New widget.
- Another thing.

### Fixed

- A bug.

## [2026.5.10] - 2026-05-10 — Worker stability

### Added

- L0 health probe.

## [2026.5.9] - 2026-05-08

### Changed

- Schema migration.
"""


def test_extracts_unreleased_section_body() -> None:
    body = _extractor.extract_section(_SAMPLE, "Unreleased")
    assert "### Added" in body
    assert "New widget." in body
    assert "### Fixed" in body
    assert "A bug." in body
    # Excludes the heading line itself
    assert "## [Unreleased]" not in body
    # Stops before the next ## section
    assert "L0 health probe" not in body
    assert "Worker stability" not in body


def test_extracts_versioned_section_body() -> None:
    body = _extractor.extract_section(_SAMPLE, "2026.5.10")
    assert "L0 health probe." in body
    assert "Schema migration" not in body  # next section excluded


def test_strips_leading_and_trailing_blank_lines() -> None:
    body = _extractor.extract_section(_SAMPLE, "Unreleased")
    assert not body.startswith("\n")
    assert not body.endswith("\n\n")


def test_missing_section_raises_keyerror() -> None:
    with pytest.raises(KeyError, match=r"\[3000\.1\.1\]"):
        _extractor.extract_section(_SAMPLE, "3000.1.1")


def test_main_writes_section_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(_SAMPLE, encoding="utf-8")

    _orig = sys.argv
    try:
        sys.argv = ["extract", str(p), "--version", "Unreleased"]
        rc = _extractor.main()
    finally:
        sys.argv = _orig

    assert rc == 0
    out = capsys.readouterr().out
    assert "New widget." in out
    assert "Worker stability" not in out


def test_main_returns_one_when_changelog_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _orig = sys.argv
    try:
        sys.argv = ["extract", str(tmp_path / "no.md")]
        rc = _extractor.main()
    finally:
        sys.argv = _orig
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_main_returns_one_when_section_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Changelog\n", encoding="utf-8")

    _orig = sys.argv
    try:
        sys.argv = ["extract", str(p), "--version", "Unreleased"]
        rc = _extractor.main()
    finally:
        sys.argv = _orig
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_main_returns_one_when_section_is_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Changelog\n\n## [Unreleased]\n\n## [2026.5.10] - 2026-05-10\n\nstuff\n", encoding="utf-8")

    _orig = sys.argv
    try:
        sys.argv = ["extract", str(p), "--version", "Unreleased"]
        rc = _extractor.main()
    finally:
        sys.argv = _orig
    assert rc == 1
    assert "empty" in capsys.readouterr().err.lower()
