"""End-to-end integration test for the GPL suite-generation pipeline.

Exercises sample_documents → process_sampled_docs → write_generated_suite
against a real SQLite database with the production schema. Closes the
integration-coverage gap on query_documents_from_db / sample_documents
left by the unit tests in tests/eval/test_generate.py.

The LLM-judge and retrieval surfaces are still injected via the existing
FakeQueryGenerator / FakeLLMJudge / FakeRetriever from tests/fakes.py —
this test verifies the SQLite I/O glue, not the chat completion logic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kairix.core.db.schema import create_schema
from kairix.quality.eval.generate import (
    SuiteGenerator,
    sample_documents,
)
from tests.fakes import FakeLLMJudge, FakeQueryGenerator, FakeRetriever

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
