"""
Tests for scripts/chunk-daily-files.py — TMP-4: daily memory log chunker.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "chunk-daily-files.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chunk_daily_files", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib Loader protocol omits exec_module in stubs
    return mod


_mod = _load_script()
split_into_sections = _mod.split_into_sections
chunk_file = _mod.chunk_file
write_chunk = _mod.write_chunk
find_memory_logs = _mod.find_memory_logs


# ---------------------------------------------------------------------------
# split_into_sections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_splits_on_h2_headings() -> None:
    content = "## Morning\nDid standup.\n\n## Afternoon\nWrote code."
    sections = split_into_sections(content)
    assert len(sections) == 2
    assert sections[0] == ("Morning", "Did standup.")
    assert sections[1] == ("Afternoon", "Wrote code.")


@pytest.mark.unit
def test_preamble_before_first_heading() -> None:
    content = "Intro text.\n\n## Section One\nBody."
    sections = split_into_sections(content)
    assert len(sections) == 2
    assert sections[0] == ("", "Intro text.")
    assert sections[1] == ("Section One", "Body.")


@pytest.mark.unit
def test_empty_sections_excluded() -> None:
    content = "## Empty\n   \n## Full\nContent here."
    sections = split_into_sections(content)
    assert len(sections) == 1
    assert sections[0][0] == "Full"


@pytest.mark.unit
def test_body_only_no_headings_returns_single() -> None:
    content = "Just some body text with no headings."
    sections = split_into_sections(content)
    assert len(sections) == 1
    assert sections[0] == ("", "Just some body text with no headings.")


# ---------------------------------------------------------------------------
# chunk_file
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_extracts_date_from_filename(tmp_path: Path) -> None:
    log = tmp_path / "2026-04-08.md"
    log.write_text("## Morning\nDid something.\n")
    chunks = chunk_file(log, tmp_path)
    assert len(chunks) == 1
    assert chunks[0]["date"] == "2026-04-08"


@pytest.mark.integration
def test_chunk_has_heading_and_body(tmp_path: Path) -> None:
    log = tmp_path / "2026-04-08.md"
    log.write_text("## Work Log\nFixed bug.\n\n## Reflections\nLearned things.\n")
    chunks = chunk_file(log, tmp_path)
    assert len(chunks) == 2
    assert chunks[0]["heading"] == "Work Log"
    assert "Fixed bug" in chunks[0]["body"]


@pytest.mark.integration
def test_non_memory_log_returns_empty(tmp_path: Path) -> None:
    log = tmp_path / "not-a-date.md"
    log.write_text("## Section\nBody.\n")
    chunks = chunk_file(log, tmp_path)
    assert chunks == []


@pytest.mark.integration
def test_frontmatter_stripped_from_body(tmp_path: Path) -> None:
    log = tmp_path / "2026-04-08.md"
    log.write_text("---\ndate: 2026-04-08\n---\n\n## Section\nReal body.\n")
    chunks = chunk_file(log, tmp_path)
    assert len(chunks) == 1
    assert "---" not in chunks[0]["body"]
    assert "Real body" in chunks[0]["body"]


# ---------------------------------------------------------------------------
# write_chunk
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_output_file_created(tmp_path: Path) -> None:
    chunk = {
        "heading": "Work Log",
        "body": "Did important work.",
        "date": "2026-04-08",
        "source": "04-Agent-Knowledge/growth/memory/2026-04-08.md",
        "filename": "2026-04-08-work-log-00.md",
    }
    out = write_chunk(chunk, tmp_path)
    assert out.exists()
    content = out.read_text()
    assert "date: 2026-04-08" in content
    assert "section_heading: " in content
    assert "Did important work" in content


@pytest.mark.integration
def test_chunk_frontmatter_has_source(tmp_path: Path) -> None:
    chunk = {
        "heading": "Reflections",
        "body": "Body text.",
        "date": "2026-04-08",
        "source": "04-Agent-Knowledge/memory/2026-04-08.md",
        "filename": "2026-04-08-reflections-01.md",
    }
    out = write_chunk(chunk, tmp_path)
    content = out.read_text()
    assert 'source: "04-Agent-Knowledge/memory/2026-04-08.md"' in content


# ---------------------------------------------------------------------------
# find_memory_logs
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_find_memory_logs_only_dated_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    mem_dir = vault / "memory"
    mem_dir.mkdir()
    (mem_dir / "2026-04-08.md").write_text("## Section\nBody.")
    (mem_dir / "2026-04-09.md").write_text("## Section\nBody.")
    (mem_dir / "notes.md").write_text("Not a dated log.")
    (mem_dir / "not-dated.md").write_text("Also not dated.")

    logs = find_memory_logs(vault)
    names = {p.name for p in logs}
    assert "2026-04-08.md" in names
    assert "2026-04-09.md" in names
    assert "notes.md" not in names
    assert "not-dated.md" not in names
