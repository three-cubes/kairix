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
    build_case,
    build_generation_prompt,
    default_chat_backend,
    empty_generation_result,
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
def test_suite_generator_process_sampled_docs_runs_pipeline_on_mock_docs() -> None:
    """SuiteGenerator.process_sampled_docs runs the full GPL pipeline against in-memory docs.

    Skips the DB-sampling step (no `sample_fn` injection seam) by handing
    pre-built doc dicts straight to the pipeline method. Verifies the
    protocol-injected QueryGenerator / LLMJudge / Retriever drive the loop.
    """
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

    accepted, _rejected, _failed, counts = suite_gen.process_sampled_docs(
        mock_docs,
        5,
        ["procedural"],
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
    )

    assert len(accepted) >= 1
    assert counts["procedural"] >= 1
    assert len(qg.calls) >= 1
    assert len(jg.grade_calls) >= 1
    assert len(retriever.calls) >= 1


@pytest.mark.unit
def test_suite_generator_process_sampled_docs_with_no_docs_returns_zero() -> None:
    """SuiteGenerator.process_sampled_docs returns zero counts when given no docs."""
    suite_gen = SuiteGenerator()
    accepted, rejected, failed, counts = suite_gen.process_sampled_docs(
        [],
        10,
        ["recall"],
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert accepted == []
    assert rejected == 0
    assert failed == 0
    assert counts == {"recall": 0}


@pytest.mark.unit
def test_suite_generator_falls_back_to_free_judge_batch_when_no_llm_judge_injected() -> None:
    """When llm_judge is None, SuiteGenerator routes judging through ``judge_batch``.

    Without a configured provider in ``kairix.config.yaml``, the free
    ``judge_batch`` shim's lazy default construction
    (``ProviderEvalChatBackend.from_config()``) raises ``ValueError``
    with an actionable affordance. That error propagates out of
    ``process_sampled_docs`` — pinning the production "fail fast on
    missing provider" contract.

    Sabotage proof: regressing the SuiteGenerator to silently swallow the
    fallback's exception (e.g. wrapping ``_judge`` in a broad try/except)
    would let this test fall through without raising; pinned by the
    explicit ``pytest.raises(ValueError)`` match here.
    """
    mock_query = GeneratedQuery(
        query="q1",
        intent="recall",
        source_doc_path="docs/x.md",
        source_doc_title="x",
    )
    qg = FakeQueryGenerator(queries_by_title={"Doc": [mock_query]})
    retriever = FakeRetriever(results_by_query={"q1": _retrieval_result(["doc.md"], ["snip"])})
    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=None, retriever=retriever)
    docs = [{"path": "docs/x.md", "title": "Doc", "collection": "shared", "body": "x" * 500}]

    with pytest.raises(ValueError, match="provider:"):
        suite_gen.process_sampled_docs(docs, 5, ["recall"], api_key="", endpoint="")


@pytest.mark.unit
def test_suite_generator_falls_back_to_free_retrieve_when_no_retriever_injected() -> None:
    """When retriever is None, SuiteGenerator routes retrieval through ``_retrieve``.

    In the test environment _retrieve fails (no FTS table) and returns ([], []),
    so each query is counted as n_failed — exercising the fallback branch.
    """
    mock_query = GeneratedQuery(
        query="q1",
        intent="recall",
        source_doc_path="docs/x.md",
        source_doc_title="x",
    )
    qg = FakeQueryGenerator(queries_by_title={"Doc": [mock_query]})
    jg = FakeLLMJudge(grades_by_query={})
    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=jg, retriever=None)
    docs = [{"path": "docs/x.md", "title": "Doc", "collection": "shared", "body": "x" * 500}]

    accepted, _rejected, failed, _ = suite_gen.process_sampled_docs(docs, 5, ["recall"], api_key="", endpoint="")
    assert accepted == []
    assert failed == 1


@pytest.mark.unit
def test_suite_generator_falls_back_to_free_generate_queries_when_no_query_generator() -> None:
    """When query_generator is None, SuiteGenerator routes to ``generate_queries``.

    Without a configured provider, the free ``generate_queries``'s lazy
    default backend (``ProviderEvalChatBackend.from_config()``) raises
    ``ValueError`` and the error propagates out — pinning the production
    "fail fast on missing provider" contract for the query-generator
    fallback branch in ``_generate_queries``.

    Sabotage proof: regressing the SuiteGenerator to silently swallow the
    fallback's exception would let this test fall through; pinned by
    ``pytest.raises(ValueError)``.
    """
    suite_gen = SuiteGenerator(query_generator=None)
    docs = [{"path": "docs/x.md", "title": "Doc", "collection": "shared", "body": "x" * 500}]

    with pytest.raises(ValueError, match="provider:"):
        suite_gen.process_sampled_docs(docs, 5, ["recall"], api_key="", endpoint="")


