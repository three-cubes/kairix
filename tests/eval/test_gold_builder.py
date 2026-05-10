"""Unit tests for kairix.quality.eval.gold_builder — TREC pooling and gold suite building.

All tests construct ``GoldBuilder`` with constructor-injected ``FakeLLMJudge`` /
``FakeRetriever`` from tests/fakes.py. No monkey-patching, no @patch, no
setattr, no ``*_fn=`` substitution kwargs. The pool/BM25 path is exercised
end-to-end against a real SQLite database in tests/integration/test_eval_gold_pipeline.py.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from kairix.core.db.schema import create_schema
from kairix.quality.eval.gold_builder import (
    GoldBuilder,
    GoldBuildReport,
    PooledCandidate,
    grade_candidates,
    path_title,
)
from kairix.quality.eval.judge import JudgeResult
from tests.fakes import FakeLLMJudge, FakeRetriever

# ---------------------------------------------------------------------------
# GoldBuilder.grade — class-method tests via FakeLLMJudge injection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Free-function shims (DEPRECATED public surface — verify they delegate cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grade_candidates_shim_delegates_to_class() -> None:
    """The free ``grade_candidates`` shim builds a default GoldBuilder.

    Empty candidates short-circuit inside the class method; this asserts the
    shim invocation path itself is valid (signature + delegation).
    """
    result = grade_candidates(
        "q",
        [],
        "k",
        "ep",  # pragma: allowlist secret
    )
    assert result == []


@pytest.mark.unit
def test_gold_builder_grade_majority_vote_across_alternating_runs() -> None:
    """When 3 runs return [2, 2, 0], majority vote selects grade 2."""
    call_count = [0]

    class _AlternatingJudge:
        def grade(self, query: str, candidates: list[tuple[str, str]], *, runs: int = 1) -> JudgeResult:
            call_count[0] += 1
            grade = 2 if call_count[0] <= 2 else 0
            return JudgeResult(
                query=query,
                grades={candidates[0][0]: grade},
                shuffle_order=tuple(c[0] for c in candidates),
                judge_model="alternating",
            )

        def calibrate(self) -> bool:
            return True

    candidates = [
        PooledCandidate(path="/path/doc1.md", title="Doc 1", snippet="text", collection="eng"),
    ]
    result = GoldBuilder(llm_judge=_AlternatingJudge()).grade(  # type: ignore[arg-type]
        "q",
        candidates,
        runs=3,
        api_key="k",
        endpoint="ep",  # pragma: allowlist secret
    )
    assert result[0].grade == 2
    assert result[0].grade_votes == [2, 2, 0]


# ---------------------------------------------------------------------------
# build_independent_gold edge cases (full-build is in tests/integration/)
# ---------------------------------------------------------------------------


class TestBuildIndependentGold:
    @pytest.mark.unit
    def test_no_credentials_returns_empty_report(self, tmp_path: Path) -> None:
        """Missing credentials short-circuit before the judge runs."""
        from kairix.quality.eval.gold_builder import build_independent_gold

        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(yaml.dump({"cases": [{"query": "q"}]}))

        report = build_independent_gold(suite_path, tmp_path / "out.yaml", credentials=("", "", ""))
        assert report.queries_processed == 0

    @pytest.mark.unit
    def test_no_cases_returns_empty_report(self, tmp_path: Path) -> None:
        """Suite with no cases returns an empty GoldBuildReport without raising."""
        suite_path = tmp_path / "empty.yaml"
        suite_path.write_text(yaml.dump({"cases": []}))
        builder = GoldBuilder(llm_judge=FakeLLMJudge(grades_by_query={}), retriever=FakeRetriever())
        report = builder.build_independent_gold(
            suite_path,
            tmp_path / "out.yaml",
            credentials=("k", "ep", "depl"),  # pragma: allowlist secret
        )
        assert report.queries_processed == 0

    @pytest.mark.unit
    def test_skips_cases_with_empty_query(self, tmp_path: Path) -> None:
        """Cases without a query field are silently skipped."""
        suite_path = tmp_path / "with_empty.yaml"
        suite_path.write_text(yaml.dump({"cases": [{"query": "", "category": "recall"}]}))
        builder = GoldBuilder(llm_judge=FakeLLMJudge(grades_by_query={}), retriever=FakeRetriever())
        report = builder.build_independent_gold(
            suite_path,
            tmp_path / "out.yaml",
            credentials=("k", "ep", "depl"),  # pragma: allowlist secret
            calibrate_first=False,
        )
        assert report.queries_processed == 0

    @pytest.mark.unit
    def test_gold_build_report_defaults(self):
        report = GoldBuildReport()
        assert report.queries_processed == 0
        assert report.grade_distribution == {0: 0, 1: 0, 2: 0}


# ---------------------------------------------------------------------------
# path_title uniqueness (Bug 1)
# ---------------------------------------------------------------------------


class TestPathTitle:
    @pytest.mark.unit
    def testpath_title_unique_for_readme_files(self):
        """Two readme.md files in different directories produce different titles."""
        t1 = path_title("reference-library/engineering/adr-examples/readme.md")
        t2 = path_title("reference-library/data-and-analysis/dbt-docs/readme.md")
        assert t1 != t2

    @pytest.mark.unit
    def testpath_title_deep_path(self):
        """Deep paths preserve enough context to be unique."""
        t = path_title("reference-library/agentic-ai/panaversity-agentic/03_ai_protocols/01_mcp/readme.md")
        assert "01_mcp" in t
        assert "readme" in t

    @pytest.mark.unit
    def testpath_title_short_path(self):
        """A short path (2 segments) returns all segments minus extension."""
        t = path_title("collection/doc.md")
        assert t == "collection/doc"

    @pytest.mark.unit
    def testpath_title_single_segment(self):
        """A single-segment path returns just the stem."""
        t = path_title("readme.md")
        assert t == "readme"

    @pytest.mark.unit
    def testpath_title_strips_md_extension(self):
        t = path_title("reference-library/engineering/patterns.md")
        assert not t.endswith(".md")
        assert "patterns" in t


# ---------------------------------------------------------------------------
# grade_candidates — duplicate stem handling (Bug 2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GoldBuilder class — constructor-injected Fakes
# ---------------------------------------------------------------------------


class TestGoldBuilderInit:
    @pytest.mark.unit
    def test_constructs_with_explicit_fakes(self) -> None:
        """GoldBuilder accepts ``llm_judge`` / ``retriever`` kwargs."""
        judge = FakeLLMJudge()
        retriever = FakeRetriever()
        builder = GoldBuilder(llm_judge=judge, retriever=retriever)
        assert builder._llm_judge is judge
        assert builder._retriever is retriever

    @pytest.mark.unit
    def test_constructs_without_args_lazy_defaults(self) -> None:
        """Defaults are lazy — constructor doesn't touch production deps."""
        builder = GoldBuilder()
        assert builder._llm_judge is None
        assert builder._retriever is None


