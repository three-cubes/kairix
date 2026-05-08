"""End-to-end integration tests for the GPL suite-generation pipeline.

Exercises every public method on QueryGenerator and SuiteGenerator end-to-end:
- sample_documents → process_sampled_docs → write_generated_suite
- QueryGenerator.generate via FakeChatBackend
- SuiteGenerator.enrich_suite against a real YAML file
- SuiteGenerator.generate_suite against a real SQLite database

The chat / retrieval / judge surfaces inject FakeXxx from tests/fakes.py — no
monkeypatch. Production credential resolution is bypassed by passing api_key
/ endpoint explicitly so resolve_credentials() is never called.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kairix.core.db.schema import create_schema
from kairix.quality.eval.generate import (
    QueryGenerator,
    SuiteGenerator,
    sample_documents,
)
from tests.fakes import FakeChatBackend, FakeLLMJudge, FakeQueryGenerator, FakeRetriever

pytestmark = pytest.mark.integration


def _seed_documents(db_path: Path, *, n: int = 5) -> None:
    """Populate a SQLite file with the production schema and ``n`` sample docs."""
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    for i in range(n):
        body = f"This is document {i}. " * 30
        path = f"/notes/doc{i}.md"
        title = f"Doc {i}"
        digest = f"hash-{i}"
        cur.execute(
            "INSERT INTO content (hash, doc) VALUES (?, ?)",
            (digest, body),
        )
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (path, title, "shared", digest, "2026-05-01", "2026-05-01"),
        )
    db.commit()
    db.close()


@pytest.mark.integration
def test_sample_documents_round_trips_against_real_sqlite(tmp_path: Path) -> None:
    """sample_documents finds rows, filters them, and shuffles the survivors."""
    db_path = tmp_path / "kairix.sqlite"
    _seed_documents(db_path, n=5)

    docs = sample_documents(db_path=str(db_path), n=5, collections=None, seed=42)

    assert len(docs) > 0
    assert {d["collection"] for d in docs} == {"shared"}
    assert all("doc" not in d["body"] or "document" in d["body"] for d in docs)
    assert all(d["title"].startswith("Doc ") for d in docs)


@pytest.mark.integration
def test_sample_documents_with_collections_filter(tmp_path: Path) -> None:
    """The collections argument scopes the SELECT to the named collections."""
    db_path = tmp_path / "kairix.sqlite"
    _seed_documents(db_path, n=3)

    docs = sample_documents(
        db_path=str(db_path),
        n=3,
        collections=["shared"],
    )
    assert len(docs) == 3

    # Filtering to a non-existent collection returns nothing.
    none = sample_documents(
        db_path=str(db_path),
        n=3,
        collections=["does-not-exist"],
    )
    assert none == []


@pytest.mark.integration
def test_suite_generator_pipeline_against_real_sqlite(tmp_path: Path) -> None:
    """SuiteGenerator drives the full pipeline against a real DB + fake judge / retriever."""
    db_path = tmp_path / "kairix.sqlite"
    _seed_documents(db_path, n=3)
    output_path = tmp_path / "suite.yaml"

    # Each sampled doc gets one query; the judge returns a grade-2 result so the
    # case is accepted; the retriever returns the doc's path back as the only candidate.
    from kairix.quality.eval.generate import GeneratedQuery

    queries_by_title = {
        f"Doc {i}": [
            GeneratedQuery(
                query=f"deploy-question-{i}",
                intent="recall",
                source_doc_path=f"/notes/doc{i}.md",
                source_doc_title=f"Doc {i}",
            )
        ]
        for i in range(3)
    }
    qg = FakeQueryGenerator(queries_by_title=queries_by_title)

    # FakeLLMJudge takes {query: {stem: grade}} — it builds the JudgeResult itself.
    grades_by_query = {f"deploy-question-{i}": {f"doc{i}": 2} for i in range(3)}
    jg = FakeLLMJudge(grades_by_query=grades_by_query)

    results_by_query = {
        f"deploy-question-{i}": SimpleNamespace(
            paths=[f"/notes/doc{i}.md"],
            snippets=[f"snippet for doc{i}"],
            results=[],
            vec_failed=False,
        )
        for i in range(3)
    }
    rt = FakeRetriever(results_by_query=results_by_query)

    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=jg, retriever=rt)
    result = suite_gen.generate_suite(
        db_path=str(db_path),
        output_path=str(output_path),
        n_cases=3,
        categories=["recall"],
        api_key="fake-key",  # pragma: allowlist secret
        endpoint="https://fake-endpoint",
        calibrate_first=False,
        seed=42,
    )

    assert result.n_accepted >= 1
    assert output_path.exists()


@pytest.mark.integration
def test_query_generator_full_cycle_against_fake_chat_backend() -> None:
    """QueryGenerator.generate runs the full prompt-build → backend → parse cycle."""
    payload = json.dumps(
        [
            {"query": "How do I deploy a Docker container?", "intent": "procedural"},
            {"query": "What is the Docker deployment process?", "intent": "recall"},
        ]
    )
    backend = FakeChatBackend(responses=[payload])
    gen = QueryGenerator(chat_backend=backend)

    queries = gen.generate(
        title="docker-guide",
        body="Deploy with docker build, tag, push, run -d." * 20,
        n=2,
        categories=["recall", "procedural"],
        api_key="integration-key",  # pragma: allowlist secret
        endpoint="https://integration-endpoint",
        source_doc_path="/notes/docker-guide.md",
    )

    assert len(queries) == 2
    assert queries[0].intent == "procedural"
    assert queries[1].intent == "recall"
    assert all(q.source_doc_path == "/notes/docker-guide.md" for q in queries)
    # Backend call carried the credentials and the assembled prompt.
    assert backend.calls[0]["api_key"] == "integration-key"  # pragma: allowlist secret
    assert "docker-guide" in backend.calls[0]["prompt"] or "Docker" in backend.calls[0]["prompt"]


@pytest.mark.integration
def test_suite_generator_enrich_suite_full_cycle_against_real_yaml(tmp_path: Path) -> None:
    """SuiteGenerator.enrich_suite reads/writes real YAML and regrades cases via fakes."""
    input_suite = {
        "meta": {"version": "1.0", "score_method": "exact"},
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": "What is the deployment process?",
                "gold_path": "docker-guide.md",
                "score_method": "exact",
            },
            {
                "id": "R002",
                "category": "recall",
                "query": "",  # empty — should be skipped
            },
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "enriched.yaml"
    yaml.safe_dump(input_suite, input_path.open("w", encoding="utf-8"))

    jg = FakeLLMJudge(
        grades_by_query={
            "What is the deployment process?": {
                "docker-deployment-guide": 2,
                "ci-cd-pipeline": 1,
            }
        }
    )
    rt = FakeRetriever(
        results_by_query={
            "What is the deployment process?": SimpleNamespace(
                paths=["docker-deployment-guide.md", "ci-cd-pipeline.md"],
                snippets=["s1", "s2"],
                results=[],
                vec_failed=False,
            )
        }
    )
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=rt)
    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",  # pragma: allowlist secret
        endpoint="https://ep",
    )

    assert result.n_cases == 2
    assert result.n_enriched == 1
    assert result.n_skipped == 1
    assert output_path.exists()

    parsed = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    enriched_case = next(c for c in parsed["cases"] if c["id"] == "R001")
    assert enriched_case["score_method"] == "ndcg"
    assert enriched_case["gold_titles"]
    # Gold titles sorted by relevance descending.
    relevances = [int(t["relevance"]) for t in enriched_case["gold_titles"]]
    assert relevances == sorted(relevances, reverse=True)
    # Skipped case is preserved unchanged.
    skipped_case = next(c for c in parsed["cases"] if c["id"] == "R002")
    assert "gold_titles" not in skipped_case
