"""Tests for kairix.knowledge.reflib.normalise — the main normalisation pipeline orchestrator.

Creates a temp directory structure mimicking a raw reference library and verifies
the full normalise() pipeline: frontmatter injection, boilerplate filtering,
output file count, and CATALOGUE.md generation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.knowledge.reflib.normalise import (
    NormaliseConfig,
    NormaliseReport,
    collect_markdown_files,
    discover_sources,
    is_gutenberg_text,
    normalise,
)
from kairix.knowledge.reflib.sources import SourceDef

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use two real registered sources: agentic-ai/openai-cookbook and
# engineering/adr-examples. These exist in the source registry.
_SOURCE_OPENAI = ("agentic-ai", "openai-cookbook")
_SOURCE_ADR = ("engineering", "adr-examples")


def _make_raw_library(tmp_path: Path) -> Path:
    """Create a minimal raw reference library directory tree with 3 files
    across 2 registered sources.

    Layout:
        raw/
          agentic-ai/
            openai-cookbook/
              guide.md        (800 bytes, real content)
              contributing.md (boilerplate, should be filtered)
          engineering/
            adr-examples/
              adr-001.md      (900 bytes, real content)
    """
    raw = tmp_path / "raw"

    # Source 1: openai-cookbook — 2 files (1 content, 1 boilerplate)
    cookbook_dir = raw / "agentic-ai" / "openai-cookbook"
    cookbook_dir.mkdir(parents=True)

    (cookbook_dir / "guide.md").write_text(
        "# Getting Started with the OpenAI API\n\n"
        "This guide walks through using the OpenAI API for common tasks.\n\n"
        "## Authentication\n\n"
        "You need an API key to make requests. "
        "Store it as an environment variable for security.\n\n"
        "## Making Requests\n\n"
        "Use the completions endpoint to generate text. "
        "The model parameter controls which model you use.\n\n"
        "## Best Practices\n\n"
        "Always set a reasonable max_tokens value. "
        "Use temperature to control randomness. "
        "Implement retry logic for rate limits.\n" + ("Additional detail. " * 20) + "\n",  # pad to exceed min_file_size
        encoding="utf-8",
    )

    # Boilerplate file — should be filtered by filter_collection
    (cookbook_dir / "contributing.md").write_text(
        "# Contributing\n\nPlease read our guidelines before submitting a PR.\n",
        encoding="utf-8",
    )

    # Source 2: adr-examples — 1 content file
    adr_dir = raw / "engineering" / "adr-examples"
    adr_dir.mkdir(parents=True)

    (adr_dir / "adr-001.md").write_text(
        "# ADR 001: Use Markdown for Architecture Decision Records\n\n"
        "## Context\n\n"
        "We need a lightweight format for capturing architectural decisions.\n\n"
        "## Decision\n\n"
        "We will use Markdown ADRs stored alongside the codebase. "
        "Each ADR has a number, title, context, decision, and consequences.\n\n"
        "## Consequences\n\n"
        "Teams can version-control decisions with the code they affect. "
        "New members can read the history of decisions made.\n" + ("More rationale. " * 20) + "\n",
        encoding="utf-8",
    )

    return raw


# ---------------------------------------------------------------------------
# discover_sources
# ---------------------------------------------------------------------------


class TestDiscoverSources:
    @pytest.mark.integration
    def test_finds_source_dirs(self, tmp_path: Path) -> None:
        raw = _make_raw_library(tmp_path)
        results = discover_sources(raw)

        # Should find at least the two sub-directories
        dir_pairs = [(col, dn) for col, dn, _ in results if dn]
        assert ("agentic-ai", "openai-cookbook") in dir_pairs
        assert ("engineering", "adr-examples") in dir_pairs

    @pytest.mark.integration
    def test_skips_non_directories(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "stray-file.txt").write_text("not a directory")
        results = discover_sources(raw)
        assert len([r for r in results if r[1] != ""]) == 0


# ---------------------------------------------------------------------------
# collect_markdown_files
# ---------------------------------------------------------------------------


class TestCollectMarkdownFiles:
    @pytest.mark.integration
    def test_finds_md_files(self, tmp_path: Path) -> None:
        d = tmp_path / "src"
        d.mkdir()
        (d / "a.md").write_text("# A")
        (d / "b.md").write_text("# B")
        (d / "c.txt").write_text("not markdown")
        files = collect_markdown_files(d)
        assert len(files) == 2


# ---------------------------------------------------------------------------
# is_gutenberg_text
# ---------------------------------------------------------------------------


class TestIsGutenbergText:
    @pytest.mark.unit
    def test_gutenberg_source(self) -> None:
        src = SourceDef(
            name="Test",
            collection="test",
            dir_name="test",
            licence="PD",
            licence_tier=1,
            source_url="https://www.gutenberg.org/ebooks/123",
            format="text",
        )
        assert is_gutenberg_text(src) is True

    @pytest.mark.unit
    def test_non_gutenberg_source(self) -> None:
        src = SourceDef(
            name="Test",
            collection="test",
            dir_name="test",
            licence="MIT",
            licence_tier=2,
            source_url="https://github.com/example/repo",
            format="markdown",
        )
        assert is_gutenberg_text(src) is False


# ---------------------------------------------------------------------------
# NormaliseConfig / NormaliseReport defaults
# ---------------------------------------------------------------------------


class TestDataclasses:
    @pytest.mark.unit
    def test_normalise_config_defaults(self, tmp_path: Path) -> None:
        cfg = NormaliseConfig(input_dir=tmp_path, output_dir=tmp_path / "out")
        assert cfg.max_tier == 3
        assert cfg.dry_run is False
        assert cfg.dedup is True

    @pytest.mark.unit
    def test_normalise_report_defaults(self) -> None:
        report = NormaliseReport()
        assert report.total_input == 0
        assert report.unregistered_sources == []
        assert report.collections == {}


# ---------------------------------------------------------------------------
# Full pipeline integration test
# ---------------------------------------------------------------------------


class TestNormalisePipeline:
    @pytest.mark.integration
    def test_full_pipeline_produces_correct_output(self, tmp_path: Path) -> None:
        """Run normalise() on a small temp library and verify outputs."""
        raw = _make_raw_library(tmp_path)
        out = tmp_path / "normalised"

        config = NormaliseConfig(
            input_dir=raw,
            output_dir=out,
            max_tier=3,
            min_file_size=100,
            dry_run=False,
            dedup=True,
        )

        report = normalise(config)

        # --- File count: 3 input files total (2 cookbook + 1 adr) ---
        assert report.total_input >= 2, f"Expected >= 2 input files, got {report.total_input}"

        # --- Boilerplate filtered: contributing.md should be filtered ---
        assert report.filtered_boilerplate >= 1, (
            f"Expected >= 1 boilerplate filtered, got {report.filtered_boilerplate}"
        )

        # --- Output files should exist ---
        assert report.total_output >= 2, f"Expected >= 2 output files, got {report.total_output}"

        output_md_files = list(out.rglob("*.md"))
        # Exclude CATALOGUE.md and LICENSE-NOTICES.md from content file count
        content_files = [f for f in output_md_files if f.name not in ("CATALOGUE.md", "LICENSE-NOTICES.md")]
        assert len(content_files) >= 2

        # --- Frontmatter was injected ---
        for f in content_files:
            text = f.read_text(encoding="utf-8")
            assert text.startswith("---\n"), f"Missing frontmatter in {f.name}"
            assert "---" in text[4:], f"Frontmatter not closed in {f.name}"

        # --- CATALOGUE.md was generated ---
        catalogue = out / "CATALOGUE.md"
        assert catalogue.exists(), "CATALOGUE.md was not generated"
        cat_text = catalogue.read_text(encoding="utf-8")
        assert "Reference Library Catalogue" in cat_text

        # --- LICENSE-NOTICES.md was generated ---
        licence_notices = out / "LICENSE-NOTICES.md"
        assert licence_notices.exists(), "LICENSE-NOTICES.md was not generated"

    @pytest.mark.integration
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        """dry_run=True should produce a report but not create output files."""
        raw = _make_raw_library(tmp_path)
        out = tmp_path / "dry-output"

        config = NormaliseConfig(
            input_dir=raw,
            output_dir=out,
            min_file_size=100,
            dry_run=True,
        )

        report = normalise(config)
        assert report.total_output >= 2
        assert not out.exists(), "dry_run should not create output directory"

    @pytest.mark.integration
    def test_unregistered_source_recorded(self, tmp_path: Path) -> None:
        """Source directories not in the registry should be logged."""
        raw = tmp_path / "raw"
        unknown_dir = raw / "agentic-ai" / "not-a-real-source"
        unknown_dir.mkdir(parents=True)
        (unknown_dir / "doc.md").write_text("# Unknown\n\nSome content here." + " pad" * 100)

        out = tmp_path / "out"
        config = NormaliseConfig(input_dir=raw, output_dir=out, min_file_size=50)
        report = normalise(config)
        assert "agentic-ai/not-a-real-source" in report.unregistered_sources

    @pytest.mark.integration
    def test_licence_tier_filtering(self, tmp_path: Path) -> None:
        """Sources above max_tier should be excluded."""
        raw = _make_raw_library(tmp_path)
        out = tmp_path / "out"

        # max_tier=0 excludes everything (all sources are tier >= 1)
        config = NormaliseConfig(
            input_dir=raw,
            output_dir=out,
            max_tier=0,
            min_file_size=100,
        )
        report = normalise(config)
        assert report.total_output == 0
        assert report.filtered_licence >= 1