class TestGoldBuilderPool:
    @pytest.mark.unit
    def test_vector_system_routes_through_retriever(self) -> None:
        """``pool`` with system 'vector' uses the injected retriever."""
        retriever = FakeRetriever(
            results_by_query={
                "deploy docker": SimpleNamespace(
                    results=[
                        {
                            "path": "ops/docker.md",
                            "title": "Docker",
                            "snippet": "Build, tag, push.",
                            "collection": "ops",
                        },
                        {
                            "path": "ops/k8s.md",
                            "title": "K8s",
                            "snippet": "Pods and services.",
                            "collection": "ops",
                        },
                    ],
                    vec_failed=False,
                )
            }
        )
        builder = GoldBuilder(llm_judge=FakeLLMJudge(), retriever=retriever)
        result = builder.pool("deploy docker", ["vector"], limit_per_system=10)
        assert len(result) == 2
        paths = {c.path for c in result}
        assert paths == {"ops/docker.md", "ops/k8s.md"}
        assert all("vector" in c.sources for c in result)

    @pytest.mark.unit
    def test_pool_records_retriever_call(self) -> None:
        """The injected retriever's call list captures the query + collections."""
        retriever = FakeRetriever()
        builder = GoldBuilder(retriever=retriever)
        builder.pool("q1", ["vector"], collections=["ops", "eng"], limit_per_system=5)
        assert len(retriever.calls) == 1
        assert retriever.calls[0]["query"] == "q1"
        assert retriever.calls[0]["collections"] == ["ops", "eng"]

    @pytest.mark.unit
    def test_pool_unknown_system_skipped(self) -> None:
        """Unknown system names are logged and skipped, not raised."""
        retriever = FakeRetriever()
        builder = GoldBuilder(retriever=retriever)
        result = builder.pool("q", ["nosuchsystem"])
        assert result == []

    @pytest.mark.unit
    def test_pool_handles_retriever_with_paths_shape(self) -> None:
        """A RetrievalResult-shaped value (paths/snippets) is also handled."""
        retriever = FakeRetriever(
            results_by_query={
                "q": SimpleNamespace(
                    paths=["doc1.md", "doc2.md"],
                    snippets=["snip1", "snip2"],
                    meta={},
                )
            }
        )
        builder = GoldBuilder(retriever=retriever)
        result = builder.pool("q", ["vector"])
        assert len(result) == 2
        assert {c.path for c in result} == {"doc1.md", "doc2.md"}

    @pytest.mark.unit
    def test_pool_dedupes_when_same_path_appears_in_multiple_systems(self) -> None:
        """Same path returned by multiple systems collapses to one candidate."""
        retriever = FakeRetriever(
            results_by_query={
                "q": SimpleNamespace(
                    results=[
                        {"path": "a.md", "title": "A", "snippet": "x", "collection": "c"},
                    ],
                    vec_failed=False,
                )
            }
        )
        builder = GoldBuilder(retriever=retriever)
        result = builder.pool("q", ["vector"])
        assert len(result) == 1
        assert result[0].sources == ["vector"]


