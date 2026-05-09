"""Unit tests for scoring metrics used by sweep — delegates to kairix.quality.eval.metrics."""

from __future__ import annotations

import math

import pytest

from kairix.quality.eval.metrics import hit_at_k_graded as _compute_hit_at_k
from kairix.quality.eval.metrics import ndcg_graded as _compute_ndcg
from kairix.quality.eval.metrics import reciprocal_rank_graded as _compute_mrr

# ---------------------------------------------------------------------------
# ndcg_graded (was _compute_ndcg)
# ---------------------------------------------------------------------------


class TestComputeNDCG:
    @pytest.mark.unit
    def test_perfect_ranking(self):
        """Single relevant doc at rank 1 → NDCG = 1.0."""
        gold = [{"title": "target", "relevance": 2}]
        retrieved = ["/path/target.md"]
        assert _compute_ndcg(retrieved, gold, k=10) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_empty_gold(self):
        assert _compute_ndcg(["/a.md"], [], k=10) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_no_relevant_retrieved(self):
        gold = [{"title": "target", "relevance": 2}]
        retrieved = ["/path/wrong.md", "/path/also-wrong.md"]
        assert _compute_ndcg(retrieved, gold, k=10) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_known_ndcg_value(self):
        """Two gold docs, retrieved at rank 1 and 3 — manual DCG/IDCG."""
        gold = [
            {"title": "a", "relevance": 2},
            {"title": "b", "relevance": 1},
        ]
        retrieved = ["/path/a.md", "/path/irrelevant.md", "/path/b.md"]

        expected_dcg = 2.0 / math.log2(2) + 0.0 + 1.0 / math.log2(4)
        expected_idcg = 2.0 / math.log2(2) + 1.0 / math.log2(3)
        expected = expected_dcg / expected_idcg

        assert _compute_ndcg(retrieved, gold, k=10) == pytest.approx(expected, rel=1e-6)

    @pytest.mark.unit
    def test_k_truncation(self):
        """Only top-k results count."""
        gold = [{"title": "late", "relevance": 2}]
        retrieved = ["/a.md", "/b.md", "/late.md"]
        assert _compute_ndcg(retrieved, gold, k=2) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_empty_retrieved(self):
        gold = [{"title": "a", "relevance": 1}]
        assert _compute_ndcg([], gold, k=10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# hit_at_k_graded (was _compute_hit_at_k)
# ---------------------------------------------------------------------------


class TestComputeHitAtK:
    @pytest.mark.unit
    def test_hit_in_top1(self):
        gold = [{"title": "target", "relevance": 1}]
        assert _compute_hit_at_k(["/target.md"], gold, k=5) is True

    @pytest.mark.unit
    def test_hit_at_boundary(self):
        gold = [{"title": "target", "relevance": 1}]
        retrieved = ["/a.md", "/b.md", "/c.md", "/d.md", "/target.md"]
        assert _compute_hit_at_k(retrieved, gold, k=5) is True

    @pytest.mark.unit
    def test_miss_beyond_k(self):
        gold = [{"title": "target", "relevance": 1}]
        retrieved = ["/a.md", "/b.md", "/c.md", "/d.md", "/e.md", "/target.md"]
        assert _compute_hit_at_k(retrieved, gold, k=5) is False

    @pytest.mark.unit
    def test_no_gold(self):
        assert _compute_hit_at_k(["/a.md"], [], k=5) is False

    @pytest.mark.unit
    def test_k1(self):
        gold = [{"title": "target", "relevance": 2}]
        assert _compute_hit_at_k(["/target.md"], gold, k=1) is True
        assert _compute_hit_at_k(["/other.md", "/target.md"], gold, k=1) is False


# ---------------------------------------------------------------------------
# reciprocal_rank_graded (was _compute_mrr)
# ---------------------------------------------------------------------------


class TestComputeMRR:
    @pytest.mark.unit
    def test_first_position(self):
        gold = [{"title": "target", "relevance": 1}]
        assert _compute_mrr(["/target.md"], gold, k=10) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_second_position(self):
        gold = [{"title": "target", "relevance": 1}]
        assert _compute_mrr(["/other.md", "/target.md"], gold, k=10) == pytest.approx(0.5)

    @pytest.mark.unit
    def test_third_position(self):
        gold = [{"title": "target", "relevance": 1}]
        retrieved = ["/a.md", "/b.md", "/target.md"]
        assert _compute_mrr(retrieved, gold, k=10) == pytest.approx(1.0 / 3)

    @pytest.mark.unit
    def test_no_relevant(self):
        gold = [{"title": "target", "relevance": 1}]
        assert _compute_mrr(["/a.md", "/b.md"], gold, k=10) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_beyond_k(self):
        gold = [{"title": "target", "relevance": 1}]
        retrieved = ["/a.md", "/b.md", "/target.md"]
        assert _compute_mrr(retrieved, gold, k=2) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_empty_gold(self):
        assert _compute_mrr(["/a.md"], [], k=10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Category alias resolution (via constants)
# ---------------------------------------------------------------------------


class TestCategoryAliases:
    @pytest.mark.unit
    def test_semantic_alias(self):
        from kairix.quality.eval.constants import CATEGORY_ALIASES

        assert CATEGORY_ALIASES["semantic"] == "recall"

    @pytest.mark.unit
    def test_keyword_alias(self):
        from kairix.quality.eval.constants import CATEGORY_ALIASES

        assert CATEGORY_ALIASES["keyword"] == "conceptual"

    @pytest.mark.unit
    def test_weights_sum(self):
        from kairix.quality.eval.constants import CATEGORY_WEIGHTS

        total = sum(CATEGORY_WEIGHTS.values())
        assert total == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# sweep_bm25_params — public interface tests
# ---------------------------------------------------------------------------


class TestSweepBm25Params:
    """Test sweep_bm25_params through its public interface with mocked DB."""

    @pytest.mark.unit
    def test_empty_suite_returns_empty_report(self, tmp_path):
        """Sweep returns empty report when suite has no cases."""
        import yaml

        from kairix.quality.eval.sweep import sweep_bm25_params

        suite = {"meta": {"version": "1.0"}, "cases": []}
        suite_path = tmp_path / "empty.yaml"
        with open(suite_path, "w") as f:
            yaml.dump(suite, f)

        report = sweep_bm25_params(suite_path)
        assert report.results == []
        assert report.best is None

    @pytest.mark.unit
    def test_no_ndcg_cases_returns_empty_report(self, tmp_path):
        """Sweep returns empty report when no ndcg-scored cases exist."""
        import yaml

        from kairix.quality.eval.sweep import sweep_bm25_params

        suite = {
            "meta": {"version": "1.0"},
            "cases": [
                {"query": "test", "category": "recall", "score_method": "hit"},
            ],
        }
        suite_path = tmp_path / "no-ndcg.yaml"
        with open(suite_path, "w") as f:
            yaml.dump(suite, f)

        report = sweep_bm25_params(suite_path)
        assert report.results == []
        assert report.best is None

    @pytest.mark.unit
    def test_sweep_runs_all_style_and_weight_combos(self, tmp_path):
        """Sweep evaluates every (weight, style) combination."""
        import sqlite3

        import yaml

        from kairix.quality.eval.sweep import sweep_bm25_params

        # Create a minimal FTS5 database
        db_path = tmp_path / "index.sqlite"
        db = sqlite3.connect(str(db_path))
        db.executescript("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                path TEXT NOT NULL,
                title TEXT,
                hash TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                UNIQUE(collection, path)
            );
            CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                path, title, doc, content='', tokenize='porter unicode61'
            );
            INSERT INTO documents (collection, path, title, hash, active)
            VALUES ('test', 'docs/architecture.md', 'Architecture Guide', 'h1', 1);
            INSERT INTO content (hash, doc) VALUES ('h1', 'Architecture patterns and decisions.');
            INSERT INTO documents_fts (rowid, path, title, doc)
            VALUES (1, 'docs/architecture.md', 'Architecture Guide', 'Architecture patterns and decisions.');
        """)
        db.close()

        suite = {
            "meta": {"version": "1.0"},
            "cases": [
                {
                    "query": "architecture patterns",
                    "category": "recall",
                    "score_method": "ndcg",
                    "gold_titles": [{"title": "Architecture Guide", "relevance": 2}],
                },
            ],
        }
        suite_path = tmp_path / "suite.yaml"
        with open(suite_path, "w") as f:
            yaml.dump(suite, f)

        weights = [(1.0, 1.0, 1.0), (10.0, 1.0, 1.0)]
        styles = ["bare", "prefix"]

        report = sweep_bm25_params(
            suite_path,
            weight_configs=weights,
            query_styles=styles,
            db_path=db_path,
        )

        assert report.total_configs == 4  # 2 weights x 2 styles
        assert len(report.results) == 4
        assert report.best is not None
        # Results sorted descending
        scores = [r.weighted_total for r in report.results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.unit
    def test_sweep_writes_csv_output(self, tmp_path):
        """Sweep writes CSV when output_path is provided."""
        import sqlite3

        import yaml

        from kairix.quality.eval.sweep import sweep_bm25_params

        db_path = tmp_path / "index.sqlite"
        db = sqlite3.connect(str(db_path))
        db.executescript("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                path TEXT NOT NULL,
                title TEXT,
                hash TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                UNIQUE(collection, path)
            );
            CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                path, title, doc, content='', tokenize='porter unicode61'
            );
            INSERT INTO documents (collection, path, title, hash, active)
            VALUES ('test', 'docs/test.md', 'Test Doc', 'h1', 1);
            INSERT INTO content (hash, doc) VALUES ('h1', 'Testing document content.');
            INSERT INTO documents_fts (rowid, path, title, doc)
            VALUES (1, 'docs/test.md', 'Test Doc', 'Testing document content.');
        """)
        db.close()

        suite = {
            "meta": {"version": "1.0"},
            "cases": [
                {
                    "query": "testing document",
                    "category": "recall",
                    "score_method": "ndcg",
                    "gold_titles": [{"title": "Test Doc", "relevance": 2}],
                },
            ],
        }
        suite_path = tmp_path / "suite.yaml"
        with open(suite_path, "w") as f:
            yaml.dump(suite, f)

        csv_path = tmp_path / "results.csv"

        sweep_bm25_params(
            suite_path,
            output_path=csv_path,
            weight_configs=[(1.0, 1.0, 1.0)],
            query_styles=["prefix"],
            db_path=db_path,
        )

        assert csv_path.exists()
        lines = csv_path.read_text().splitlines()
        assert len(lines) == 2  # header + 1 data row
        assert "fp_weight" in lines[0]

    @pytest.mark.unit
    def test_sweep_report_best_is_highest_weighted(self, tmp_path):
        """The best result has the highest weighted_total."""
        import sqlite3

        import yaml

        from kairix.quality.eval.sweep import sweep_bm25_params

        db_path = tmp_path / "index.sqlite"
        db = sqlite3.connect(str(db_path))
        db.executescript("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                path TEXT NOT NULL,
                title TEXT,
                hash TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                UNIQUE(collection, path)
            );
            CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                path, title, doc, content='', tokenize='porter unicode61'
            );
            INSERT INTO documents (collection, path, title, hash, active)
            VALUES ('test', 'docs/arch.md', 'Architecture', 'h1', 1);
            INSERT INTO content (hash, doc) VALUES ('h1', 'Software architecture guide.');
            INSERT INTO documents_fts (rowid, path, title, doc)
            VALUES (1, 'docs/arch.md', 'Architecture', 'Software architecture guide.');
        """)
        db.close()

        suite = {
            "meta": {"version": "1.0"},
            "cases": [
                {
                    "query": "architecture guide",
                    "category": "recall",
                    "score_method": "ndcg",
                    "gold_titles": [{"title": "Architecture", "relevance": 2}],
                },
            ],
        }
        suite_path = tmp_path / "suite.yaml"
        with open(suite_path, "w") as f:
            yaml.dump(suite, f)

        report = sweep_bm25_params(
            suite_path,
            weight_configs=[(1.0, 1.0, 1.0), (10.0, 5.0, 1.0)],
            query_styles=["prefix", "bare", "quoted"],
            db_path=db_path,
        )

        assert report.best is not None
        assert report.best.weighted_total == max(r.weighted_total for r in report.results)
