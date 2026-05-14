"""Tests for `kairix entity count` CLI subcommand (#259).

The subcommand reports total entity count plus a by-type rollup,
driven by a single `MATCH (n) RETURN labels(n) AS labels, count(n) AS count`
Cypher query that the CLI aggregates in Python. Tests inject a
``FakeNeo4jClient`` — no real Neo4j connection, no patching.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from kairix.knowledge.entities.cli import (
    build_parser,
    format_count_text,
    main,
    rollup_entity_counts,
)
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.unit


def _drive(args: list[str], **kw: Any) -> tuple[str, str, int]:
    """Drive the `kairix entity` CLI and capture (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args, **kw)
    except SystemExit as exc:  # pragma: no cover - argparse exit path
        code = int(exc.code) if exc.code is not None else 0
    return out.getvalue(), err.getvalue(), code


# -- parser wiring ------------------------------------------------------------


@pytest.mark.unit
class TestCountParser:
    def test_count_subcommand_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["count"])
        assert args.command == "count"
        assert args.type_filter is None
        assert args.json is False

    @pytest.mark.unit
    def test_count_accepts_type_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["count", "--type", "Organisation"])
        assert args.type_filter == "Organisation"

    @pytest.mark.unit
    def test_count_accepts_json_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["count", "--json"])
        assert args.json is True


# -- pure helpers (sabotage-prove these) -------------------------------------


@pytest.mark.unit
class TestRollupEntityCounts:
    def test_groups_by_primary_label(self) -> None:
        rows = [
            {"labels": ["Organisation"], "count": 3},
            {"labels": ["Person"], "count": 2},
            {"labels": ["Organisation"], "count": 1},
        ]
        total, by_type = rollup_entity_counts(rows)
        assert total == 6
        assert by_type == {"Organisation": 4, "Person": 2}

    @pytest.mark.unit
    def test_empty_labels_buckets_unlabelled(self) -> None:
        rows = [{"labels": [], "count": 2}, {"labels": ["Person"], "count": 1}]
        total, by_type = rollup_entity_counts(rows)
        assert total == 3
        assert by_type == {"Person": 1, "Unlabelled": 2}

    @pytest.mark.unit
    def test_sorted_keys(self) -> None:
        rows = [
            {"labels": ["Zeta"], "count": 1},
            {"labels": ["Alpha"], "count": 1},
            {"labels": ["Mu"], "count": 1},
        ]
        _, by_type = rollup_entity_counts(rows)
        assert list(by_type.keys()) == ["Alpha", "Mu", "Zeta"]

    @pytest.mark.unit
    def test_empty_rows(self) -> None:
        total, by_type = rollup_entity_counts([])
        assert total == 0
        assert by_type == {}


@pytest.mark.unit
class TestFormatCountText:
    def test_two_line_header_then_rollup(self) -> None:
        text = format_count_text(5, {"Organisation": 3, "Person": 2})
        assert text == ("total_entities: 5\nby_type:\n  Organisation: 3\n  Person: 2")

    @pytest.mark.unit
    def test_zero_total_with_empty_rollup(self) -> None:
        text = format_count_text(0, {})
        assert text == "total_entities: 0\nby_type:"


# -- CLI end-to-end with injected fake ---------------------------------------


@pytest.mark.unit
class TestCountCLI:
    def test_default_text_output_shows_total_and_rollup(self) -> None:
        # Default FakeNeo4jClient has 3 Organisation, 1 Person, 1 Project.
        stdout, _stderr, code = _drive(["count"], neo4j_client=FakeNeo4jClient())
        assert code == 0
        # Order: alphabetical by label.
        assert "total_entities: 5" in stdout
        assert "by_type:" in stdout
        assert "  Organisation: 3" in stdout
        assert "  Person: 1" in stdout
        assert "  Project: 1" in stdout

    @pytest.mark.unit
    def test_type_filter_prints_just_the_number(self) -> None:
        stdout, _stderr, code = _drive(
            ["count", "--type", "Organisation"],
            neo4j_client=FakeNeo4jClient(),
        )
        assert code == 0
        assert stdout.strip() == "3"
        assert "total_entities" not in stdout
        assert "by_type" not in stdout

    @pytest.mark.unit
    def test_type_filter_unknown_label_returns_zero(self) -> None:
        stdout, _stderr, code = _drive(
            ["count", "--type", "Nonexistent"],
            neo4j_client=FakeNeo4jClient(),
        )
        assert code == 0
        assert stdout.strip() == "0"

    @pytest.mark.unit
    def test_json_envelope_round_trips(self) -> None:
        stdout, _stderr, code = _drive(["count", "--json"], neo4j_client=FakeNeo4jClient())
        assert code == 0
        parsed = json.loads(stdout)
        assert parsed == {
            "total": 5,
            "by_type": {"Organisation": 3, "Person": 1, "Project": 1},
        }

    @pytest.mark.unit
    def test_json_indented_two_spaces(self) -> None:
        stdout, _stderr, code = _drive(["count", "--json"], neo4j_client=FakeNeo4jClient())
        assert code == 0
        # json.dumps(indent=2) prefixes nested keys with two spaces.
        assert '  "total":' in stdout
        assert '  "by_type":' in stdout

    @pytest.mark.unit
    def test_empty_graph_reports_zero(self) -> None:
        stdout, _stderr, code = _drive(["count"], neo4j_client=FakeNeo4jClient(entities=[]))
        assert code == 0
        assert "total_entities: 0" in stdout
        assert "by_type:" in stdout

    @pytest.mark.unit
    def test_neo4j_unavailable_exits_1(self) -> None:
        fake = FakeNeo4jClient(entities=[])
        fake.available = False
        _stdout, stderr, code = _drive(["count"], neo4j_client=fake)
        assert code == 1
        assert "Neo4j not available" in stderr