class TestGoldBuilderGrade:
    @pytest.mark.unit
    def test_grade_assigns_configured_grades(self) -> None:
        """``grade()`` writes the configured grades onto each candidate."""
        # Keys are path_title() output — every segment is preserved for uniqueness.
        # ``/path/doc1.md`` → ``/path/doc1``.
        judge = FakeLLMJudge(grades_by_query={"q1": {"/path/doc1": 2, "/path/doc2": 1}})
        builder = GoldBuilder(llm_judge=judge)

        candidates = [
            PooledCandidate(path="/path/doc1.md", title="Doc 1", snippet="t", collection="eng"),
            PooledCandidate(path="/path/doc2.md", title="Doc 2", snippet="t", collection="eng"),
        ]

        result = builder.grade("q1", candidates, runs=1)
        assert result[0].grade == 2
        assert result[1].grade == 1

    @pytest.mark.unit
    def test_grade_majority_vote_across_runs(self) -> None:
        """Multi-run grade uses majority vote — same answer twice, grade is fixed."""
        # path_title("col/a/doc.md") = "col/a/doc" (every segment preserved).
        judge = FakeLLMJudge(grades_by_query={"q": {"col/a/doc": 2}})
        builder = GoldBuilder(llm_judge=judge)
        candidates = [PooledCandidate(path="col/a/doc.md", title="Doc", snippet="t", collection="col")]
        result = builder.grade("q", candidates, runs=3)
        assert result[0].grade == 2
        assert result[0].grade_votes == [2, 2, 2]
        assert judge.grade_calls and len(judge.grade_calls) == 3

    @pytest.mark.unit
    def test_grade_empty_candidates(self) -> None:
        builder = GoldBuilder(llm_judge=FakeLLMJudge())
        assert builder.grade("q", [], runs=1) == []

    @pytest.mark.unit
    def test_grade_unknown_query_returns_zero_grades(self) -> None:
        """An unconfigured query yields all-zero grades (FakeLLMJudge default)."""
        judge = FakeLLMJudge()  # no grades_by_query — defaults to zero
        builder = GoldBuilder(llm_judge=judge)
        candidates = [PooledCandidate(path="/p/d.md", title="D", snippet="s", collection="c")]
        result = builder.grade("uncovered", candidates, runs=1)
        assert result[0].grade == 0


