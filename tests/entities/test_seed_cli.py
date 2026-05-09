"""Tests for `kairix entity seed` CLI subcommand."""

from __future__ import annotations

import sqlite3

import pytest

from kairix.knowledge.entities.cli import build_parser, main

pytestmark = pytest.mark.unit


class TestSeedCLIParsing:
    @pytest.mark.unit
    def test_seed_subcommand_exists(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["seed"])
        assert args.command == "seed"

    @pytest.mark.unit
    def test_seed_dry_run_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["seed", "--dry-run"])
        assert args.dry_run is True

    @pytest.mark.unit
    def test_seed_limit_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["seed", "--limit", "100"])
        assert args.limit == 100


class TestSeedCLIExecution:
    @pytest.mark.unit
    def test_exits_1_when_no_index(self, tmp_path) -> None:
        assert main(["seed"], db_path=tmp_path / "absent.sqlite") == 1

    @pytest.mark.unit
    def test_exits_1_when_index_unpopulated(self, tmp_path) -> None:
        # File exists but has no documents table — operator gets the same hint.
        db_path = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db_path)).close()
        assert main(["seed"], db_path=db_path) == 1

    @pytest.mark.integration
    def test_dry_run_does_not_seed(self, tmp_path) -> None:
        db_path = tmp_path / "index.sqlite"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE documents (id INTEGER, path TEXT, title TEXT, active INTEGER)")
        db.close()
        # No rows → 0 candidates → return 0 with "no candidates" message
        assert main(["seed", "--dry-run"], db_path=db_path) == 0

    @pytest.mark.integration
    def test_returns_0_when_no_candidates(self, tmp_path) -> None:
        db_path = tmp_path / "index.sqlite"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE documents (id INTEGER, path TEXT, title TEXT, active INTEGER)")
        db.close()
        assert main(["seed"], db_path=db_path) == 0
