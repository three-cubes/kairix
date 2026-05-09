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

from kairix.core.db.fts import rebuild_fts
from kairix.core.db.schema import create_schema
from kairix.quality.eval.gold_builder import GoldBuilder
from tests.fakes import FakeLLMJudge, FakeRetriever

pytestmark = pytest.mark.integration


def _seed_db(db_path: Path, docs: list[tuple[str, str, str, str]]) -> None:
    """Create the kairix schema, populate documents + content, and rebuild the FTS index.

    Each ``docs`` tuple is (path, title, collection, body). The FTS index
    rebuild at the end is mandatory — without it ``documents_fts`` stays empty
    and any BM25 query returns nothing, which silently passes ``if results:``-
    style assertions.
    """
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    for i, (path, title, collection, body) in enumerate(docs):
        digest = f"hash-{i}"
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (digest, body))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (path, title, collection, digest, "2026-05-01", "2026-05-01"),
        )
    db.commit()
    rebuild_fts(db)
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
def test_bm25_pool_returns_seeded_doc_against_real_fts(kairix_db: Path) -> None:
    """``GoldBuilder.pool`` with a BM25 system must surface the seeded doc for 'docker deployment'."""
    builder = GoldBuilder()
    candidates = builder.pool(
        "docker deployment",
        systems=["bm25-equal"],
        collections=None,
        limit_per_system=5,
    )
    paths = [c.path for c in candidates]
    assert "/eng/docker-deployment-guide.md" in paths, (
        f"BM25 pool did not return the docker-deployment-guide; got: {paths}"
    )
    # PooledCandidate exposes the same fields downstream consumers read.
    first = candidates[0]
    assert first.path
    assert first.title is not None
    assert first.snippet is not None
    assert first.collection


@pytest.mark.integration
def test_pool_combines_bm25_variants_with_vector_retrieval(kairix_db: Path) -> None:
    """``GoldBuilder.pool`` aggregates BM25 results AND the injected retriever's vector hit."""
    retriever = FakeRetriever(
        results_by_query={
            "docker": SimpleNamespace(
                results=[
                    {
                        "path": "/notes/api-guidelines.md",
                        "title": "API Guidelines",
                        "snippet": "Public APIs require...",
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
    paths = {c.path for c in candidates}
    # BM25 must hit docker-deployment-guide for "docker"; the vector retriever
    # contributes /notes/api-guidelines.md. Both ought to be in the pool.
    assert "/eng/docker-deployment-guide.md" in paths, f"BM25 contribution missing; got: {paths}"
    assert "/notes/api-guidelines.md" in paths, f"Vector contribution missing; got: {paths}"
    # The docker-deployment-guide should be sourced by at least one BM25 system.
    docker_candidate = next(c for c in candidates if c.path == "/eng/docker-deployment-guide.md")
    assert any(s.startswith("bm25-") for s in docker_candidate.sources)
    # The api-guidelines came from the vector retriever only.
    api_candidate = next(c for c in candidates if c.path == "/notes/api-guidelines.md")
    assert "vector" in api_candidate.sources
    assert len(retriever.calls) == 1


@pytest.mark.integration
def test_pool_skips_unknown_system_with_warning(kairix_db: Path) -> None:
    """Unknown system names are skipped; pool result is identical to the known-system call."""
    builder = GoldBuilder(retriever=FakeRetriever())
    with_unknown = builder.pool(
        "docker",
        systems=["bm25-equal", "noooo-not-a-real-system"],
        collections=None,
        limit_per_system=5,
    )
    only_known = builder.pool(
        "docker",
        systems=["bm25-equal"],
        collections=None,
        limit_per_system=5,
    )
    # Skipping the unknown system must not change the pooled paths.
    assert {c.path for c in with_unknown} == {c.path for c in only_known}
    # The known BM25 system must have actually contributed.
    assert any(c.sources and "bm25-equal" in c.sources for c in with_unknown)


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