class TestGoldBuilderBuildIndependentGold:
    @pytest.mark.unit
    def test_end_to_end_with_fakes(self, tmp_path) -> None:
        """``build_independent_gold`` runs end-to-end against tmp_path output."""
        retriever = FakeRetriever(
            results_by_query={
                "test query": SimpleNamespace(
                    results=[
                        {
                            "path": "eng/relevant.md",
                            "title": "Relevant",
                            "snippet": "Good content",
                            "collection": "eng",
                        },
                        {
                            "path": "eng/noise.md",
                            "title": "Noise",
                            "snippet": "Bad content",
                            "collection": "eng",
                        },
                    ],
                    vec_failed=False,
                )
            }
        )
        # path_title("eng/relevant.md") -> "eng/relevant" (2 segments, no drop).
        judge = FakeLLMJudge(
            grades_by_query={
                "test query": {"eng/relevant": 2, "eng/noise": 0},
            }
        )
        builder = GoldBuilder(llm_judge=judge, retriever=retriever)

        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(
            yaml.dump(
                {
                    "cases": [
                        {"query": "test query", "category": "recall", "score_method": "ndcg"},
                    ]
                }
            )
        )
        output_path = tmp_path / "out" / "gold.yaml"

        report = builder.build_independent_gold(
            suite_path=suite_path,
            output_path=output_path,
            systems=["vector"],
            credentials=("api-key", "https://endpoint", "gpt-4o-mini"),
            judge_runs=1,
        )

        assert report.queries_processed == 1
        assert report.total_candidates_pooled == 2
        assert output_path.exists()

        output = yaml.safe_load(output_path.read_text())
        gold_titles = output["cases"][0]["gold_titles"]
        # Only 'relevant' grade>=1; 'noise' filtered out
        assert len(gold_titles) == 1
        assert gold_titles[0]["title"] == "eng/relevant"
        assert gold_titles[0]["relevance"] == 2
        assert output["meta"]["gold_method"] == "trec-pooling-llm-judge"

        # Calibration was invoked
        assert judge.calibrate_calls == 1

    @pytest.mark.unit
    def test_skips_calibration_when_disabled(self, tmp_path) -> None:
        retriever = FakeRetriever()
        judge = FakeLLMJudge()
        builder = GoldBuilder(llm_judge=judge, retriever=retriever)

        suite_path = tmp_path / "s.yaml"
        suite_path.write_text(yaml.dump({"cases": [{"query": "q"}]}))

        builder.build_independent_gold(
            suite_path=suite_path,
            output_path=tmp_path / "out.yaml",
            systems=["vector"],
            credentials=("k", "e", "d"),
            calibrate_first=False,
        )
        assert judge.calibrate_calls == 0

    @pytest.mark.unit
    def test_no_credentials_returns_empty_report(self, tmp_path) -> None:
        builder = GoldBuilder(llm_judge=FakeLLMJudge(), retriever=FakeRetriever())
        suite_path = tmp_path / "s.yaml"
        suite_path.write_text(yaml.dump({"cases": [{"query": "q"}]}))
        report = builder.build_independent_gold(
            suite_path=suite_path,
            output_path=tmp_path / "out.yaml",
            credentials=("", "", ""),
        )
        assert report.queries_processed == 0