@pytest.mark.unit
def test_suite_generator_generate_suite_returns_error_on_credential_failure(tmp_path: Path) -> None:
    """generate_suite catches credential-fetch failures and returns errors=[...] result."""
    output = tmp_path / "out.yaml"
    suite_gen = SuiteGenerator()
    # api_key=None and endpoint=None forces resolve_credentials. The test env has no
    # secrets configured, so resolve_credentials raises — we expect generate_suite
    # to capture it via empty_generation_result rather than raising.
    result = suite_gen.generate_suite(
        output_path=str(output),
        n_cases=1,
        api_key=None,
        endpoint=None,
        calibrate_first=False,
        db_path="/this/path/does-not-exist.sqlite",
    )
    assert result.n_accepted == 0


@pytest.mark.unit
def test_suite_generator_generate_suite_returns_error_on_calibration_failure(tmp_path: Path) -> None:
    """When calibrate_first=True and the judge fails calibration, errors are captured."""
    from kairix.quality.eval.judge import JudgeCalibrationError

    class _FailingJudge:
        def grade(self, query: str, candidates: list[tuple[str, str]], *, runs: int = 1) -> JudgeResult:
            del query, candidates, runs
            raise AssertionError("not used in this test")

        def calibrate(self) -> bool:
            raise JudgeCalibrationError("calibration failed for the test")

    output = tmp_path / "out.yaml"
    suite_gen = SuiteGenerator(llm_judge=_FailingJudge())  # type: ignore[arg-type]  # test double satisfies Judge protocol structurally
    result = suite_gen.generate_suite(
        output_path=str(output),
        n_cases=1,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
        calibrate_first=True,
    )
    assert result.n_accepted == 0
    assert any("calibration" in err.lower() for err in result.errors)


@pytest.mark.unit
def test_suite_generator_generate_suite_falls_back_to_default_db_path_when_empty(tmp_path: Path) -> None:
    """db_path='' triggers the lazy default-db-path lookup branch.

    Inject fakes for ``query_generator`` / ``llm_judge`` / ``retriever`` so
    the production-default fallbacks (which now eagerly construct a
    provider-backed ``ChatBackend`` via ``ProviderEvalChatBackend.from_config()``)
    are bypassed — the test pins ``if not db_path: db_path = _get_db_path_str()``,
    not the chat-backend resolution path.
    """
    output = tmp_path / "out.yaml"
    suite_gen = SuiteGenerator(
        query_generator=FakeQueryGenerator(queries_by_title={}),
        llm_judge=FakeLLMJudge(grades_by_query={}),
        retriever=FakeRetriever(results_by_query={}),
    )
    # An empty db_path string forces the `if not db_path: db_path = _get_db_path_str()`
    # branch. We expect it to return an empty result without raising.
    result = suite_gen.generate_suite(
        db_path="",
        output_path=str(output),
        n_cases=1,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
        calibrate_first=False,
    )
    assert isinstance(result, GenerationResult)


@pytest.mark.unit
def test_suite_generator_enrich_suite_handles_load_failure(tmp_path: Path) -> None:
    """enrich_suite catches yaml-load failures and returns an EnrichmentResult with errors."""
    suite_gen = SuiteGenerator()
    result = suite_gen.enrich_suite(
        suite_path=str(tmp_path / "does-not-exist.yaml"),
        output_path=str(tmp_path / "out.yaml"),
        api_key="k",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert result.n_cases == 0
    assert any("Failed to load" in err for err in result.errors)


@pytest.mark.unit
def test_suite_generator_enrich_suite_skips_case_with_empty_query(tmp_path: Path) -> None:
    """A case missing or with empty 'query' is skipped without invoking the judge."""
    input_path = tmp_path / "in.yaml"
    output_path = tmp_path / "out.yaml"
    yaml.safe_dump(
        {"meta": {"version": "1.0"}, "cases": [{"id": "X1", "query": "", "category": "recall"}]},
        input_path.open("w", encoding="utf-8"),
    )

    jg = FakeLLMJudge(grades_by_query={})
    rt = FakeRetriever(results_by_query={})
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=rt)
    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_path),
        api_key="k",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert result.n_cases == 1
    assert result.n_skipped == 1
    assert len(jg.grade_calls) == 0


