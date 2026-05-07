"""Tests for kairix.quality.eval.auto_gold — auto-generate evaluation queries from corpus."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _make_corpus_db() -> sqlite3.Connection:
    """Create an in-memory DB with sample indexed documents."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1
        );
        INSERT INTO documents (collection, path, title, hash, active) VALUES
            ('vault-projects', 'projects/alpha.md', 'alpha', 'h1', 1),
            ('vault-projects', 'projects/beta.md', 'beta', 'h2', 1),
            ('vault-knowledge', 'knowledge/microservices.md', 'microservices', 'h3', 1),
            ('vault-knowledge', 'knowledge/event-driven.md', 'event-driven', 'h4', 1),
            ('vault-areas', 'areas/how-to-deploy.md', 'how-to-deploy', 'h5', 1),
            ('vault-areas', 'areas/runbook-incidents.md', 'runbook-incidents', 'h6', 1),
            ('reference-library', 'reflib/architecture-patterns.md', 'architecture-patterns', 'h7', 1),
            ('reference-library', 'reflib/testing-strategies.md', 'testing-strategies', 'h8', 1);
    """)
    return db


class TestAnalyseCorpus:
    def test_returns_corpus_profile(self) -> None:
        from kairix.quality.eval.auto_gold import CorpusProfile, analyse_corpus

        db = _make_corpus_db()
        profile = analyse_corpus(db)
        assert isinstance(profile, CorpusProfile)
        assert profile.total_docs == 8
        assert len(profile.collections) > 0

    def test_detects_procedural_documents(self) -> None:
        from kairix.quality.eval.auto_gold import analyse_corpus

        db = _make_corpus_db()
        profile = analyse_corpus(db)
        assert profile.procedural_count > 0  # how-to-deploy, runbook-incidents

    def test_detects_collections(self) -> None:
        from kairix.quality.eval.auto_gold import analyse_corpus

        db = _make_corpus_db()
        profile = analyse_corpus(db)
        assert "vault-projects" in profile.collections
        assert "reference-library" in profile.collections


class TestGenerateQueries:
    def test_generates_correct_count(self) -> None:
        from kairix.quality.eval.auto_gold import (
            CorpusProfile,
            generate_template_queries,
        )

        profile = CorpusProfile(
            total_docs=100,
            collections={"default": 100},
            procedural_count=10,
            date_filename_count=5,
            entity_doc_count=8,
            titles=["alpha", "beta", "microservices", "event-driven"],
        )
        queries = generate_template_queries(profile, n=20)
        assert len(queries) == 20

    def test_covers_multiple_categories(self) -> None:
        from kairix.quality.eval.auto_gold import (
            CorpusProfile,
            generate_template_queries,
        )

        profile = CorpusProfile(
            total_docs=100,
            collections={"default": 100},
            procedural_count=10,
            date_filename_count=5,
            entity_doc_count=8,
            titles=[
                "alpha",
                "beta",
                "microservices",
                "event-driven",
                "kubernetes",
                "testing",
                "architecture",
                "deployment",
            ],
        )
        queries = generate_template_queries(profile, n=30)
        categories = {q["category"] for q in queries}
        assert len(categories) >= 3  # at least 3 different categories

    def test_queries_reference_real_titles(self) -> None:
        from kairix.quality.eval.auto_gold import (
            CorpusProfile,
            generate_template_queries,
        )

        titles = ["microservices", "kubernetes", "testing"]
        profile = CorpusProfile(
            total_docs=50,
            collections={"default": 50},
            procedural_count=5,
            date_filename_count=0,
            entity_doc_count=0,
            titles=titles,
        )
        queries = generate_template_queries(profile, n=10)
        # At least some queries should reference document titles
        query_texts = " ".join(q["query"] for q in queries).lower()
        title_hits = sum(1 for t in titles if t in query_texts)
        assert title_hits >= 1


class TestBuildSuite:
    def test_produces_valid_yaml_structure(self, tmp_path: Path) -> None:

        import yaml

        from kairix.quality.eval.auto_gold import (
            CorpusProfile,
            build_suite,
            generate_template_queries,
        )

        profile = CorpusProfile(
            total_docs=50,
            collections={"default": 50},
            procedural_count=5,
            date_filename_count=0,
            entity_doc_count=0,
            titles=["alpha", "beta", "gamma"],
        )
        queries = generate_template_queries(profile, n=10)
        output = tmp_path / "test-suite.yaml"
        build_suite(queries, str(output))

        with open(output, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "meta" in data
        assert "cases" in data
        assert len(data["cases"]) == 10
        for case in data["cases"]:
            assert "id" in case
            assert "query" in case
            assert "category" in case
            assert "score_method" in case

    def test_writes_utf8_for_non_ascii_titles(self, tmp_path: Path) -> None:
        """Regression for #143 Phase 0: build_suite previously opened the
        output file without encoding= so non-ASCII titles were mangled or
        raised UnicodeEncodeError on non-UTF-8 hosts."""
        import yaml

        from kairix.quality.eval.auto_gold import build_suite

        queries = [
            {"id": "T1", "category": "recall", "query": "café résumé naïve", "score_method": "ndcg"},
            {"id": "T2", "category": "recall", "query": "数据 explorers 🌐", "score_method": "ndcg"},
        ]
        output = tmp_path / "non-ascii-suite.yaml"
        build_suite(queries, str(output))

        with open(output, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["cases"][0]["query"] == "café résumé naïve"
        assert data["cases"][1]["query"] == "数据 explorers 🌐"


class TestProceduralTitleClassification:
    """Regression for #143 Phase 0: _PROCEDURAL_PATTERNS was applied to titles
    in generate_template_queries, but the regex anchors on path separators
    (`(?:^|/)how-to-|runbook|...`). Titles never contain '/', so the filter
    was permanently empty and the procedural-query fallback (titles[:n_procedural])
    always ran — mislabelling recall queries as procedural."""

    def test_analyse_corpus_classifies_procedural_titles_by_path(self) -> None:
        from kairix.quality.eval.auto_gold import analyse_corpus

        db = _make_corpus_db()
        profile = analyse_corpus(db)
        # The fixture has paths matching how-to and runbook-* under /areas/.
        assert "how-to-deploy" in profile.procedural_titles
        assert "runbook-incidents" in profile.procedural_titles
        # Non-procedural titles are absent.
        assert "alpha" not in profile.procedural_titles
        assert "microservices" not in profile.procedural_titles

    def test_generate_template_queries_uses_procedural_titles(self) -> None:
        from kairix.quality.eval.auto_gold import (
            CorpusProfile,
            generate_template_queries,
        )

        profile = CorpusProfile(
            total_docs=20,
            collections={"areas": 20},
            procedural_count=3,
            date_filename_count=0,
            entity_doc_count=0,
            titles=["alpha", "beta", "gamma", "how-to-deploy", "runbook-incidents"],
            procedural_titles=["how-to-deploy", "runbook-incidents"],
        )
        queries = generate_template_queries(profile, n=20)
        proc_queries = [q for q in queries if q["category"] == "procedural"]
        # Procedural queries are constructed from real procedural titles, not
        # from arbitrary recall titles.
        assert len(proc_queries) > 0
        for q in proc_queries:
            assert any(title in q["query"] for title in ["how to deploy", "runbook incidents"])