# ---------------------------------------------------------------------------
# Note: ``_validate_weights`` is a defensive Phase-0b guard against future
# devs adding bm25 weight presets with nan / inf / non-positive values.
# Through the public surface (``GoldBuilder.pool``), all weights come from
# the hardcoded ``_WEIGHT_PRESETS`` dict and are always finite-positive, so
# the raise branch is unreachable today and pragma'd in gold_builder.py.
# Direct tests of the guard were a code smell (test-shaped API) and have
# been removed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level deprecated function default uses JUDGE_DEPLOYMENT
# ---------------------------------------------------------------------------


class TestGradeCandidatesDeploymentDefault:
    @pytest.mark.unit
    def test_grade_candidates_uses_judge_deployment_constant(self) -> None:
        """``grade_candidates`` default deployment is ``JUDGE_DEPLOYMENT``."""
        import inspect

        from kairix.quality.eval.gold_builder import grade_candidates as gc
        from kairix.quality.eval.judge import JUDGE_DEPLOYMENT

        sig = inspect.signature(gc)
        assert sig.parameters["deployment"].default == JUDGE_DEPLOYMENT


# ---------------------------------------------------------------------------
# Branch coverage — defensive paths and fallback branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_candidates_shim_delegates_to_class() -> None:
    """The free ``pool_candidates`` shim builds a default GoldBuilder."""
    from kairix.quality.eval.gold_builder import pool_candidates

    # Without injection, the class falls through to real BM25/vector. The "vector"
    # path uses _DefaultGoldRetriever → embed_text which fails in the test env →
    # returns empty results; we just need the shim invocation path itself to be valid.
    candidates = pool_candidates("any", systems=["vector"], limit_per_system=5)
    assert isinstance(candidates, list)


@pytest.mark.unit
def test_pool_with_bm25_system_and_empty_tokenised_query_returns_no_candidates() -> None:
    """An all-punctuation query tokenises to the empty FTS string → no BM25 candidates pooled."""
    builder = GoldBuilder()
    candidates = builder.pool("!!! @@@ ###", systems=["bm25-equal"], limit_per_system=5)
    assert candidates == []


@pytest.mark.unit
def test_pool_with_vector_system_and_raising_retriever_returns_no_candidates() -> None:
    """A retriever that raises is swallowed by _vector_retrieve → vector system contributes nothing."""

    class _RaisingRetriever:
        def retrieve(self, query: str, *, collections: Any = None, cfg: Any = None) -> Any:
            raise RuntimeError("retrieval failed")

    builder = GoldBuilder(retriever=_RaisingRetriever())  # type: ignore[arg-type]
    # vector is the only requested system, and it raises → no candidates pooled.
    candidates = builder.pool("any query", systems=["vector"], limit_per_system=5)
    assert candidates == []


@pytest.mark.unit
def test_build_independent_gold_handles_legacy_gold_paths_field(tmp_path: Path) -> None:
    """A case with the deprecated ``gold_paths`` field has it preserved as ``legacy_gold_paths``."""
    suite_path = tmp_path / "input.yaml"
    suite_path.write_text(
        yaml.dump(
            {
                "cases": [
                    {
                        "id": "L1",
                        "category": "recall",
                        "query": "anything",
                        "gold_paths": ["legacy/path.md"],
                    }
                ]
            }
        )
    )
    judge = FakeLLMJudge(grades_by_query={"anything": {"x/y": 2}})
    retriever = FakeRetriever(
        results_by_query={
            "anything": SimpleNamespace(
                results=[{"path": "/x/y.md", "title": "Y", "snippet": "s", "collection": "c"}],
                vec_failed=False,
            )
        }
    )
    builder = GoldBuilder(llm_judge=judge, retriever=retriever)
    builder.build_independent_gold(
        suite_path=suite_path,
        output_path=tmp_path / "out.yaml",
        systems=["vector"],
        judge_runs=1,
        calibrate_first=False,
        credentials=("k", "ep", "depl"),  # pragma: allowlist secret
    )
    parsed = yaml.safe_load((tmp_path / "out.yaml").read_text(encoding="utf-8"))
    case = parsed["cases"][0]
    assert "gold_paths" not in case  # popped
    assert case["legacy_gold_paths"] == ["legacy/path.md"]


