"""Step definitions for eval_gold_builder.feature."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pytest_bdd import given, parsers, then, when

from kairix.core.db.schema import create_schema
from kairix.quality.eval.gold_builder import GoldBuilder
from tests.fakes import FakeLLMJudge, FakeRetriever

_state: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _gold_builder_scenario_state(tmp_path: Path) -> None:
    """Clear state and seed tmp_path at the start of each scenario."""
    _state.clear()
    _state["tmp_path"] = tmp_path


def _seed_db(db_path: Path) -> None:
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    docs = [
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
    ]
    for i, (path, title, collection, body) in enumerate(docs):
        digest = f"hash-{i}"
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (digest, body))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (path, title, collection, digest, "2026-05-01", "2026-05-01"),
        )
    db.commit()
    db.close()


@given("a knowledge store with documents indexed for retrieval")
def knowledge_store_with_docs() -> None:
    db_path = _state["tmp_path"] / "kairix.sqlite"
    _seed_db(db_path)
    _state["db_path"] = db_path
    _state["prev_db_env"] = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(db_path)


@given("a retriever that returns one document for the query")
def retriever_returns_one_doc() -> None:
    _state["retriever"] = FakeRetriever(
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
    _state["query"] = "docker"


@given("a retriever that returns the same document the BM25 systems retrieve")
def retriever_overlaps_bm25() -> None:
    # Use BM25-friendly query so the FTS path may also return the doc.
    _state["retriever"] = FakeRetriever(
        results_by_query={
            "docker": SimpleNamespace(
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
    _state["query"] = "docker"


@given(parsers.parse('an existing query suite asking "{query}"'))
def existing_query_suite(query: str) -> None:
    _state["query"] = query
    suite = {"cases": [{"id": "R001", "category": "recall", "query": query}]}
    input_path = _state["tmp_path"] / "input.yaml"
    yaml.safe_dump(suite, input_path.open("w", encoding="utf-8"))
    _state["input_path"] = input_path


@given("an LLM judge that grades the deployment doc as 2 and the pipeline doc as 1")
def judge_with_graded_results() -> None:
    _state["judge"] = FakeLLMJudge(
        grades_by_query={
            _state["query"]: {
                "eng/docker-deployment-guide": 2,
                "eng/ci-cd-pipeline": 1,
            }
        }
    )


@given("a retriever that returns both docs for the query")
def retriever_returns_both_docs() -> None:
    _state["retriever"] = FakeRetriever(
        results_by_query={
            _state["query"]: SimpleNamespace(
                results=[
                    {
                        "path": "/eng/docker-deployment-guide.md",
                        "title": "Docker Deployment Guide",
                        "snippet": "Deploy",
                        "collection": "engineering",
                    },
                    {
                        "path": "/eng/ci-cd-pipeline.md",
                        "title": "CI CD Pipeline",
                        "snippet": "GitHub Actions",
                        "collection": "engineering",
                    },
                ],
                vec_failed=False,
            )
        }
    )


@given("a retriever that returns nothing for any query")
def retriever_returns_nothing() -> None:
    _state["retriever"] = FakeRetriever(results_by_query={})


@when("the operator pools candidates across BM25 variants and vector retrieval")
def operator_pools_candidates() -> None:
    builder = GoldBuilder(retriever=_state["retriever"])
    _state["candidates"] = builder.pool(
        _state["query"],
        systems=["bm25-equal", "bm25-title", "vector"],
        collections=None,
        limit_per_system=10,
    )


@when("the operator builds the independent gold suite")
def operator_builds_gold_suite() -> None:
    output_path = _state["tmp_path"] / "gold.yaml"
    builder = GoldBuilder(
        llm_judge=_state.get("judge", FakeLLMJudge(grades_by_query={})),
        retriever=_state.get("retriever", FakeRetriever()),
    )
    _state["report"] = builder.build_independent_gold(
        suite_path=_state["input_path"],
        output_path=output_path,
        systems=["vector"],
        judge_runs=1,
        calibrate_first=False,
        credentials=("k", "ep", "depl"),  # pragma: allowlist secret
    )
    _state["output_path"] = output_path


@when("the operator builds the independent gold suite without credentials")
def operator_builds_gold_suite_no_creds() -> None:
    output_path = _state["tmp_path"] / "gold.yaml"
    builder = GoldBuilder()
    _state["report"] = builder.build_independent_gold(
        suite_path=_state["input_path"],
        output_path=output_path,
        systems=["vector"],
        calibrate_first=False,
        credentials=("", "", ""),
    )
    _state["output_path"] = output_path


@then("the pool contains the retrieved document")
def pool_contains_retrieved_doc() -> None:
    paths = [c.path for c in _state["candidates"]]
    assert "/eng/docker-deployment-guide.md" in paths


@then("each pooled candidate records the systems that retrieved it")
def candidates_record_systems() -> None:
    assert all(c.sources for c in _state["candidates"])


@then("duplicate documents collapse to a single candidate")
def duplicates_collapse() -> None:
    paths = [c.path for c in _state["candidates"]]
    assert len(paths) == len(set(paths))


@then("the output YAML contains graded gold_titles sorted by relevance descending")
def output_yaml_graded_descending() -> None:
    parsed = yaml.safe_load(_state["output_path"].read_text(encoding="utf-8"))
    case = parsed["cases"][0]
    assert case["gold_titles"]
    relevances = [int(t["relevance"]) for t in case["gold_titles"]]
    assert relevances == sorted(relevances, reverse=True)


@then(parsers.parse('the output meta records the gold method as "{method}"'))
def output_meta_records_method(method: str) -> None:
    parsed = yaml.safe_load(_state["output_path"].read_text(encoding="utf-8"))
    assert parsed["meta"]["gold_method"] == method


@then("the report records exactly one query processed")
def report_one_query_processed() -> None:
    assert _state["report"].queries_processed == 1


@then("the report records zero queries processed")
def report_zero_queries_processed() -> None:
    assert _state["report"].queries_processed == 0


def teardown_function() -> None:
    """Restore KAIRIX_DB_PATH after scenarios that mutated it."""
    prev = _state.get("prev_db_env")
    if prev is None:
        os.environ.pop("KAIRIX_DB_PATH", None)
    else:
        os.environ["KAIRIX_DB_PATH"] = prev
