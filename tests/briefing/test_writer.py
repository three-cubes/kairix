"""
Tests for the briefing writer (kairix/briefing/writer.py).

Uses output_dir parameter for dependency injection — no monkey-patching needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.agents.briefing.writer import write_briefing


@pytest.mark.unit
class TestWriteBriefing:
    @pytest.mark.unit
    def test_creates_output_file(self, tmp_path):
        out = write_briefing(
            "builder",
            "## Pending & Blocked\nNone.",
            sources_count=3,
            token_estimate=200,
            output_dir=tmp_path,
        )
        assert out.exists()
        assert out.name == "builder-latest.md"

    @pytest.mark.unit
    def test_creates_directory_if_missing(self, tmp_path):
        target_dir = tmp_path / "nested" / "briefing"
        assert not target_dir.exists()
        out = write_briefing(
            "shape",
            "## Section\nContent",
            sources_count=2,
            token_estimate=100,
            output_dir=target_dir,
        )
        assert target_dir.exists()
        assert out.exists()

    @pytest.mark.unit
    def test_file_contains_header(self, tmp_path):
        out = write_briefing(
            "builder",
            "## Pending\nNone.",
            sources_count=4,
            token_estimate=150,
            output_dir=tmp_path,
        )
        content = out.read_text()
        assert "# Agent Briefing — builder" in content
        assert "_Generated:" in content
        assert "Sources: 4" in content
        assert "Tokens: ~150" in content

    @pytest.mark.unit
    def test_file_contains_body(self, tmp_path):
        body = "## Pending & Blocked\n- Fix the bug\n\n## Recent Decisions\n- ADR-001"
        out = write_briefing("builder", body, output_dir=tmp_path)
        content = out.read_text()
        assert "Fix the bug" in content
        assert "ADR-001" in content

    @pytest.mark.unit
    def test_overwrites_existing_file(self, tmp_path):
        write_briefing("builder", "First content", sources_count=1, output_dir=tmp_path)
        out = write_briefing("builder", "Second content", sources_count=2, output_dir=tmp_path)
        content = out.read_text()
        assert "Second content" in content
        assert "First content" not in content

    @pytest.mark.unit
    def test_correct_filename_per_agent(self, tmp_path):
        b_out = write_briefing("builder", "b content", output_dir=tmp_path)
        s_out = write_briefing("shape", "s content", output_dir=tmp_path)
        assert b_out.name == "builder-latest.md"
        assert s_out.name == "shape-latest.md"

    @pytest.mark.unit
    def test_returns_path_object(self, tmp_path):
        result = write_briefing("builder", "content", output_dir=tmp_path)
        assert isinstance(result, Path)

    @pytest.mark.unit
    def test_oserror_is_re_raised_when_target_path_is_a_directory(self, tmp_path, caplog):
        """When write_text() fails, write_briefing logs and re-raises OSError.

        The function deliberately catches OSError only to log it, then
        re-raises so the caller (briefing pipeline) sees the failure
        instead of silently swallowing it. We trigger the failure by
        pre-creating the would-be output file path as a directory:
        ``output_dir / "{agent}-latest.md"`` is then a directory, and
        ``Path.write_text`` raises ``IsADirectoryError`` (an OSError
        subclass) when it tries to open it for write.
        """
        import logging

        # The output path the writer computes is output_dir / "{agent}-latest.md".
        # Pre-create THAT exact path as a directory so write_text fails.
        clashing_path = tmp_path / "builder-latest.md"
        clashing_path.mkdir()

        with caplog.at_level(logging.ERROR, logger="kairix.agents.briefing.writer"):
            with pytest.raises(OSError):
                write_briefing(
                    "builder",
                    "content",
                    sources_count=1,
                    token_estimate=10,
                    output_dir=tmp_path,
                )

        assert any("failed to write briefing" in rec.message for rec in caplog.records), (
            "expected an error log when write_text raises"
        )
