"""Step definitions for eval_generate.feature."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pytest_bdd import given, parsers, then, when

from kairix.core.db.schema import create_schema
from kairix.quality.eval.generate import (
    GeneratedQuery,
    QueryGenerator,
    SuiteGenerator,
)
from kairix.quality.eval.judge import JudgeCalibrationError, JudgeResult
from tests.fakes import (
    FakeChatBackend,
    FakeLLMJudge,
    FakeQueryGenerator,
    FakeRetriever,
)

_state: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    _state.clear()


@pytest.fixture(autouse=True)
def _provide_tmp_path(tmp_path: Path) -> None:
    _state["tmp_path"] = tmp_path


def _retrieval_result(paths: list[str], snippets: list[str]) -> SimpleNamespace:
    return SimpleNamespace(paths=paths, snippets=snippets, results=[], vec_failed=False)


def _seed_db(db_path: Path, n: int) -> None:
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    for i in range(n):
        body = f"This is document {i}. " * 30
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (f"h{i}", body))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"/notes/doc{i}.md", f"Doc {i}", "shared", f"h{i}", "2026-05-01", "2026-05-01"),
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# QueryGenerator scenarios
# ---------------------------------------------------------------------------


@given(parsers.parse('a chat backend that returns one query "{query}" with intent "{intent}"'))
def chat_backend_returns_one_query(query: str, intent: str) -> None:
    payload = json.dumps([{"query": query, "intent": intent}])
    _state["backend"] = FakeChatBackend(responses=[payload])


@given("a chat backend that always raises an Azure 401 unauthorized error")
def chat_backend_raises_401() -> None:
    _state["backend"] = FakeChatBackend(raise_on_call=RuntimeError("Azure 401 Unauthorized"))


@when(parsers.parse('the operator generates {n:d} query for a document titled "{title}"'))
def operator_generates_queries(n: int, title: str) -> None:
    gen = QueryGenerator(chat_backend=_state["backend"])
    _state["queries"] = gen.generate(
        title=title,
        body="Deploy with docker build, tag, push, run." * 20,
        n=n,
        categories=["recall", "procedural"],
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://test.openai.azure.com",
        source_doc_path="/notes/docker-guide.md",
    )


@then("the result contains a single GeneratedQuery")
def result_has_one_query() -> None:
    assert len(_state["queries"]) == 1
    assert isinstance(_state["queries"][0], GeneratedQuery)


@then(parsers.parse('the query intent is "{intent}"'))
def query_intent_matches(intent: str) -> None:
    assert _state["queries"][0].intent == intent


@then("the source document path is recorded in the query")
def source_path_recorded() -> None:
    assert _state["queries"][0].source_doc_path == "/notes/docker-guide.md"


@then("the result contains no queries")
def result_has_no_queries() -> None:
    assert _state["queries"] == []


@then("the query-generation call returns without raising")
def no_exception_raised_generate() -> None:
    assert "queries" in _state


# ---------------------------------------------------------------------------
# SuiteGenerator scenarios
# ---------------------------------------------------------------------------


@given(parsers.parse("a SQLite knowledge store with {n:d} indexed documents"))
def sqlite_with_documents(n: int) -> None:
    db_path = _state["tmp_path"] / "kairix.sqlite"
    _seed_db(db_path, n)
    _state["db_path"] = str(db_path)
    _state["doc_count"] = n


@given("a query generator that returns one query per document")
def query_generator_one_per_doc() -> None:
    n = _state["doc_count"]
    queries_by_title = {
        f"Doc {i}": [
            GeneratedQuery(
                query=f"deploy-question-{i}",
                intent="recall",
                source_doc_path=f"/notes/doc{i}.md",
                source_doc_title=f"Doc {i}",
            )
        ]
        for i in range(n)
    }
    _state["query_generator"] = FakeQueryGenerator(queries_by_title=queries_by_title)


@given("a retriever that returns each document for its own query")
def retriever_round_trips() -> None:
    n = _state["doc_count"]
    results_by_query = {
        f"deploy-question-{i}": _retrieval_result([f"/notes/doc{i}.md"], [f"snippet for doc{i}"]) for i in range(n)
    }
    _state["retriever"] = FakeRetriever(results_by_query=results_by_query)


@given("an LLM judge that grades retrieved documents as primary answers")
def judge_grades_grade2() -> None:
    n = _state["doc_count"]
    grades = {f"deploy-question-{i}": {f"doc{i}": 2} for i in range(n)}
    _state["llm_judge"] = FakeLLMJudge(grades_by_query=grades)


@when(parsers.parse("the operator generates a suite with up to {n:d} cases"))
def operator_generates_suite(n: int) -> None:
    output_path = _state["tmp_path"] / "suite.yaml"
    suite_gen = SuiteGenerator(
        query_generator=_state["query_generator"],
        llm_judge=_state["llm_judge"],
        retriever=_state["retriever"],
    )
    _state["result"] = suite_gen.generate_suite(
        db_path=_state["db_path"],
        output_path=str(output_path),
        n_cases=n,
        categories=["recall"],
        api_key="fake-key",  # pragma: allowlist secret
        endpoint="https://fake-endpoint",
        calibrate_first=False,
        seed=42,
    )
    _state["output_path"] = output_path


@then("the output suite YAML is written to disk")
def output_yaml_exists() -> None:
    assert _state["output_path"].exists()


@then("the suite contains at least one accepted case")
def suite_has_accepted_case() -> None:
    assert _state["result"].n_accepted >= 1


@then("each accepted case carries graded gold_titles")
def cases_have_gold_titles() -> None:
    with _state["output_path"].open(encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    cases = parsed.get("cases", [])
    assert len(cases) >= 1
    for case in cases:
        assert case.get("gold_titles"), f"case {case.get('id')} missing gold_titles"


# ---------------------------------------------------------------------------
# Calibration-failure scenario
# ---------------------------------------------------------------------------


@given("an LLM judge whose calibrate() always raises a calibration error")
def judge_calibration_raises() -> None:
    class _FailingJudge:
        def grade(
            self,
            query: str,
            candidates: list[tuple[str, str]],
            *,
            runs: int = 1,
        ) -> JudgeResult:
            del query, candidates, runs
            raise AssertionError("not used in this scenario")

        def calibrate(self) -> bool:
            raise JudgeCalibrationError("calibration failed for the BDD scenario")

    _state["llm_judge"] = _FailingJudge()


@when("the operator generates a suite with calibration enabled")
def operator_generates_suite_with_calibration() -> None:
    output_path = _state["tmp_path"] / "suite.yaml"
    suite_gen = SuiteGenerator(llm_judge=_state["llm_judge"])  # type: ignore[arg-type]
    _state["result"] = suite_gen.generate_suite(
        db_path=str(_state["tmp_path"] / "noop.sqlite"),
        output_path=str(output_path),
        n_cases=1,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
        calibrate_first=True,
    )


@then("the result contains zero accepted cases")
def zero_accepted() -> None:
    assert _state["result"].n_accepted == 0


@then("the result errors mention calibration")
def errors_mention_calibration() -> None:
    assert any("calibration" in err.lower() for err in _state["result"].errors)


# ---------------------------------------------------------------------------
# Enrichment scenarios
# ---------------------------------------------------------------------------


@given(parsers.parse('an existing suite YAML with one case asking "{query}"'))
def existing_suite_with_query(query: str) -> None:
    suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": query,
                "gold_path": "docker-guide.md",
                "score_method": "exact",
            }
        ],
    }
    input_path = _state["tmp_path"] / "input.yaml"
    yaml.safe_dump(suite, input_path.open("w", encoding="utf-8"))
    _state["input_path"] = input_path
    _state["enrich_query"] = query


@given("a retriever that returns docker and ci-cd documents for the case")
def retriever_for_enrichment() -> None:
    _state["retriever"] = FakeRetriever(
        results_by_query={
            _state["enrich_query"]: _retrieval_result(
                ["docker-deployment-guide.md", "ci-cd-pipeline.md"],
                ["s1", "s2"],
            )
        }
    )


@given("an LLM judge that grades docker as 2 and ci-cd as 1")
def judge_for_enrichment() -> None:
    _state["llm_judge"] = FakeLLMJudge(
        grades_by_query={
            _state["enrich_query"]: {
                "docker-deployment-guide": 2,
                "ci-cd-pipeline": 1,
            }
        }
    )


@when("the operator enriches the suite")
def operator_enriches_suite() -> None:
    output_path = _state["tmp_path"] / "enriched.yaml"
    suite_gen = SuiteGenerator(
        llm_judge=_state.get("llm_judge"),
        retriever=_state.get("retriever"),
    )
    _state["enrich_result"] = suite_gen.enrich_suite(
        suite_path=str(_state["input_path"]),
        output_path=str(output_path),
        api_key="k",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    _state["enrich_output_path"] = output_path


@then("the enriched output is written to disk")
def enriched_output_exists() -> None:
    assert _state["enrich_output_path"].exists()


@then("the case has graded gold_titles sorted by relevance descending")
def case_gold_titles_sorted() -> None:
    with _state["enrich_output_path"].open(encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    case = parsed["cases"][0]
    titles = case["gold_titles"]
    relevances = [int(t["relevance"]) for t in titles]
    assert relevances == sorted(relevances, reverse=True)
    assert relevances[0] == 2


@then('the case score_method is "ndcg"')
def score_method_is_ndcg() -> None:
    with _state["enrich_output_path"].open(encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    assert parsed["cases"][0]["score_method"] == "ndcg"


@given("an existing suite YAML with one case that has an empty query")
def suite_with_empty_query() -> None:
    suite = {
        "meta": {"version": "1.0"},
        "cases": [{"id": "X1", "query": "", "category": "recall"}],
    }
    input_path = _state["tmp_path"] / "empty.yaml"
    yaml.safe_dump(suite, input_path.open("w", encoding="utf-8"))
    _state["input_path"] = input_path
    _state["llm_judge"] = FakeLLMJudge(grades_by_query={})
    _state["retriever"] = FakeRetriever(results_by_query={})


@then("the case is recorded as skipped")
def case_recorded_skipped() -> None:
    assert _state["enrich_result"].n_skipped == 1
    assert _state["enrich_result"].n_enriched == 0


@then("no judge call is made for the empty-query case")
def no_judge_call_made() -> None:
    judge = _state["llm_judge"]
    assert len(judge.grade_calls) == 0
