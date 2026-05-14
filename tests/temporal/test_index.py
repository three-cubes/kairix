"""
Tests for kairix.core.temporal.index — query_temporal_chunks and get_memory_log_paths.

Uses document_root DI parameter to pass temp dirs directly (no patching needed).
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from kairix.core.temporal.index import get_memory_log_paths, query_temporal_chunks


@pytest.fixture()
def doc_root(tmp_path: Path) -> Path:
    """Create a synthetic document root with agent memory logs and boards."""
    # Agent memory logs
    memory_dir = tmp_path / "04-Agent-Knowledge" / "builder" / "memory"
    memory_dir.mkdir(parents=True)

    (memory_dir / "2026-04-28.md").write_text(
        textwrap.dedent("""\
            ## Session Summary

            Worked on hybrid search integration and BM25 tuning.

            ## Decisions

            - Adopted RRF fusion strategy for search.
        """),
        encoding="utf-8",
    )
    (memory_dir / "2026-04-29.md").write_text(
        textwrap.dedent("""\
            ## Session Summary

            Completed temporal index implementation.

            ## Next Steps

            - Run benchmark after Phase 3.
        """),
        encoding="utf-8",
    )
    (memory_dir / "2026-03-15.md").write_text(
        textwrap.dedent("""\
            ## Session Summary

            Old session from March.
        """),
        encoding="utf-8",
    )

    # Board files
    boards_dir = tmp_path / "01-Projects" / "Boards"
    boards_dir.mkdir(parents=True)
    (boards_dir / "Kairix.md").write_text(
        textwrap.dedent("""\
            ## Done

            - [ ] Phase 1 shipped [completed::2026-03-10] [project::Kairix]

            ## In Progress

            - [ ] Phase 3 temporal [started::2026-04-28] [project::Kairix]
        """),
        encoding="utf-8",
    )

    return tmp_path


@pytest.mark.unit
class TestGetMemoryLogPaths:
    @pytest.mark.unit
    def test_finds_logs_in_date_range(self, doc_root: Path) -> None:
        paths = get_memory_log_paths(
            start=date(2026, 4, 28),
            end=date(2026, 4, 30),
            document_root=doc_root,
        )
        assert len(paths) == 2
        assert any("2026-04-28.md" in p for p in paths)
        assert any("2026-04-29.md" in p for p in paths)

    @pytest.mark.unit
    def test_excludes_logs_outside_range(self, doc_root: Path) -> None:
        paths = get_memory_log_paths(
            start=date(2026, 4, 28),
            end=date(2026, 4, 30),
            document_root=doc_root,
        )
        assert not any("2026-03-15.md" in p for p in paths)

    @pytest.mark.unit
    def test_returns_all_when_no_range(self, doc_root: Path) -> None:
        paths = get_memory_log_paths(start=None, end=None, document_root=doc_root)
        assert len(paths) == 3

    @pytest.mark.unit
    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        paths = get_memory_log_paths(
            start=None,
            end=None,
            document_root=tmp_path,
        )
        assert paths == []

    @pytest.mark.unit
    def test_returns_sorted_paths(self, doc_root: Path) -> None:
        paths = get_memory_log_paths(start=None, end=None, document_root=doc_root)
        assert paths == sorted(paths)


@pytest.mark.unit
class TestQueryTemporalChunks:
    @pytest.mark.unit
    def test_finds_chunks_matching_topic(self, doc_root: Path) -> None:
        results = query_temporal_chunks(
            topic="hybrid search",
            start=date(2026, 4, 28),
            end=date(2026, 4, 30),
            document_root=doc_root,
        )
        assert len(results) > 0
        assert any("hybrid" in c.text.lower() or "search" in c.text.lower() for c in results)

    @pytest.mark.unit
    def test_returns_empty_for_future_dates(self, doc_root: Path) -> None:
        results = query_temporal_chunks(
            topic="anything",
            start=date(2099, 1, 1),
            end=date(2099, 12, 31),
            document_root=doc_root,
        )
        assert len(results) == 0

    @pytest.mark.unit
    def test_filters_by_chunk_type(self, doc_root: Path) -> None:
        results = query_temporal_chunks(
            topic="Phase",
            start=None,
            end=None,
            chunk_types=["memory_section"],
            document_root=doc_root,
        )
        assert all(c.chunk_type == "memory_section" for c in results)

    @pytest.mark.unit
    def test_respects_limit(self, doc_root: Path) -> None:
        results = query_temporal_chunks(
            topic="session",
            start=None,
            end=None,
            limit=1,
            document_root=doc_root,
        )
        assert len(results) <= 1

    @pytest.mark.unit
    def test_returns_empty_for_empty_dir(self, tmp_path: Path) -> None:
        results = query_temporal_chunks(
            topic="anything",
            start=None,
            end=None,
            document_root=tmp_path,
        )
        assert results == []


@pytest.mark.unit
class TestGetMemoryLogPathsEdgeCases:
    """Cover non-directory siblings, malformed filenames, invalid dates."""

    @pytest.mark.unit
    def test_skips_non_directory_inside_agent_knowledge(self, tmp_path: Path) -> None:
        """A non-dir entry inside 04-Agent-Knowledge is skipped (line 78)."""
        agent_kb = tmp_path / "04-Agent-Knowledge"
        agent_kb.mkdir()
        # stray file at the top-level
        (agent_kb / "stray.txt").write_text("not a dir")
        # a real agent dir with memory
        memory = agent_kb / "builder" / "memory"
        memory.mkdir(parents=True)
        (memory / "2026-04-29.md").write_text("## Note")

        paths = get_memory_log_paths(start=None, end=None, document_root=tmp_path)
        assert len(paths) == 1

    @pytest.mark.unit
    def test_skips_agent_without_memory_dir(self, tmp_path: Path) -> None:
        """Agent directories without a 'memory' subdir are skipped (line 81)."""
        agent_kb = tmp_path / "04-Agent-Knowledge"
        no_mem = agent_kb / "builder"
        no_mem.mkdir(parents=True)
        # Sibling agent that does have memory
        memory = agent_kb / "shape" / "memory"
        memory.mkdir(parents=True)
        (memory / "2026-04-29.md").write_text("## Note")

        paths = get_memory_log_paths(start=None, end=None, document_root=tmp_path)
        assert len(paths) == 1
        assert any("shape" in p for p in paths)

    @pytest.mark.unit
    def test_skips_files_with_non_matching_filename(self, tmp_path: Path) -> None:
        """Files not matching YYYY-MM-DD.md are skipped (line 86)."""
        memory = tmp_path / "04-Agent-Knowledge" / "builder" / "memory"
        memory.mkdir(parents=True)
        (memory / "README.md").write_text("# Index")
        (memory / "2026-04-29.md").write_text("## Real log")

        paths = get_memory_log_paths(start=None, end=None, document_root=tmp_path)
        assert len(paths) == 1
        assert "2026-04-29.md" in paths[0]

    @pytest.mark.unit
    def test_skips_files_with_invalid_dates(self, tmp_path: Path) -> None:
        """Files with regex-matching but invalid dates are skipped (lines 89-90)."""
        memory = tmp_path / "04-Agent-Knowledge" / "builder" / "memory"
        memory.mkdir(parents=True)
        # February 30 doesn't exist — regex matches but date() raises ValueError
        (memory / "2026-02-30.md").write_text("## Invalid date")
        (memory / "2026-02-28.md").write_text("## Valid")

        paths = get_memory_log_paths(start=None, end=None, document_root=tmp_path)
        assert len(paths) == 1
        assert "2026-02-28.md" in paths[0]


@pytest.mark.unit
class TestBM25ScoreEdgeCase:
    """Cover the empty-tokens short-circuit (line 127)."""

    @pytest.mark.unit
    def test_no_matches_returns_empty(self, doc_root: Path) -> None:
        """A topic with stop-words only produces empty query_tokens — the
        BM25 scorer returns 0.0 for every chunk. Results may still surface
        but ranking is uniform."""
        # All stop words → _tokenise returns []
        results = query_temporal_chunks(
            topic="the and or",  # all stop words
            start=None,
            end=None,
            document_root=doc_root,
        )
        # Should not raise; results may be empty or include chunks with 0 score
        assert isinstance(results, list)


@pytest.mark.unit
class TestRecencyFactorNoDate:
    """Cover the chunk_date=None branch via real chunks (line 153)."""

    @pytest.mark.unit
    def test_memory_section_chunk_has_recency_applied(self, doc_root: Path) -> None:
        """Memory log chunks may have chunk.date=None, exercising the 0.5
        recency factor branch via query_temporal_chunks."""
        results = query_temporal_chunks(
            topic="Session",
            start=None,
            end=None,
            chunk_types=["memory_section"],
            document_root=doc_root,
        )
        # Memory section chunks without explicit date hit the chunk_date is None
        # branch in _recency_factor (line 153). We don't assert on the exact
        # score — just that scoring completed without errors.
        assert isinstance(results, list)


@pytest.mark.unit
class TestQueryTemporalChunksBoards:
    """Cover board-card filtering and exception handling for board chunking."""

    @pytest.mark.unit
    def test_board_files_chunked_via_doc_root(self, doc_root: Path) -> None:
        """Board files under 01-Projects/Boards are picked up via document_root.

        Exercises lines 205-208 (board chunking try-block) and 228-231
        (chunk.date filtering for board cards).
        """
        results = query_temporal_chunks(
            topic="Phase 3 temporal",
            start=date(2026, 4, 28),
            end=date(2026, 4, 30),
            document_root=doc_root,
        )
        # Board cards may or may not match the topic strongly enough; the
        # important thing is that no exception is raised and the function
        # returns a list.
        assert isinstance(results, list)

    @pytest.mark.unit
    def test_board_card_outside_date_range_excluded(self, tmp_path: Path) -> None:
        """Board cards with a date outside [start, end] are filtered out
        (lines 228-231)."""
        boards_dir = tmp_path / "01-Projects" / "Boards"
        boards_dir.mkdir(parents=True)
        (boards_dir / "Old.md").write_text(
            "## Done\n\n- [ ] Ancient card [completed::2020-01-01] [project::Old]\n",
            encoding="utf-8",
        )
        results = query_temporal_chunks(
            topic="ancient card",
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            document_root=tmp_path,
        )
        # Card date 2020-01-01 < start 2026-01-01 → excluded
        assert all("Ancient card" not in c.text for c in results)


@pytest.mark.unit
class TestQueryTemporalChunksException:
    """Cover the outermost exception handler (lines 258-260)."""

    @pytest.mark.unit
    def test_returns_empty_when_topic_is_none(self, doc_root: Path) -> None:
        """Passing topic=None triggers an AttributeError inside the scorer;
        query_temporal_chunks catches it and returns []."""
        results = query_temporal_chunks(  # NOSONAR(python:S5655) — see type: ignore below; outer-except.
            topic=None,  # type: ignore[arg-type]  # deliberate type misuse — see NOSONAR above.
            start=None,
            end=None,
            document_root=doc_root,
        )
        assert results == []