@pytest.mark.unit
def test_pool_returns_empty_when_db_cannot_be_opened(tmp_path: Path) -> None:
    """``GoldBuilder.pool`` (BM25 system) returns ``[]`` rather than raising
    when the constructor-injected ``db_path`` cannot be opened. Pins the
    never-raises contract on the public surface; the seam is the same
    constructor injection used for ``llm_judge`` and ``retriever``.
    """
    bad_path = tmp_path / "subdir" / "missing.sqlite"  # parent dir does not exist
    builder = GoldBuilder(db_path=bad_path)
    pooled = builder.pool("docker", systems=["bm25-equal"])
    assert pooled == [], "BM25 search must return empty when DB cannot be opened"


@pytest.mark.unit
def test_pool_returns_empty_when_fts_table_missing(tmp_path: Path) -> None:
    """``GoldBuilder.pool`` (BM25 system) returns ``[]`` rather than raising
    when the FTS5 virtual table is absent from the database. Pinned via
    constructor injection against a real SQLite file set up explicitly
    to lack the FTS table.
    """
    import sqlite3

    db_path = tmp_path / "no_fts.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, "
        "path TEXT, title TEXT, hash TEXT, active INTEGER DEFAULT 1)"
    )
    conn.execute("CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT)")
    conn.commit()
    conn.close()

    builder = GoldBuilder(db_path=db_path)
    pooled = builder.pool("docker", systems=["bm25-equal"])
    assert pooled == [], "BM25 search must return empty when FTS5 SELECT raises"


@pytest.mark.unit
def test_pool_with_bm25_system_filters_candidates_by_collection(kairix_db_path: Path) -> None:
    """``GoldBuilder.pool`` propagates the ``collections`` filter to BM25 search.

    Asserts that a seeded doc in collection ``engineering`` is pooled when the
    filter matches it AND when no filter is set, but is filtered out when the
    only requested collection is unrelated.
    """
    builder = GoldBuilder()

    pooled_with_match = builder.pool(
        "docker",
        systems=["bm25-equal"],
        collections=["engineering"],
        limit_per_system=5,
    )
    pooled_without_filter = builder.pool(
        "docker",
        systems=["bm25-equal"],
        collections=None,
        limit_per_system=5,
    )
    pooled_excluded = builder.pool(
        "docker",
        systems=["bm25-equal"],
        collections=["non-existent-collection"],
        limit_per_system=5,
    )

    matched_paths = [c.path for c in pooled_with_match]
    assert "/eng/docker.md" in matched_paths, f"expected match; got: {matched_paths}"

    unfiltered_paths = [c.path for c in pooled_without_filter]
    assert "/eng/docker.md" in unfiltered_paths, f"expected match; got: {unfiltered_paths}"

    assert pooled_excluded == [], f"expected empty pool for excluded collection; got: {pooled_excluded}"


@pytest.fixture
def kairix_db_path(tmp_path: Path) -> Iterator[Path]:
    """Production-schema SQLite with FTS5 populated; KAIRIX_DB_PATH overridden."""
    from kairix.core.db.fts import rebuild_fts

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", ("h0", "Docker deployment guide content. " * 20))
    cur.execute(
        "INSERT INTO documents (path, title, collection, hash, created_at, modified_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("/eng/docker.md", "Docker", "engineering", "h0", "2026-05-01", "2026-05-01"),
    )
    db.commit()
    rebuild_fts(db)
    db.close()
    prev = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(db_path)
    yield db_path
    if prev is None:
        os.environ.pop("KAIRIX_DB_PATH", None)
    else:
        os.environ["KAIRIX_DB_PATH"] = prev