@pytest.mark.unit
def test_suite_generator_enrich_suite_returns_error_on_credential_failure(tmp_path: Path) -> None:
    """Missing credentials surface as errors in the EnrichmentResult, not a raise."""
    suite_gen = SuiteGenerator()
    result = suite_gen.enrich_suite(
        suite_path=str(tmp_path / "ignored.yaml"),
        output_path=str(tmp_path / "out.yaml"),
        api_key=None,
        endpoint=None,
    )
    assert result.n_cases == 0


# resolve_credentials' deployment-from-vault override branch (line 587) requires
# a FakeCredentials helper to exercise without monkeypatching. Deferred to the
# credentials-DI initiative — the branch is honest dead code under current test
# infrastructure (see feedback_quality_gate_no_overrides.md).


# ---------------------------------------------------------------------------
# Free-function shim coverage (production defaults)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_free_process_sampled_docs_shim_delegates_to_suite_generator() -> None:
    """The free ``process_sampled_docs`` shim runs against the production defaults."""
    from kairix.quality.eval.generate import process_sampled_docs as free_psd

    accepted, _, _, _ = free_psd(
        docs=[],
        n_cases=1,
        active_cats=["recall"],
        api_key="",
        endpoint="",
        deployment=JUDGE_DEPLOYMENT,
        agent="shape",
    )
    assert accepted == []


@pytest.mark.unit
def test_free_generate_suite_shim_delegates_to_suite_generator(tmp_path: Path) -> None:
    """The free ``generate_suite`` shim returns a result without raising."""
    output = tmp_path / "out.yaml"
    result = generate_suite(
        db_path=str(tmp_path / "noop.sqlite"),
        output_path=str(output),
        n_cases=1,
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
        calibrate_first=False,
    )
    assert isinstance(result, GenerationResult)


