"""Unit tests for kairix.quality.eval.gold_builder — TREC pooling and gold suite building.

#143 Phase 2b — exercises the new ``GoldBuilder`` class via constructor-injected
``FakeLLMJudge`` / ``FakeRetriever``. Legacy tests still cover the deprecated
module-level functions until Phase 4 removes the ``*_fn=`` kwargs.

No monkey-patching, no @patch, no setattr — all substitution happens via
constructor injection.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from kairix.quality.eval.gold_builder import (
    GoldBuilder,
    GoldBuildReport,
    PooledCandidate,
    _validate_weights,
    grade_candidates,
    path_title,
    pool_candidates,
)
from kairix.quality.eval.judge import JudgeResult
from tests.fakes import FakeLLMJudge, FakeRetriever

# ---------------------------------------------------------------------------
# pool_candidates
# ---------------------------------------------------------------------------


def _make_bm25_fn(results: list[dict]):
    """Return a search_fn callable that returns fixed results."""

    def _fn(query, collections, limit):
        return results

    return _fn


class TestPoolCandidates:
    @pytest.mark.unit
    def test_pools_from_bm25(self):
        results = [
            {
                "path": "/doc1.md",
                "title": "Doc 1",
                "snippet": "text",
                "collection": "eng",
            },
            {
                "path": "/doc2.md",
                "title": "Doc 2",
                "snippet": "text",
                "collection": "eng",
            },
        ]
        result = pool_candidates(
            "test query",
            ["bm25-equal"],
            search_fns={"bm25-equal": _make_bm25_fn(results)},
        )
        assert len(result) == 2
        assert all(isinstance(c, PooledCandidate) for c in result)

    @pytest.mark.unit
    def test_deduplicates_across_systems(self):
        results = [
            {
                "path": "/doc1.md",
                "title": "Doc 1",
                "snippet": "text",
                "collection": "eng",
            },
        ]
        fn = _make_bm25_fn(results)
        result = pool_candidates(
            "test query",
            ["bm25-equal", "bm25-filepath"],
            search_fns={"bm25-equal": fn, "bm25-filepath": fn},
        )
        assert len(result) == 1
        assert "bm25-equal" in result[0].sources
        assert "bm25-filepath" in result[0].sources

    @pytest.mark.unit
    def test_pools_bm25_and_vector(self):
        bm25_results = [
            {
                "path": "/doc1.md",
                "title": "Doc 1",
                "snippet": "text",
                "collection": "eng",
            },
        ]
        vector_results = [
            {
                "path": "/doc2.md",
                "title": "Doc 2",
                "snippet": "text",
                "collection": "eng",
            },
        ]
        result = pool_candidates(
            "test query",
            ["bm25-equal", "vector"],
            search_fns={
                "bm25-equal": _make_bm25_fn(bm25_results),
                "vector": _make_bm25_fn(vector_results),
            },
        )
        assert len(result) == 2

    @pytest.mark.unit
    def test_unknown_system_skipped(self):
        result = pool_candidates(
            "test query",
            ["bm25-equal", "nosuchsystem"],
            search_fns={"bm25-equal": _make_bm25_fn([])},
        )
        assert isinstance(result, list)

    @pytest.mark.unit
    def test_candidate_fields(self):
        results = [
            {
                "path": "/eng/doc.md",
                "title": "Title",
                "snippet": "Some text",
                "collection": "eng",
            },
        ]
        result = pool_candidates(
            "query",
            ["bm25-equal"],
            search_fns={"bm25-equal": _make_bm25_fn(results)},
        )
        c = result[0]
        assert c.path == "/eng/doc.md"
        assert c.title == "Title"
        assert c.snippet == "Some text"
        assert c.collection == "eng"


# ---------------------------------------------------------------------------
# grade_candidates
# ---------------------------------------------------------------------------


def _make_judge_fn(grades: dict[str, int]) -> object:
    """Return a judge_fn that returns a JudgeResult with fixed grades."""

    def _fn(**kwargs):
        return JudgeResult(
            query=kwargs.get("query", ""),
            grades=grades,
            shuffle_order=list(grades.keys()),
            judge_model="gpt-4o-mini",
        )

    return _fn


class TestGradeCandidates:
    @pytest.mark.unit
    def test_grades_assigned(self):
        # Keys are path_title() output: "/path/doc1.md" -> "path/doc1"
        grades = {"path/doc1": 2, "path/doc2": 1}

        candidates = [
            PooledCandidate(path="/path/doc1.md", title="Doc 1", snippet="text", collection="eng"),
            PooledCandidate(path="/path/doc2.md", title="Doc 2", snippet="text", collection="eng"),
        ]

        result = grade_candidates(
            "query",
            candidates,
            "key",
            "endpoint",
            judge_runs=2,
            judge_fn=_make_judge_fn(grades),
        )
        assert result[0].grade == 2
        assert result[1].grade == 1

    @pytest.mark.unit
    def test_majority_vote(self):
        """Two runs with different grades — majority wins."""
        call_count = [0]

        def _judge_fn(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                grades = {"path/doc1": 2}
            else:
                grades = {"path/doc1": 1}
            return JudgeResult(
                query=kwargs.get("query", ""),
                grades=grades,
                shuffle_order=list(grades.keys()),
                judge_model="gpt-4o-mini",
            )

        candidates = [
            PooledCandidate(path="/path/doc1.md", title="Doc 1", snippet="text", collection="eng"),
        ]
        result = grade_candidates("query", candidates, "key", "endpoint", judge_runs=2, judge_fn=_judge_fn)
        # With 2 runs and different grades, majority vote picks one
        assert result[0].grade in (1, 2)
        assert len(result[0].grade_votes) == 2

    @pytest.mark.unit
    def test_empty_candidates(self):
        result = grade_candidates("query", [], "key", "endpoint")
        assert result == []

    @pytest.mark.unit
    def test_three_runs_majority(self):
        """Three runs — grade 2 appears twice, should win."""
        call_count = [0]

        def _judge_fn(**kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                grades = {"path/doc1": 2}
            else:
                grades = {"path/doc1": 0}
            return JudgeResult(
                query=kwargs.get("query", ""),
                grades=grades,
                shuffle_order=list(grades.keys()),
                judge_model="gpt-4o-mini",
            )

        candidates = [
            PooledCandidate(path="/path/doc1.md", title="Doc 1", snippet="text", collection="eng"),
        ]
        result = grade_candidates("query", candidates, "key", "endpoint", judge_runs=3, judge_fn=_judge_fn)
        assert result[0].grade == 2


# ---------------------------------------------------------------------------
# build_independent_gold (integration-level test with fakes)
# ---------------------------------------------------------------------------


class TestBuildIndependentGold:
    @pytest.mark.unit
    def test_full_build(self, tmp_path):
        from kairix.quality.eval.gold_builder import build_independent_gold

        def fake_bm25(query, collections, limit):
            return [
                {
                    "path": "/eng/relevant.md",
                    "title": "Relevant",
                    "snippet": "Good content",
                    "collection": "eng",
                },
                {
                    "path": "/eng/irrelevant.md",
                    "title": "Irrelevant",
                    "snippet": "Bad content",
                    "collection": "eng",
                },
            ]

        def fake_grade(query, candidates, *args, **kwargs):
            for c in candidates:
                if "relevant" in c.path:
                    c.grade = 2
                    c.grade_votes = [2, 2]
                else:
                    c.grade = 0
                    c.grade_votes = [0, 0]
            return candidates

        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(
            yaml.dump(
                {
                    "cases": [
                        {
                            "query": "test query",
                            "category": "recall",
                            "score_method": "ndcg",
                        },
                    ],
                }
            )
        )

        output_path = tmp_path / "output" / "gold.yaml"
        report = build_independent_gold(
            suite_path,
            output_path,
            systems=["bm25-equal"],
            credentials=("api-key", "https://endpoint", "gpt-4o-mini"),
            search_fns={"bm25-equal": fake_bm25},
            calibrate_fn=lambda *_a: True,
            grade_fn=fake_grade,
        )

        assert report.queries_processed == 1
        assert report.total_candidates_pooled == 2
        assert output_path.exists()

        output = yaml.safe_load(output_path.read_text())
        gold_titles = output["cases"][0]["gold_titles"]
        assert any("relevant" in g["title"] for g in gold_titles)
        assert output["meta"]["gold_method"] == "trec-pooling-llm-judge"

    @pytest.mark.unit
    def test_no_credentials(self, tmp_path):
        from kairix.quality.eval.gold_builder import build_independent_gold

        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(yaml.dump({"cases": [{"query": "q"}]}))

        report = build_independent_gold(suite_path, tmp_path / "out.yaml", credentials=("", "", ""))
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


class TestGradeCandidatesDuplicateStem:
    @pytest.mark.unit
    def test_grade_candidates_distinguishes_same_stem(self):
        """Two candidates with the same filename stem get independent grades."""
        grades = {"a/readme": 2, "b/readme": 0}

        candidates = [
            PooledCandidate(
                path="col/a/readme.md",
                title="A Readme",
                snippet="good",
                collection="col",
            ),
            PooledCandidate(
                path="col/b/readme.md",
                title="B Readme",
                snippet="bad",
                collection="col",
            ),
        ]

        result = grade_candidates(
            "query",
            candidates,
            "key",
            "endpoint",
            judge_runs=1,
            judge_fn=_make_judge_fn(grades),
        )
        assert result[0].grade == 2
        assert result[1].grade == 0

    @pytest.mark.unit
    def test_judge_receivespath_title_keys(self):
        """judge_batch receives path_title() keys, not bare stems."""
        captured_candidates: list = []

        def _capture_judge(**kwargs):
            captured_candidates.extend(kwargs.get("candidates", []))
            return JudgeResult(
                query=kwargs.get("query", ""),
                grades={},
                shuffle_order=[],
                judge_model="gpt-4o-mini",
            )

        candidates = [
            PooledCandidate(
                path="col/sub/readme.md",
                title="Readme",
                snippet="text",
                collection="col",
            ),
        ]
        grade_candidates(
            "query",
            candidates,
            "key",
            "endpoint",
            judge_runs=1,
            judge_fn=_capture_judge,
        )

        # The key should be "sub/readme", not just "readme"
        assert captured_candidates[0][0] == "sub/readme"


# ---------------------------------------------------------------------------
# GoldBuilder class (#143 Phase 2b) — constructor-injected Fakes
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
        # Keys are path_title() output: "/path/doc1.md" -> "path/doc1"
        judge = FakeLLMJudge(grades_by_query={"q1": {"path/doc1": 2, "path/doc2": 1}})
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
        judge = FakeLLMJudge(grades_by_query={"q": {"a/doc": 2}})
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
# _validate_weights — Phase 0b math.isfinite guard
# ---------------------------------------------------------------------------


class TestValidateWeights:
    @pytest.mark.unit
    def test_accepts_finite_positive(self) -> None:
        # No exception
        _validate_weights((1.0, 2.0, 3.0))
        _validate_weights((10.0, 1.0, 1.0))

    @pytest.mark.unit
    def test_rejects_nan(self) -> None:
        with pytest.raises(ValueError, match="must be finite and positive"):
            _validate_weights((float("nan"), 1.0, 1.0))

    @pytest.mark.unit
    def test_rejects_positive_infinity(self) -> None:
        with pytest.raises(ValueError, match="must be finite and positive"):
            _validate_weights((float("inf"), 1.0, 1.0))

    @pytest.mark.unit
    def test_rejects_negative_infinity(self) -> None:
        with pytest.raises(ValueError, match="must be finite and positive"):
            _validate_weights((1.0, float("-inf"), 1.0))

    @pytest.mark.unit
    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="must be finite and positive"):
            _validate_weights((1.0, 1.0, 0.0))

    @pytest.mark.unit
    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="must be finite and positive"):
            _validate_weights((-1.0, 1.0, 1.0))

    @pytest.mark.unit
    def test_message_names_offending_label(self) -> None:
        """Error names which weight position is at fault for diagnosability."""
        with pytest.raises(ValueError, match=r"title="):
            _validate_weights((1.0, float("nan"), 1.0))

    @pytest.mark.unit
    def test_bm25_search_validates_weights_before_db(self) -> None:
        """``_bm25_search_with_weights`` raises before opening DB on bad weight."""
        builder = GoldBuilder()
        with pytest.raises(ValueError, match="must be finite and positive"):
            builder._bm25_search_with_weights("query", (float("nan"), 1.0, 1.0))


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
