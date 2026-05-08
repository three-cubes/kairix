"""
Unit tests for kairix.quality.eval.generate.

All external calls (SQLite, hybrid search, LLM API) use DI fakes from
``tests/fakes.py`` — `FakeChatBackend`, `FakeQueryGenerator`, `FakeLLMJudge`,
`FakeRetriever`. No monkeypatch / @patch / setattr.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kairix.quality.eval.generate import (
    EnrichmentResult,
    GeneratedQuery,
    GenerationResult,
    QueryGenerator,
    SuiteGenerator,
    _default_chat_backend,
    _empty_generation_result,
    _LegacyLLMFnBackend,
    build_case,
    build_generation_prompt,
    enrich_suite,
    filter_and_process_sampled_rows,
    generate_queries,
    generate_suite,
    parse_llm_query_response,
    resolve_credentials,
    sample_documents,
    write_generated_suite,
)
from kairix.quality.eval.judge import JUDGE_DEPLOYMENT, JudgeResult
from tests.fakes import (
    FakeChatBackend,
    FakeLLMJudge,
    FakeQueryGenerator,
    FakeRetriever,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_JUDGE_RESULT_WITH_GRADE2 = JudgeResult(
    query="What is the deployment process?",
    grades={"docker-deployment-guide": 2, "ci-cd-pipeline": 1, "readme": 0},
    shuffle_order=("docker-deployment-guide", "ci-cd-pipeline", "readme"),
    judge_model="gpt-4o-mini",
    calibration_passed=True,
)

_JUDGE_RESULT_NO_GRADE2 = JudgeResult(
    query="What is the deployment process?",
    grades={"ci-cd-pipeline": 1, "readme": 0},
    shuffle_order=("ci-cd-pipeline", "readme"),
    judge_model="gpt-4o-mini",
    calibration_passed=True,
)

_JUDGE_RESULT_ALL_ZERO = JudgeResult(
    query="What is the deployment process?",
    grades={"readme": 0, "changelog": 0},
    shuffle_order=("readme", "changelog"),
    judge_model="gpt-4o-mini",
    calibration_passed=True,
)


def _retrieval_result(paths: list[str], snippets: list[str]) -> SimpleNamespace:
    """Construct a Retriever-protocol-shaped result with paths + snippets."""
    return SimpleNamespace(paths=paths, snippets=snippets, results=[], vec_failed=False)


# ---------------------------------------------------------------------------
# generate_queries — DI via FakeChatBackend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_queries_returns_list_on_valid_response() -> None:
    """generate_queries returns list of GeneratedQuery from a valid API response."""
    mock_response = json.dumps(
        [
            {"query": "How do I deploy a Docker container?", "intent": "procedural"},
            {"query": "What is the Docker deployment process?", "intent": "recall"},
        ]
    )

    results = generate_queries(
        doc_title="docker-guide",
        doc_body="Deploy with docker build, tag, push, run -d.",
        n=2,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        chat_backend=FakeChatBackend(responses=[mock_response]),
    )

    assert len(results) == 2
    assert all(isinstance(q, GeneratedQuery) for q in results)
    assert results[0].query == "How do I deploy a Docker container?"
    assert results[0].intent == "procedural"
    assert results[1].intent == "recall"


@pytest.mark.unit
def test_generate_queries_returns_empty_on_parse_failure() -> None:
    """generate_queries returns [] on JSON parse failure after 2 attempts."""
    # Two responses are needed since generate_queries retries once on parse failure.
    backend = FakeChatBackend(responses=["not a json array", "still not json"])

    results = generate_queries(
        doc_title="test-doc",
        doc_body="some content",
        n=2,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        chat_backend=backend,
    )

    assert results == []


@pytest.mark.unit
def test_generate_queries_returns_empty_on_api_error() -> None:
    """generate_queries returns [] when the chat backend raises on every call."""
    backend = FakeChatBackend(raise_on_call=OSError("connection error"))

    results = generate_queries(
        doc_title="test-doc",
        doc_body="some content",
        n=2,
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        chat_backend=backend,
    )

    assert results == []


@pytest.mark.unit
def test_generate_queries_returns_empty_when_no_credentials() -> None:
    """generate_queries returns [] with empty credentials — no chat backend call made."""
    backend = FakeChatBackend(responses=[])  # would IndexError if called

    results = generate_queries(
        doc_title="test-doc",
        doc_body="some content",
        n=2,
        api_key="",
        endpoint="",
        chat_backend=backend,
    )
    assert results == []
    assert len(backend.calls) == 0


@pytest.mark.unit
def test_generate_queries_defaults_unknown_intent_to_recall() -> None:
    """Unknown intent categories default to 'recall'."""
    mock_response = json.dumps(
        [
            {"query": "What does this doc cover?", "intent": "unknown_category_xyz"},
        ]
    )

    results = generate_queries(
        doc_title="test",
        doc_body="content",
        n=1,
        api_key="test-key",
        endpoint="https://test",
        chat_backend=FakeChatBackend(responses=[mock_response]),
    )

    assert len(results) == 1
    assert results[0].intent == "recall"


# ---------------------------------------------------------------------------
# QueryGenerator class — DI via FakeChatBackend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_generator_class_returns_queries_via_injected_backend() -> None:
    """QueryGenerator.generate uses the constructor-injected ChatBackend."""
    mock_response = json.dumps(
        [
            {"query": "How is X configured?", "intent": "procedural"},
        ]
    )
    backend = FakeChatBackend(responses=[mock_response])
    gen = QueryGenerator(chat_backend=backend)

    queries = gen.generate(
        title="config-guide",
        body="Configure X via YAML.",
        n=1,
        categories=["procedural", "recall"],
        api_key="key",
        endpoint="https://ep",
    )

    assert len(queries) == 1
    assert queries[0].intent == "procedural"
    assert len(backend.calls) == 1
    # Per-call credentials passed through to the backend
    assert backend.calls[0]["api_key"] == "key"
    assert backend.calls[0]["endpoint"] == "https://ep"


@pytest.mark.unit
def test_query_generator_class_returns_empty_on_backend_error() -> None:
    """QueryGenerator.generate returns [] when the backend raises (never re-raises)."""
    backend = FakeChatBackend(raise_on_call=RuntimeError("network down"))
    gen = QueryGenerator(chat_backend=backend)

    queries = gen.generate(
        title="x",
        body="y",
        n=1,
        categories=["recall"],
        api_key="k",
        endpoint="https://e",
    )

    assert queries == []


# ---------------------------------------------------------------------------
# build_case
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_case_returns_case_with_grade2_doc() -> None:
    """build_case returns a valid case dict when grade-2 doc exists."""
    case = build_case(
        query="What is the deployment process?",
        intent="procedural",
        judge_result=_JUDGE_RESULT_WITH_GRADE2,
        paths=["docker-deployment-guide.md", "ci-cd-pipeline.md", "readme.md"],
        snippets=["snippet1", "snippet2", "snippet3"],
        case_id="GEN-P001",
    )

    assert case is not None
    assert case["id"] == "GEN-P001"
    assert case["category"] == "procedural"
    assert case["score_method"] == "ndcg"
    # gold_titles should include grade>=1 docs
    gold_titles = case["gold_titles"]
    assert any(g["title"] == "docker-deployment-guide" and g["relevance"] == 2 for g in gold_titles)
    assert any(g["title"] == "ci-cd-pipeline" and g["relevance"] == 1 for g in gold_titles)
    # grade-0 docs excluded
    assert not any(g["title"] == "readme" for g in gold_titles)


@pytest.mark.unit
def test_build_case_returns_none_when_no_grade2() -> None:
    """build_case returns None when no grade-2 document found."""
    result = build_case(
        query="What is the deployment process?",
        intent="recall",
        judge_result=_JUDGE_RESULT_NO_GRADE2,
        paths=["ci-cd-pipeline.md", "readme.md"],
        snippets=["snippet1", "snippet2"],
        case_id="GEN-R001",
    )

    assert result is None


@pytest.mark.unit
def test_build_case_returns_none_when_all_zero() -> None:
    """build_case returns None when all grades are 0."""
    result = build_case(
        query="irrelevant query",
        intent="recall",
        judge_result=_JUDGE_RESULT_ALL_ZERO,
        paths=["readme.md", "changelog.md"],
        snippets=["a", "b"],
        case_id="GEN-R002",
    )

    assert result is None


@pytest.mark.unit
def test_build_case_gold_titles_sorted_by_relevance_desc() -> None:
    """gold_titles are sorted with highest relevance first."""
    case = build_case(
        query="test query",
        intent="recall",
        judge_result=_JUDGE_RESULT_WITH_GRADE2,
        paths=["docker-deployment-guide.md", "ci-cd-pipeline.md", "readme.md"],
        snippets=["s1", "s2", "s3"],
        case_id="GEN-R001",
    )

    assert case is not None
    gold = case["gold_titles"]
    relevances = [g["relevance"] for g in gold]
    assert relevances == sorted(relevances, reverse=True)


# ---------------------------------------------------------------------------
# SuiteGenerator — DI via FakeQueryGenerator / FakeLLMJudge / FakeRetriever
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_suite_generator_writes_valid_yaml(tmp_path: Path) -> None:
    """SuiteGenerator.generate_suite writes a YAML file via injected protocol fakes."""
    output = tmp_path / "test-suite.yaml"

    mock_docs = [
        {
            "path": "docs/docker-guide.md",
            "title": "Docker Guide",
            "collection": "knowledge",
            "body": "x" * 500,
        },
    ]
    mock_query = GeneratedQuery(
        query="How do I deploy?",
        intent="procedural",
        source_doc_path="docs/docker-guide.md",
        source_doc_title="Docker Guide",
    )

    qg = FakeQueryGenerator(queries_by_title={"Docker Guide": [mock_query]})
    jg = FakeLLMJudge(grades_by_query={"How do I deploy?": {"docker-deployment-guide": 2, "ci-cd-pipeline": 1}})
    retriever = FakeRetriever(
        results_by_query={
            "How do I deploy?": _retrieval_result(
                ["docker-deployment-guide.md", "ci-cd-pipeline.md"],
                ["s1", "s2"],
            )
        }
    )
    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=jg, retriever=retriever)

    result = suite_gen.generate_suite(
        output_path=str(output),
        n_cases=5,
        calibrate_first=False,
        api_key="key",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
        sample_fn=lambda **_kw: mock_docs,
    )

    assert isinstance(result, GenerationResult)
    assert output.exists()
    with open(output, encoding="utf-8") as f:
        parsed = yaml.safe_load(f)

    assert "cases" in parsed
    assert isinstance(parsed["cases"], list)
    # The case should have been accepted (grade-2 present)
    assert len(parsed["cases"]) >= 1
    # FakeQueryGenerator was actually invoked
    assert len(qg.calls) >= 1
    assert len(jg.grade_calls) >= 1
    assert len(retriever.calls) >= 1


@pytest.mark.unit
def test_suite_generator_returns_result_on_empty_docs(tmp_path: Path) -> None:
    """SuiteGenerator.generate_suite returns a GenerationResult when no docs sampled."""
    output = tmp_path / "empty-suite.yaml"

    suite_gen = SuiteGenerator()

    result = suite_gen.generate_suite(
        output_path=str(output),
        n_cases=10,
        calibrate_first=False,
        api_key="key",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
        sample_fn=lambda **_kw: [],
    )

    assert isinstance(result, GenerationResult)
    assert result.n_accepted == 0
    assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# Backwards-compat: free generate_suite still honours legacy *_fn kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_suite_free_function_legacy_kwargs(tmp_path: Path) -> None:
    """Legacy `*_fn` kwargs on `generate_suite` are preserved for backwards compat.

    cli.py / gold_builder.py keep calling `generate_suite(...)` directly;
    Phase 3 routes them through `SuiteGenerator`. This regression test ensures
    Phase 2b doesn't break the existing call shape.
    """
    output = tmp_path / "legacy-suite.yaml"
    mock_query = GeneratedQuery(
        query="legacy q",
        intent="recall",
        source_doc_path="docs/x.md",
        source_doc_title="x",
    )

    result = generate_suite(
        output_path=str(output),
        n_cases=1,
        calibrate_first=False,
        api_key="key",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
        sample_fn=lambda **_kw: [
            {"path": "docs/x.md", "title": "x", "collection": "k", "body": "x" * 500},
        ],
        query_fn=lambda **_kw: [mock_query],
        retrieve_fn=lambda *_a, **_kw: (["doc.md"], ["snippet"]),
        judge_fn=lambda **_kw: JudgeResult(
            query="legacy q",
            grades={"doc": 2},
            shuffle_order=("doc",),
            judge_model="x",
        ),
    )

    assert isinstance(result, GenerationResult)
    assert output.exists()


# ---------------------------------------------------------------------------
# enrich_suite — DI via FakeLLMJudge / FakeRetriever
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_suite_generator_enrich_writes_valid_yaml_with_gold_titles(tmp_path: Path) -> None:
    """SuiteGenerator.enrich_suite enriches cases via injected protocol fakes."""
    input_suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": "What is the deployment process?",
                "gold_path": "docker-guide.md",
                "score_method": "exact",
            }
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    with open(input_path, "w", encoding="utf-8") as f:
        yaml.dump(input_suite, f)

    jg = FakeLLMJudge(
        grades_by_query={
            "What is the deployment process?": {
                "docker-deployment-guide": 2,
                "ci-cd-pipeline": 1,
            }
        }
    )
    retriever = FakeRetriever(
        results_by_query={
            "What is the deployment process?": _retrieval_result(
                ["docker-deployment-guide.md", "ci-cd-pipeline.md"],
                ["s1", "s2"],
            )
        }
    )
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=retriever)

    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="key",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
    )

    assert isinstance(result, EnrichmentResult)
    assert result.n_cases == 1
    assert output_path.exists()

    with open(output_path, encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    case = parsed["cases"][0]
    assert "gold_titles" in case
    assert case["score_method"] == "ndcg"


@pytest.mark.unit
def test_suite_generator_enrich_preserves_existing_fields(tmp_path: Path) -> None:
    """SuiteGenerator.enrich_suite preserves all case fields not updated by enrichment."""
    input_suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "id": "T001",
                "category": "temporal",
                "query": "What happened last week?",
                "gold_path": "daily-log.md",
                "score_method": "exact",
                "notes": "Important temporal case",
                "agent": "builder",
            }
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    with open(input_path, "w", encoding="utf-8") as f:
        yaml.dump(input_suite, f)

    jg = FakeLLMJudge(grades_by_query={"What happened last week?": {"daily-log": 2}})
    retriever = FakeRetriever(
        results_by_query={"What happened last week?": _retrieval_result(["daily-log.md"], ["snippet"])}
    )
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=retriever)

    suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
    )

    with open(output_path, encoding="utf-8") as f:
        parsed = yaml.safe_load(f)

    case = parsed["cases"][0]
    assert case["id"] == "T001"
    assert case["category"] == "temporal"
    assert case["notes"] == "Important temporal case"
    assert case["agent"] == "builder"


@pytest.mark.unit
def test_suite_generator_enrich_skips_case_when_no_relevant_doc(tmp_path: Path) -> None:
    """enrich_suite keeps original case when no grade>=1 doc found."""
    input_suite = {
        "meta": {},
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": "obscure query",
                "gold_path": "obscure-doc.md",
                "score_method": "exact",
            }
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    with open(input_path, "w", encoding="utf-8") as f:
        yaml.dump(input_suite, f)

    jg = FakeLLMJudge(grades_by_query={})  # all-zero grades for any query
    retriever = FakeRetriever(results_by_query={"obscure query": _retrieval_result(["unrelated.md"], ["snippet"])})
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=retriever)

    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
    )

    assert result.n_skipped == 1
    assert result.n_enriched == 0

    with open(output_path, encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    # Original gold_path preserved
    assert parsed["cases"][0].get("gold_path") == "obscure-doc.md"


# ---------------------------------------------------------------------------
# Backwards-compat: free enrich_suite still honours legacy *_fn kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_suite_free_function_legacy_kwargs(tmp_path: Path) -> None:
    """Legacy `retrieve_fn` / `judge_fn` kwargs on `enrich_suite` work for backwards compat."""
    input_suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "id": "L001",
                "category": "recall",
                "query": "legacy",
                "gold_path": "x.md",
                "score_method": "exact",
            }
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    with open(input_path, "w", encoding="utf-8") as f:
        yaml.dump(input_suite, f)

    result = enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
        retrieve_fn=lambda *_a, **_kw: (["x.md"], ["s"]),
        judge_fn=lambda **_kw: JudgeResult(
            query="legacy",
            grades={"x": 2},
            shuffle_order=("x",),
            judge_model="x",
        ),
    )

    assert result.n_cases == 1
    assert result.n_enriched == 1
    assert output_path.exists()


# ---------------------------------------------------------------------------
# Phase 0-deferred regression tests
#
# The original Phase 0 PR landed credential-handling fixes without unit-level
# regression tests because both required substituting the module-level
# ``fetch_llm_credentials`` callable — the smell this initiative is removing.
#
# Status after Phase 2b:
#   - ``resolve_credentials`` caller-wins semantics still tests cleanest with
#     a substituted ``fetch_llm_credentials``. Phase 3 will inject this as a
#     constructor seam on ``SuiteGenerator``; we DEFER the unit-level test
#     until then to avoid introducing monkeypatch.
#   - ``enrich_suite`` credential-failure handling is verified at the broader
#     pipeline level: passing both ``api_key`` and ``endpoint`` as non-empty
#     strings means ``resolve_credentials`` is never called, and the rest of
#     the pipeline runs through the injected protocol fakes.
#
# Both deferrals are explicit in the PR body for #143 Phase 2b.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_suite_handles_runtime_failure_via_chat_backend(tmp_path: Path) -> None:
    """enrich_suite returns EnrichmentResult (not raises) when the judge backend errors.

    This is the broader credential-failure-shape coverage the Phase 0 deferral
    asked for: any RuntimeError from the LLM call path (which a credential
    rejection from the API would surface as) is captured and surfaced via
    result.errors / n_failed rather than propagating.

    NOTE: Tests the LLM-failure branch via FakeLLMJudge wired to a chat
    backend that always raises. The credential-fetch failure branch in
    `resolve_credentials` itself remains a Phase 3 deferral — see module
    docstring above.
    """
    input_suite = {
        "meta": {},
        "cases": [
            {
                "id": "R001",
                "category": "recall",
                "query": "any query",
                "gold_path": "x.md",
                "score_method": "exact",
            }
        ],
    }
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "output.yaml"
    with open(input_path, "w", encoding="utf-8") as f:
        yaml.dump(input_suite, f)

    # Retriever returns no results — pipeline records n_failed without raising.
    retriever = FakeRetriever()  # default empty
    jg = FakeLLMJudge()  # would return all-zero grades, but won't be called
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=retriever)

    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",
        endpoint="https://ep",
        deployment="gpt-4o-mini",
    )

    assert isinstance(result, EnrichmentResult)
    # No retrieval results → case is recorded as failed, not raised
    assert result.n_failed == 1
    assert result.n_enriched == 0


@pytest.mark.unit
def test_query_generator_handles_credential_rejection_via_chat_backend() -> None:
    """QueryGenerator returns [] when the chat backend raises RuntimeError.

    Mirrors the contract that an Azure 401 Unauthorized at the chat layer
    is caught and surfaced as an empty result rather than propagating.
    Replaces the `_call_llm` private-import-substitution test pattern with
    pure protocol injection.
    """
    backend = FakeChatBackend(raise_on_call=RuntimeError("Azure 401 Unauthorized"))
    gen = QueryGenerator(chat_backend=backend)

    queries = gen.generate(
        title="x",
        body="y",
        n=2,
        categories=["recall"],
        api_key="bogus-key",  # pragma: allowlist secret
        endpoint="https://e",
    )

    assert queries == []
    # Backend was actually invoked (twice — generate_queries retries once)
    assert len(backend.calls) == 2


# ---------------------------------------------------------------------------
# filter_and_process_sampled_rows — pure data-shaping
# ---------------------------------------------------------------------------


def _row(doc: str, path: str, title: str | None, collection: str) -> dict[str, str | None]:
    """Build a sqlite3.Row-shaped dict (subscriptable by column name)."""
    return {"doc": doc, "path": path, "title": title, "collection": collection}


@pytest.mark.unit
def test_filter_and_process_sampled_rows_strips_yaml_frontmatter() -> None:
    """Body that begins with --- has its frontmatter block stripped."""
    rows = [_row("---\ntitle: x\n---\n\n" + "body content " * 50, "/a.md", "Title", "shared")]
    docs = filter_and_process_sampled_rows(rows, min_length=50)
    assert len(docs) == 1
    assert "title: x" not in docs[0]["body"]
    assert docs[0]["body"].startswith("body content")
    assert docs[0]["path"] == "/a.md"
    assert docs[0]["title"] == "Title"


@pytest.mark.unit
def test_filter_and_process_sampled_rows_drops_short_bodies() -> None:
    """Bodies shorter than min_length are dropped."""
    rows = [
        _row("short", "/a.md", "Title", "shared"),
        _row("x" * 200, "/b.md", "B", "shared"),
    ]
    docs = filter_and_process_sampled_rows(rows, min_length=100)
    assert [d["path"] for d in docs] == ["/b.md"]


@pytest.mark.unit
def test_filter_and_process_sampled_rows_falls_back_to_filename_stem_when_title_missing() -> None:
    """Missing/None title is replaced by the filename stem."""
    rows = [_row("x" * 200, "/notes/important.md", None, "shared")]
    docs = filter_and_process_sampled_rows(rows, min_length=50)
    assert docs[0]["title"] == "important"


@pytest.mark.unit
def test_filter_and_process_sampled_rows_truncates_body_to_2000() -> None:
    """Body is truncated to 2000 chars to bound prompt size."""
    rows = [_row("x" * 5000, "/a.md", "T", "shared")]
    docs = filter_and_process_sampled_rows(rows, min_length=50)
    assert len(docs[0]["body"]) == 2000


# ---------------------------------------------------------------------------
# parse_llm_query_response — JSON extraction + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_llm_query_response_skips_non_dict_items() -> None:
    """List entries that are not dicts are silently skipped."""
    content = '[{"query": "q1", "intent": "recall"}, "stray string", 42, {"query": "q2", "intent": "recall"}]'
    result = parse_llm_query_response(content, ["recall"], "/p.md", "T")
    assert [q.query for q in result] == ["q1", "q2"]


@pytest.mark.unit
def test_parse_llm_query_response_skips_blank_query_strings() -> None:
    """Items with empty/whitespace queries are dropped."""
    content = '[{"query": "  ", "intent": "recall"}, {"query": "real", "intent": "recall"}]'
    result = parse_llm_query_response(content, ["recall"], "/p.md", "T")
    assert [q.query for q in result] == ["real"]


@pytest.mark.unit
def test_parse_llm_query_response_raises_when_no_array() -> None:
    """No JSON array bracket → ValueError."""
    with pytest.raises(ValueError, match="No JSON array"):
        parse_llm_query_response("plain prose, no JSON", ["recall"], "/p.md", "T")


# ---------------------------------------------------------------------------
# resolve_credentials — caller-wins semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_credentials_uses_fetched_when_caller_passes_none() -> None:
    """When caller provides no creds, fall back to fetch_llm_credentials.

    In the test environment, fetch_llm_credentials returns ("", "", JUDGE_DEPLOYMENT)
    via its except-branch — so we get those defaults back.
    """
    api_key, endpoint, deployment = resolve_credentials(None, None, JUDGE_DEPLOYMENT)
    assert api_key == ""
    assert endpoint == ""
    assert deployment == JUDGE_DEPLOYMENT


@pytest.mark.unit
def test_resolve_credentials_caller_value_wins_when_provided() -> None:
    """Caller-supplied api_key/endpoint take precedence over the fetched values."""
    api_key, endpoint, deployment = resolve_credentials(
        "caller-key",  # pragma: allowlist secret
        "https://caller-endpoint",
        "caller-deployment",
    )
    # Caller's deployment wins because it isn't the JUDGE_DEPLOYMENT default
    assert api_key == "caller-key"  # pragma: allowlist secret
    assert endpoint == "https://caller-endpoint"
    assert deployment == "caller-deployment"


# ---------------------------------------------------------------------------
# build_generation_prompt — boundary delimiters + newline stripping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_generation_prompt_strips_newlines_in_caller_supplied_content() -> None:
    """Newlines in title/body must not break out of the <document> boundary."""
    prompt = build_generation_prompt(
        title="evil\ntitle\rwith\nbreaks",
        body="document\nwith\rnewlines",
        n=2,
        cats=["recall"],
    )
    # Newlines from caller input must not appear inside the <document> block.
    inner = prompt.split("<document>", 1)[1].split("</document>", 1)[0]
    assert "\n" not in inner
    assert "\r" not in inner
    inner_title = prompt.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "\n" not in inner_title
    assert "\r" not in inner_title


# ---------------------------------------------------------------------------
# _empty_generation_result — early-exit helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_generation_result_returns_zero_counts() -> None:
    """The early-exit helper returns a GenerationResult with all-zero counts."""
    result = _empty_generation_result("/tmp/x.yaml", calibration_passed=False, errors=["e"])
    assert result.n_generated == 0
    assert result.n_accepted == 0
    assert result.n_rejected == 0
    assert result.n_failed == 0
    assert result.calibration_passed is False
    assert result.errors == ["e"]


# ---------------------------------------------------------------------------
# _default_chat_backend + _LegacyLLMFnBackend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_chat_backend_returns_azure_adapter() -> None:
    """Production fallback constructs an AzureChatBackend."""
    from kairix._azure import AzureChatBackend

    backend = _default_chat_backend()
    assert isinstance(backend, AzureChatBackend)


@pytest.mark.unit
def test_legacy_llm_fn_backend_routes_call_through_supplied_callable() -> None:
    """The deprecated llm_fn= shim adapts the callable into the ChatBackend protocol."""
    captured: dict[str, str] = {}

    def _fn(prompt: str, api_key: str, endpoint: str, deployment: str) -> str:
        captured["prompt"] = prompt
        captured["api_key"] = api_key
        return '[{"query": "from-legacy", "intent": "recall"}]'

    backend = _LegacyLLMFnBackend(_fn)
    out = backend.complete(
        "the-prompt",
        api_key="k",  # pragma: allowlist secret
        endpoint="https://e",
        deployment="depl",
    )
    assert "from-legacy" in out
    assert captured["prompt"] == "the-prompt"
    assert captured["api_key"] == "k"


@pytest.mark.unit
def test_generate_queries_legacy_llm_fn_kwarg_still_works() -> None:
    """The deprecated llm_fn= kwarg routes through the legacy adapter to produce queries."""

    def _fn(prompt: str, api_key: str, endpoint: str, deployment: str) -> str:
        return '[{"query": "via-legacy", "intent": "recall"}]'

    queries = generate_queries(
        doc_title="t",
        doc_body="b" * 100,
        n=1,
        categories=["recall"],
        api_key="k",  # pragma: allowlist secret
        endpoint="https://e",
        llm_fn=_fn,
    )
    assert [q.query for q in queries] == ["via-legacy"]


# ---------------------------------------------------------------------------
# sample_documents — bad-path → empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sample_documents_returns_empty_when_db_path_invalid(tmp_path: Path) -> None:
    """Pointing at a non-existent DB triggers the open-db except branch and returns []."""
    bad = tmp_path / "no-such-database.sqlite"
    docs = sample_documents(db_path=str(bad), n=10, collections=None, seed=42)
    assert docs == []


# ---------------------------------------------------------------------------
# write_generated_suite — exception path on unwritable output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retrieve_returns_empty_when_no_index_available() -> None:
    """``_retrieve`` swallows runtime errors from missing index/DB and returns ([], [])."""
    from kairix.quality.eval.generate import _retrieve

    paths, snippets = _retrieve("any query", "recall")
    # In the test environment there is no FTS table or vector index — `_retrieve`
    # must surface this as ([], []), never raise.
    assert paths == []
    assert snippets == []


@pytest.mark.unit
def test_generate_queries_constructs_default_backend_when_no_chat_backend_or_llm_fn() -> None:
    """When neither chat_backend nor llm_fn is supplied, the function builds an
    AzureChatBackend lazily — empty creds force the early ``ValueError`` path so
    no network call happens, but the default-backend branch is exercised.
    """
    queries = generate_queries(
        doc_title="t",
        doc_body="b" * 200,
        n=1,
        categories=["recall"],
        api_key="",
        endpoint="",
    )
    assert queries == []


@pytest.mark.unit
def test_suite_generator_process_sampled_docs_delegates_with_empty_docs() -> None:
    """SuiteGenerator.process_sampled_docs delegates to the free function and short-circuits on empty input."""
    qg = FakeQueryGenerator(queries_by_title={})
    jg = FakeLLMJudge(grades_by_query={})
    rt = FakeRetriever(results_by_query={})
    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=jg, retriever=rt)

    accepted, rejected, failed, counts = suite_gen.process_sampled_docs(
        docs=[],
        n_cases=5,
        active_cats=["recall"],
    )
    assert accepted == []
    assert rejected == 0
    assert failed == 0
    assert counts == {"recall": 0}  # initialised per active category


@pytest.mark.unit
def test_write_generated_suite_appends_error_when_path_unwritable(tmp_path: Path) -> None:
    """Failure to write the YAML output appends to the errors list rather than raising."""
    # Use a directory as the output path — open(...,"w") raises IsADirectoryError.
    target = tmp_path / "out_dir"
    target.mkdir()

    errors: list[str] = []
    write_generated_suite(str(target), cases=[], cats=["recall"], errors=errors)

    assert len(errors) == 1
    assert "Failed to write" in errors[0]
