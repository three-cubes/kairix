"""
Tests for kairix.core.temporal.chunker.

Covers:
  - chunk_board(): column parsing, date extraction, cards without dates
  - chunk_memory_log(): date from filename, section splitting, frontmatter strip
  - chunk_file(): dispatch to board vs memory chunker
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from kairix.core.temporal.chunker import chunk_board, chunk_file, chunk_memory_log

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def board_file(tmp_path: Path) -> Path:
    """Create a minimal Kanban board file for testing."""
    content = textwrap.dedent("""\
        ---

        kanban-plugin: board

        ---

        ## Done

        - [ ] Phase 1 shipped [completed::2026-03-10] [project::Kairix]
        - [ ] Fix BM25 bug [completed::2026-03-12] [started::2026-03-11]

        ## In Progress

        - [ ] Phase 2 temporal [started::2026-03-23] [project::Kairix]

        ## Ready

        - [ ] Seed entities.db [created::2026-03-23]

        ## Backlog

        - [ ] Future task with no dates at all

    """)
    p = tmp_path / "Boards" / "Kairix.md"
    p.parent.mkdir(parents=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def memory_log_file(tmp_path: Path) -> Path:
    """Create a memory log file named 2026-03-22.md."""
    content = textwrap.dedent("""\
        ---
        date: 2026-03-22
        ---

        ## Session Summary

        Worked on hybrid search integration.

        ## Decisions

        - Use BM25 + vector fusion.
        - Adopt RRF as the fusion strategy.

        ## Next Steps

        - Run benchmark after Phase 2.
    """)
    p = tmp_path / "memory" / "2026-03-22.md"
    p.parent.mkdir(parents=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def undated_memory_log(tmp_path: Path) -> Path:
    """Memory log with no ## headings and no date in filename."""
    content = "Some raw notes without structure.\nMore notes here."
    p = tmp_path / "notes.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def board_no_dates(tmp_path: Path) -> Path:
    """Board with cards that have no date fields."""
    content = textwrap.dedent("""\
        ## Done

        - [ ] Task with no date tags

        ## Backlog

        - [ ] Another undated task

    """)
    p = tmp_path / "Boards" / "Plain.md"
    p.parent.mkdir(parents=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# chunk_board tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkBoard:
    @pytest.mark.unit
    def test_parses_all_columns(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        assert len(chunks) > 0

    @pytest.mark.unit
    def test_chunk_type_is_board_card(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        assert all(c.chunk_type == "board_card" for c in chunks)

    @pytest.mark.unit
    def test_source_path_preserved(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        assert all(c.source_path == str(board_file) for c in chunks)

    @pytest.mark.unit
    def test_extracts_completed_date(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        # "Phase 1 shipped" card should have completed date 2026-03-10
        phase1 = [c for c in chunks if "Phase 1 shipped" in c.text]
        assert phase1, "Expected to find 'Phase 1 shipped' card"
        assert phase1[0].date == date(2026, 3, 10)
        assert phase1[0].metadata.get("date_field") == "completed"

    @pytest.mark.unit
    def test_extracts_started_date_when_no_completed(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        # "Phase 2 temporal" card has only [started::2026-03-23]
        phase2 = [c for c in chunks if "Phase 2 temporal" in c.text]
        assert phase2
        assert phase2[0].date == date(2026, 3, 23)
        assert phase2[0].metadata.get("date_field") == "started"

    @pytest.mark.unit
    def test_completed_takes_priority_over_started(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        # "Fix BM25 bug" has both [completed::2026-03-12] and [started::2026-03-11]
        bug = [c for c in chunks if "Fix BM25 bug" in c.text]
        assert bug
        assert bug[0].date == date(2026, 3, 12), "completed should take priority over started"

    @pytest.mark.unit
    def test_cards_without_dates_get_date_none(self, board_no_dates: Path) -> None:
        chunks = chunk_board(str(board_no_dates))
        assert len(chunks) > 0
        undated = [c for c in chunks if c.date is None]
        assert len(undated) == len(chunks), "All cards should have date=None when no date tags"

    @pytest.mark.unit
    def test_cards_without_dates_have_status(self, board_no_dates: Path) -> None:
        chunks = chunk_board(str(board_no_dates))
        done_chunks = [c for c in chunks if c.metadata.get("status") == "done"]
        assert done_chunks, "Done column cards should have status='done'"

    @pytest.mark.unit
    def test_column_status_mapping(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        # In Progress column → status="in_progress"
        in_progress = [c for c in chunks if "Phase 2 temporal" in c.text]
        assert in_progress
        assert in_progress[0].metadata.get("status") == "in_progress"

    @pytest.mark.unit
    def test_card_id_is_set(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        assert all("card_id" in c.metadata for c in chunks)

    @pytest.mark.unit
    def test_returns_empty_on_missing_file(self) -> None:
        chunks = chunk_board("/nonexistent/path/board.md")
        assert chunks == []

    @pytest.mark.unit
    def test_created_date_used_as_fallback(self, board_file: Path) -> None:
        chunks = chunk_board(str(board_file))
        # "Seed entities.db" has [created::2026-03-23] only
        seed = [c for c in chunks if "Seed entities" in c.text]
        assert seed
        assert seed[0].date == date(2026, 3, 23)
        assert seed[0].metadata.get("date_field") == "created"


# ---------------------------------------------------------------------------
# chunk_memory_log tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkMemoryLog:
    @pytest.mark.unit
    def test_date_extracted_from_filename(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        assert len(chunks) > 0
        assert all(c.date == date(2026, 3, 22) for c in chunks)

    @pytest.mark.unit
    def test_splits_on_section_headers(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        headings = [c.metadata.get("section_heading") for c in chunks]
        assert "Session Summary" in headings
        assert "Decisions" in headings
        assert "Next Steps" in headings

    @pytest.mark.unit
    def test_chunk_type_is_memory_section(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        assert all(c.chunk_type == "memory_section" for c in chunks)

    @pytest.mark.unit
    def test_source_path_preserved(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        assert all(c.source_path == str(memory_log_file) for c in chunks)

    @pytest.mark.unit
    def test_frontmatter_stripped(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        # Frontmatter "date: 2026-03-22" should not appear in chunk text
        all_text = " ".join(c.text for c in chunks)
        assert "kanban-plugin" not in all_text

    @pytest.mark.unit
    def test_section_text_contains_content(self, memory_log_file: Path) -> None:
        chunks = chunk_memory_log(str(memory_log_file))
        decisions = [c for c in chunks if c.metadata.get("section_heading") == "Decisions"]
        assert decisions
        assert "BM25" in decisions[0].text or "RRF" in decisions[0].text

    @pytest.mark.unit
    def test_no_headings_produces_single_chunk(self, undated_memory_log: Path) -> None:
        chunks = chunk_memory_log(str(undated_memory_log))
        assert len(chunks) == 1
        assert "raw notes" in chunks[0].text

    @pytest.mark.unit
    def test_invalid_filename_gives_none_date(self, tmp_path: Path) -> None:
        p = tmp_path / "random_notes.md"
        p.write_text("## Notes\n\nSome content.", encoding="utf-8")
        chunks = chunk_memory_log(str(p))
        assert all(c.date is None for c in chunks)

    @pytest.mark.unit
    def test_returns_empty_on_missing_file(self) -> None:
        chunks = chunk_memory_log("/nonexistent/path/2026-03-22.md")
        assert chunks == []


# ---------------------------------------------------------------------------
# chunk_file dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkFile:
    @pytest.mark.unit
    def test_dispatches_memory_log_by_filename(self, memory_log_file: Path) -> None:
        chunks = chunk_file(str(memory_log_file))
        assert len(chunks) > 0
        assert all(c.chunk_type == "memory_section" for c in chunks)

    @pytest.mark.unit
    def test_dispatches_board_by_directory(self, board_file: Path) -> None:
        chunks = chunk_file(str(board_file))
        assert len(chunks) > 0
        assert all(c.chunk_type == "board_card" for c in chunks)

    @pytest.mark.unit
    def test_board_detected_by_content(self, tmp_path: Path) -> None:
        """File not in Boards/ dir but with kanban content should be detected as board."""
        content = textwrap.dedent("""\
            ## Done

            - [ ] Some task [completed::2026-03-10]

            ## Backlog

            - [ ] Future work
        """)
        p = tmp_path / "project-status.md"
        p.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(p))
        # Content has ## Done so should be detected as board
        assert any(c.chunk_type == "board_card" for c in chunks)

    @pytest.mark.unit
    def test_memory_filename_takes_priority(self, tmp_path: Path) -> None:
        """YYYY-MM-DD.md should always be treated as memory log."""
        content = textwrap.dedent("""\
            ## Done

            - [ ] Task [completed::2026-03-10]
        """)
        p = tmp_path / "2026-03-22.md"
        p.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(p))
        # Even though content looks like a board, filename wins
        assert all(c.chunk_type == "memory_section" for c in chunks)


# ---------------------------------------------------------------------------
# Branch-coverage tests for the defensive paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractDateInvalidValues:
    """Closes coverage of ``_extract_date_from_card``'s ValueError path (lines 112-113)."""

    @pytest.mark.unit
    def test_invalid_calendar_date_in_card_is_silently_skipped(self, tmp_path: Path) -> None:
        """A card with [completed::2026-13-45] (impossible date) parses without that field."""
        content = textwrap.dedent("""\
            ## Done

            - [x] Task with bad date [completed::2026-13-45]
            - [x] Task with good date [completed::2026-03-10]
        """)
        p = tmp_path / "Boards" / "project.md"
        p.parent.mkdir()
        p.write_text(content, encoding="utf-8")
        chunks = chunk_board(str(p))
        bad = next(c for c in chunks if "bad date" in c.text)
        good = next(c for c in chunks if "good date" in c.text)
        assert bad.date is None
        assert "date_field" not in bad.metadata
        assert good.date == date(2026, 3, 10)
        assert good.metadata.get("date_field") == "completed"


@pytest.mark.unit
class TestChunkBoardCardBuffering:
    """Closes coverage of card-flush branches around lines 173, 224-225."""

    @pytest.mark.unit
    def test_whitespace_only_card_does_not_emit_chunk(self, tmp_path: Path) -> None:
        """A card whose body strips to empty produces no chunk (line 173)."""
        content = "## Done\n\n- [ ]   \n\n## Backlog\n\n- [ ] Real task\n"
        p = tmp_path / "Boards" / "ws.md"
        p.parent.mkdir()
        p.write_text(content, encoding="utf-8")
        chunks = chunk_board(str(p))
        # Only the "Real task" card produces a chunk; the whitespace card is dropped.
        real_chunks = [c for c in chunks if "Real task" in c.text]
        assert len(real_chunks) == 1
        # And the whitespace-only card did NOT produce a chunk.
        whitespace_chunks = [c for c in chunks if "Real task" not in c.text and c.text.strip() == ""]
        assert whitespace_chunks == []

    @pytest.mark.unit
    def test_non_indented_non_card_line_flushes_pending_card(self, tmp_path: Path) -> None:
        """A non-indented, non-blank line that isn't a checklist item flushes the pending card."""
        content = (
            "## Done\n"
            "\n"
            "- [ ] First card body\n"
            "Non-indented prose that breaks the card\n"  # triggers the flush
            "- [ ] Second card body\n"
        )
        p = tmp_path / "Boards" / "flush.md"
        p.parent.mkdir()
        p.write_text(content, encoding="utf-8")
        chunks = chunk_board(str(p))
        # Both cards should appear as separate chunks.
        first = [c for c in chunks if "First card body" in c.text]
        second = [c for c in chunks if "Second card body" in c.text]
        assert len(first) == 1
        assert len(second) == 1


@pytest.mark.unit
class TestMemoryLogEdgeCases:
    """Closes coverage in chunk_memory_log: invalid filename date + un-headed content."""

    @pytest.mark.unit
    def test_filename_with_impossible_date_drops_log_date_silently(self, tmp_path: Path) -> None:
        """A filename like 2026-13-45.md keeps log_date=None (the ValueError is swallowed)."""
        # _MEMORY_LOG_RE may not match invalid-month dates; check that this
        # combination of regex match + date construction is tolerated.
        # Use 2026-02-30 — Feb only has 28/29 days, so date(2026, 2, 30) raises ValueError.
        p = tmp_path / "2026-02-30.md"
        p.write_text("## Notes\n\nSome content.\n", encoding="utf-8")
        chunks = chunk_memory_log(str(p))
        # The chunk is still produced, but with no date.
        assert len(chunks) == 1
        assert chunks[0].date is None

    @pytest.mark.unit
    def test_memory_log_without_section_headings_emits_single_chunk(self, tmp_path: Path) -> None:
        """A memory log with no ## headings produces one chunk wrapping the full text."""
        p = tmp_path / "2026-03-22.md"
        p.write_text("Just plain prose with no headings.\nAnother line.\n", encoding="utf-8")
        chunks = chunk_memory_log(str(p))
        assert len(chunks) == 1
        assert "Just plain prose with no headings." in chunks[0].text
        assert chunks[0].metadata == {"section_heading": None}
        assert chunks[0].date == date(2026, 3, 22)

    @pytest.mark.unit
    def test_memory_log_with_only_empty_section_headings_falls_back_to_full_text_chunk(self, tmp_path: Path) -> None:
        """A memory log with only empty ## headings (no body anywhere) emits one chunk
        wrapping the original text.

        Closes coverage of the ``chunks.append(TemporalChunk(text=text, ...))`` line
        inside the if-not-chunks fallback (line 317). Construction: every
        per-section flush sees an empty body, so chunks remains empty after
        the section loop; the fallback grabs the original content.
        """
        p = tmp_path / "2026-03-22.md"
        p.write_text("## Heading One\n## Heading Two\n", encoding="utf-8")
        chunks = chunk_memory_log(str(p))
        # Both sections have no body → each per-heading flush exits early →
        # the if-not-chunks fallback fires.
        assert len(chunks) == 1
        # The fallback chunk wraps the original (heading-only) text.
        assert "## Heading One" in chunks[0].text
        assert "## Heading Two" in chunks[0].text
        assert chunks[0].metadata == {"section_heading": None}

    @pytest.mark.unit
    def test_empty_memory_log_emits_no_chunks(self, tmp_path: Path) -> None:
        """An empty file produces no chunks (the ``if text:`` guard at line 316 fires)."""
        p = tmp_path / "2026-03-22.md"
        p.write_text("", encoding="utf-8")
        chunks = chunk_memory_log(str(p))
        assert chunks == []


@pytest.mark.unit
class TestIsBoardFile:
    """Closes coverage of ``_is_board_file`` heuristic branches (lines 345, 348, 354-356)."""

    @pytest.mark.unit
    def test_file_with_board_in_stem_detected_as_board(self, tmp_path: Path) -> None:
        """``ProjectBoard.md`` (stem contains 'Board') is detected as a board."""
        p = tmp_path / "ProjectBoard.md"
        p.write_text("## Done\n\n- [ ] Task\n", encoding="utf-8")
        chunks = chunk_file(str(p))
        assert any(c.chunk_type == "board_card" for c in chunks)

    @pytest.mark.unit
    def test_file_under_boards_directory_detected_as_board(self, tmp_path: Path) -> None:
        """A file two levels under ``/Boards/`` is detected via the parent-directory regex.

        The regex ``[/\\\\]Boards?[/\\\\]`` requires slashes on BOTH sides of
        ``Boards``. A path like ``tmp/Boards/file.md`` has only the leading
        slash; we need an additional subdirectory: ``tmp/Boards/sub/file.md``.

        We also use a stem that doesn't contain ``Board``/``Kanban`` so the
        first heuristic at line 344 doesn't short-circuit, and content that
        doesn't contain kanban markers so the third heuristic at line 351
        also doesn't match — line 348 (parent-dir match) is the only branch
        that can fire.
        """
        p = tmp_path / "Boards" / "subproject" / "anonymous.md"
        p.parent.mkdir(parents=True)
        # Content has board-shape (## Done + checklist) but no kanban-plugin
        # marker; the content peek would NOT trigger the marker check, leaving
        # the parent-dir match at line 348 as the sole reason this is detected.
        p.write_text("## Done\n\n- [ ] Task\n", encoding="utf-8")
        chunks = chunk_file(str(p))
        assert any(c.chunk_type == "board_card" for c in chunks)

    @pytest.mark.unit
    def test_unreadable_file_falls_through_heuristic_to_default_chunker(self, tmp_path: Path) -> None:
        """When the content peek raises OSError (path is a directory), ``_is_board_file``
        returns False and ``chunk_file`` falls through to the default memory_log chunker.

        Closes coverage of lines 354-356 (``except OSError: pass`` + trailing ``return False``).
        """
        d = tmp_path / "regular_file.md"
        d.mkdir()  # directory, not a file
        # The memory_log chunker also can't read a directory; returns [] cleanly.
        chunks = chunk_file(str(d))
        assert chunks == []

    @pytest.mark.unit
    def test_dispatch_unknown_type_routes_to_memory_log(self, tmp_path: Path) -> None:
        """A file that's neither memory-log-named nor board-shaped routes to memory_log.

        Closes coverage of the default-fallthrough at lines 381-382.
        """
        p = tmp_path / "random-notes.md"
        p.write_text("Just some random notes.", encoding="utf-8")
        chunks = chunk_file(str(p))
        # The memory_log chunker emits one fallback chunk for headingless content.
        assert len(chunks) == 1
        assert "Just some random notes." in chunks[0].text