@pytest.mark.unit
def test_free_enrich_suite_shim_delegates_to_suite_generator(tmp_path: Path) -> None:
    """The free ``enrich_suite`` shim returns an EnrichmentResult without raising."""
    result = enrich_suite(
        suite_path=str(tmp_path / "missing.yaml"),
        output_path=str(tmp_path / "out.yaml"),
        api_key="key",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert isinstance(result, EnrichmentResult)


@pytest.mark.unit
def test_suite_generator_process_sampled_docs_breaks_when_n_cases_already_hit() -> None:
    """Outer doc-loop break fires when accepted_cases already meets n_cases (n_cases=0)."""
    suite_gen = SuiteGenerator()
    docs = [{"path": "x.md", "title": "X", "collection": "k", "body": "x" * 500}]
    accepted, rejected, failed, _ = suite_gen.process_sampled_docs(
        docs, n_cases=0, active_cats=["recall"], api_key="", endpoint=""
    )
    assert accepted == []
    assert rejected == 0
    assert failed == 0


@pytest.mark.unit
def test_suite_generator_process_sampled_docs_inner_break_after_target_hit() -> None:
    """Inner query-loop break fires when n_cases is hit mid-doc.

    Two queries per doc; after the first case is accepted, the second query
    triggers the inner break instead of being processed.
    """
    q1 = GeneratedQuery(query="q1", intent="recall", source_doc_path="x.md", source_doc_title="X")
    q2 = GeneratedQuery(query="q2", intent="recall", source_doc_path="x.md", source_doc_title="X")
    qg = FakeQueryGenerator(queries_by_title={"X": [q1, q2]})
    jg = FakeLLMJudge(grades_by_query={"q1": {"d": 2}})
    rt = FakeRetriever(
        results_by_query={
            "q1": _retrieval_result(["d.md"], ["s"]),
            "q2": _retrieval_result(["d.md"], ["s"]),
        }
    )
    suite_gen = SuiteGenerator(query_generator=qg, llm_judge=jg, retriever=rt)
    docs = [{"path": "x.md", "title": "X", "collection": "k", "body": "x" * 500}]

    accepted, _, _, _ = suite_gen.process_sampled_docs(docs, n_cases=1, active_cats=["recall"], api_key="", endpoint="")
    assert len(accepted) == 1
    # Second query never made it through the judge — only one grade call recorded.
    assert len(jg.grade_calls) == 1


@pytest.mark.unit
def test_suite_generator_enrich_suite_appends_error_on_unwritable_output(tmp_path: Path) -> None:
    """Output path that is a directory triggers the write-failure except branch."""
    input_path = tmp_path / "in.yaml"
    yaml.safe_dump(
        {"meta": {}, "cases": [{"id": "X1", "query": "q", "category": "recall"}]},
        input_path.open("w", encoding="utf-8"),
    )
    output_dir = tmp_path / "out_dir"
    output_dir.mkdir()  # treating a directory as the output file → write fails

    jg = FakeLLMJudge(grades_by_query={})
    rt = FakeRetriever(results_by_query={})
    suite_gen = SuiteGenerator(llm_judge=jg, retriever=rt)
    result = suite_gen.enrich_suite(
        suite_path=str(input_path),
        output_path=str(output_dir),
        api_key="k",  # pragma: allowlist secret
        endpoint="https://ep",
    )
    assert any("Failed to write" in err for err in result.errors)


@pytest.mark.unit
def test_suite_generator_calibrate_falls_back_to_free_calibrate_when_no_judge(tmp_path: Path) -> None:
    """When llm_judge=None and calibrate_first=True, _calibrate falls through to ``calibrate``.

    Without a configured provider, the free ``calibrate`` shim's lazy default
    backend (``ProviderEvalChatBackend.from_config()``) raises ``ValueError``.
    ``generate_suite`` only catches ``JudgeCalibrationError`` from the
    ``_calibrate`` step, so the ValueError propagates — pinning the production
    "fail fast on missing provider" contract for the calibrate fallback.

    Sabotage proof: regressing ``_calibrate`` to silently mask the fallback's
    exception (e.g. broad try/except returning ``False``) would let this test
    fall through without raising; pinned by ``pytest.raises(ValueError)``.
    """
    output = tmp_path / "out.yaml"
    suite_gen = SuiteGenerator(llm_judge=None)
    with pytest.raises(ValueError, match="provider:"):
        suite_gen.generate_suite(
            db_path=str(tmp_path / "noop.sqlite"),
            output_path=str(output),
            n_cases=1,
            api_key="k",  # pragma: allowlist secret
            endpoint="https://ep",
            calibrate_first=True,
        )


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
# Credential-failure handling — pipeline-level coverage
#
# ``enrich_suite`` credential-failure handling is verified at the pipeline
# level: passing both ``api_key`` and ``endpoint`` as non-empty strings means
# ``resolve_credentials`` is never called, and the rest of the pipeline runs
# through the injected protocol fakes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_suite_handles_runtime_failure_via_chat_backend(tmp_path: Path) -> None:
    """enrich_suite returns EnrichmentResult (not raises) when the judge backend errors.

    Any RuntimeError from the LLM call path (which a credential rejection from
    the API would surface as) is captured and surfaced via result.errors /
    n_failed rather than propagating. The branch is driven via FakeLLMJudge
    wired to a chat backend that always raises.
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
# empty_generation_result — early-exit helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_generation_result_returns_zero_counts() -> None:
    """The early-exit helper returns a GenerationResult with all-zero counts."""
    # Opaque path label — never opened. Avoid /tmp to satisfy "publicly
    # writable directory" hotspot rule even though no file is ever written.
    result = empty_generation_result("fixtures/x.yaml", calibration_passed=False, errors=["e"])
    assert result.n_generated == 0
    assert result.n_accepted == 0
    assert result.n_rejected == 0
    assert result.n_failed == 0
    assert result.calibration_passed is False
    assert result.errors == ["e"]


# ---------------------------------------------------------------------------
# default_chat_backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_chat_backend_raises_value_error_when_no_provider_configured() -> None:
    """Production fallback constructs a provider-backed ``ChatBackend``.

    Without a configured provider in ``kairix.config.yaml`` (the default
    test-environment shape), ``default_chat_backend()`` raises
    ``ValueError`` with an actionable ``fix:`` affordance pointing the
    operator at the missing ``provider:`` field. F5-clean: the test
    exercises the public factory's failure contract, not the concrete
    adapter class.

    Sabotage proof: regressing the factory to silently return a stub
    (e.g. ``object()``) would skip the raise and fail the
    ``pytest.raises`` assertion.
    """
    with pytest.raises(ValueError, match="provider:"):
        default_chat_backend()


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
def test_generate_queries_raises_when_default_backend_has_no_provider_configured() -> None:
    """When neither chat_backend nor llm_fn is supplied, the function calls
    ``default_chat_backend()`` which constructs a provider-backed adapter
    via ``ProviderEvalChatBackend.from_config()``. Without a configured
    provider, that factory raises ``ValueError`` and the error propagates
    out of ``generate_queries`` rather than being swallowed.

    Sabotage proof: regressing the function to swallow construction
    errors (e.g. wrapping the lazy default in a broad try/except returning
    ``[]``) would let this test fall through silently — pinned by the
    explicit ``pytest.raises(ValueError)`` here.
    """
    with pytest.raises(ValueError, match="provider:"):
        generate_queries(
            doc_title="t",
            doc_body="b" * 200,
            n=1,
            categories=["recall"],
            api_key="",
            endpoint="",
        )


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
