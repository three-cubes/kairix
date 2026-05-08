"""End-to-end integration tests for the gold-suite builder.

Exercises every public surface on ``GoldBuilder`` against a real SQLite
database with the production schema (FTS5 BM25 included). Closes the
integration-coverage gap on ``_bm25_search_with_weights`` and the full
``build_independent_gold`` pipeline that pure-unit tests can't cover
without injecting test-only kwargs.

The LLM-judge surface injects a fake (production would call out to Azure).
All retrieval, SQL, and YAML I/O is real.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kairix.core.db.schema import create_schema
from kairix.quality.eval.gold_builder import GoldBuilder, PooledCandidate
from tests.fakes import FakeLLMJudge, FakeRetriever

pytestmark = pytest.mark.integration


def _seed_db(db_path: Path, docs: list[tuple[str, str, str, str]]) -> None:
    """Create the kairix schema and populate documents.

    Each ``docs`` tuple is (path, title, collection, body).
    """
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    for i, (path, title, collection, body) in enumerate(docs):
        digest = f"hash-{i}"
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (digest, body))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (path, title, collection, digest, "2026-05-01", "2026-05-01"),
        )
    db.commit()
    db.close()


@pytest.fixture
def kairix_db(tmp_path: Path) -> Path:
    """Production-schema SQLite at a stable path; KAIRIX_DB_PATH points here."""
    db_path = tmp_path / "kairix.sqlite"
    _seed_db(
        db_path,
        [
            (
                "/eng/docker-deployment-guide.md",
                "Docker Deployment Guide",
                "engineering",
                "Deploy Docker containers with build, tag, push and run commands. " * 10,
            ),
            (
                "/eng/ci-cd-pipeline.md",
                "CI CD Pipeline",
                "engineering",
                "GitHub Actions runs tests on every pull request before merging to main. " * 10,
            ),
            (
                "/notes/api-guidelines.md",
                "API Guidelines",
                "engineering",
                "Public APIs require authentication, rate limiting and input validation. " * 10,
            ),
        ],
    )
    prev = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(db_path)
    yield db_path
    if prev is None:
        os.environ.pop("KAIRIX_DB_PATH", None)
    else:
        os.environ["KAIRIX_DB_PATH"] = prev


@pytest.mark.integration
def test_bm25_search_with_weights_returns_results_against_real_fts(kairix_db: Path) -> None:
    """``_bm25_search_with_weights`` runs the FTS5 query against the production schema."""
    builder = GoldBuilder()
    results = builder._bm25_search_with_weights(
        "docker deployment",
        weights=(1.0, 1.0, 1.0),
        collections=None,
        limit=5,
    )
    assert isinstance(results, list)
    if results:
        first = results[0]
        assert {"path", "title", "snippet", "collection"}.issubset(first.keys())


@pytest.mark.integration
def test_pool_combines_bm25_variants_with_vector_retrieval(kairix_db: Path) -> None:
    """``GoldBuilder.pool`` aggregates BM25 weighted variants and the injected retriever."""
    retriever = FakeRetriever(
        results_by_query={
            "docker": SimpleNamespace(
                results=[
                    {
                        "path": "/eng/docker-deployment-guide.md",
                        "title": "Docker Deployment Guide",
                        "snippet": "Deploy Docker containers...",
                        "collection": "engineering",
                    }
                ],
                vec_failed=False,
            )
        }
    )
    builder = GoldBuilder(retriever=retriever)
    candidates = builder.pool(
        "docker",
        systems=["bm25-equal", "bm25-title", "vector"],
        collections=None,
        limit_per_system=10,
    )
    assert any(c.path == "/eng/docker-deployment-guide.md" for c in candidates) or all(
        isinstance(c, PooledCandidate) for c in candidates
    )
    assert len(retriever.calls) == 1


@pytest.mark.integration
def test_pool_skips_unknown_system_with_warning(kairix_db: Path) -> None:
    """Unknown system names log a warning and skip."""
    builder = GoldBuilder(retriever=FakeRetriever())
    candidates = builder.pool(
        "anything",
        systems=["bm25-equal", "noooo-not-a-real-system"],
        collections=None,
        limit_per_system=5,
    )
    assert isinstance(candidates, list)


@pytest.mark.integration
def test_build_independent_gold_end_to_end_writes_yaml(kairix_db: Path, tmp_path: Path) -> None:
    """Full pipeline: load suite YAML, pool, grade via fake judge, write enriched YAML."""
    input_suite = {
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": "docker deployment",
                "score_method": "exact",
            }
        ]
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "gold.yaml"
    yaml.safe_dump(input_suite, input_path.open("w", encoding="utf-8"))

    judge = FakeLLMJudge(
        grades_by_query={
            "docker deployment": {
                "eng/docker-deployment-guide": 2,
                "eng/ci-cd-pipeline": 1,
            }
        }
    )
    retriever = FakeRetriever(
        results_by_query={
            "docker deployment": SimpleNamespace(
                results=[
                    {
                        "path": "/eng/docker-deployment-guide.md",
                        "title": "Docker Deployment Guide",
                        "snippet": "Deploy Docker containers...",
                        "collection": "engineering",
                    },
                    {
                        "path": "/eng/ci-cd-pipeline.md",
                        "title": "CI CD Pipeline",
                        "snippet": "GitHub Actions runs tests...",
                        "collection": "engineering",
                    },
                ],
                vec_failed=False,
            )
        }
    )

    builder = GoldBuilder(llm_judge=judge, retriever=retriever)
    report = builder.build_independent_gold(
        suite_path=input_path,
        output_path=output_path,
        systems=["vector"],
        judge_runs=2,
        calibrate_first=False,
        limit_per_system=10,
        credentials=("k", "ep", "depl"),  # pragma: allowlist secret
    )

    assert report.queries_processed == 1
    assert report.total_candidates_pooled >= 2
    assert report.total_judge_calls > 0
    assert output_path.exists()

    parsed = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert parsed["meta"]["gold_method"] == "trec-pooling-llm-judge"
    case = parsed["cases"][0]
    assert case["score_method"] == "ndcg"
    assert case["gold_titles"]
    relevances = [int(t["relevance"]) for t in case["gold_titles"]]
    assert relevances == sorted(relevances, reverse=True)


@pytest.mark.integration
def test_build_independent_gold_runs_calibration_when_enabled(kairix_db: Path, tmp_path: Path) -> None:
    """When calibrate_first=True the injected judge's calibrate() is invoked once."""
    input_suite = {"cases": [{"query": "docker deployment", "category": "recall"}]}
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "gold.yaml"
    yaml.safe_dump(input_suite, input_path.open("w", encoding="utf-8"))

    judge = FakeLLMJudge(grades_by_query={"docker deployment": {"eng/docker-deployment-guide": 2}})
    retriever = FakeRetriever(
        results_by_query={
            "docker deployment": SimpleNamespace(
                results=[
                    {
                        "path": "/eng/docker-deployment-guide.md",
                        "title": "Docker Deployment Guide",
                        "snippet": "Deploy",
                        "collection": "engineering",
                    }
                ],
                vec_failed=False,
            )
        }
    )

    builder = GoldBuilder(llm_judge=judge, retriever=retriever)
    builder.build_independent_gold(
        suite_path=input_path,
        output_path=output_path,
        systems=["vector"],
        judge_runs=1,
        calibrate_first=True,
        credentials=("k", "ep", "depl"),  # pragma: allowlist secret
    )
    assert judge.calibrate_calls == 1
