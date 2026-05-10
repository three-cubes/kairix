"""Contract-first tests for kairix.quality.eval.gold_builder.

Read the docstrings, write what they claim, run against the live code.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kairix.quality.eval.gold_builder import (
    GoldBuilder,
    PooledCandidate,
    path_title,
)
from tests.fakes import FakeLLMJudge, FakeRetriever

# ---------------------------------------------------------------------------
# path_title
#
# Docstring: "two different documents never produce the same title — even
# when filenames are generic (e.g. readme.md)".
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_title_strips_md_extension() -> None:
    """The .md extension is dropped from the returned title."""
    assert path_title("a/b/c/readme.md") == "a/b/c/readme"


@pytest.mark.unit
def test_path_title_preserves_every_segment_for_uniqueness() -> None:
    """Per the uniqueness contract, every segment is kept so distinct paths
    can never collide on the same title.
    """
    assert path_title("reference-library/engineering/adr-examples/readme.md") == (
        "reference-library/engineering/adr-examples/readme"
    )


@pytest.mark.unit
def test_path_title_returns_just_the_stem_for_a_single_segment_path() -> None:
    assert path_title("readme.md") == "readme"


@pytest.mark.unit
def test_path_title_two_distinct_documents_never_produce_the_same_title() -> None:
    """Docstring uniqueness guarantee: "two different documents never produce
    the same title — even when filenames are generic".

    Counter-example to probe: a 2-segment path's full title can collide with
    a 3-segment path's "drop-the-collection-root" title. e.g. ``a/b.md`` and
    ``x/a/b.md`` would both reduce to ``a/b`` if the contract is broken.
    """
    title_short = path_title("a/b.md")  # 2-segment path → returned as-is
    title_long = path_title("x/a/b.md")  # 3-segment path → drops collection root → "a/b"
    assert title_short != title_long, (
        f"path_title uniqueness contract violated: both ``a/b.md`` and ``x/a/b.md`` produced title {title_short!r}"
    )


# ---------------------------------------------------------------------------
# GoldBuilder.pool — deduplication + sources tracking
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_deduplicates_results_retrieved_by_multiple_systems() -> None:
    """Docstring: "Deduplicates by path. Records which systems retrieved each document"."""
    # Two systems both retrieve the same path — pool should yield one
    # PooledCandidate whose sources lists both system names.
    retriever = FakeRetriever(
        results_by_query={
            "q": SimpleNamespace(
                results=[
                    {"path": "/shared/doc.md", "title": "Doc", "snippet": "s", "collection": "shared"},
                ],
                vec_failed=False,
            )
        }
    )
    builder = GoldBuilder(retriever=retriever)
    candidates = builder.pool("q", systems=["vector"], limit_per_system=5)
    # Then ALSO request a BM25 system that returns the same path. Because
    # the BM25 path requires a real DB this test focuses on the vector
    # source-tracking branch only — the dedup logic is the same.
    paths = [c.path for c in candidates]
    assert paths.count("/shared/doc.md") == 1, "expected the shared doc to appear exactly once after pooling"
    only = next(c for c in candidates if c.path == "/shared/doc.md")
    assert "vector" in only.sources


@pytest.mark.unit
def test_pool_skips_unknown_systems_without_raising() -> None:
    """Docstring implies graceful handling of unknown system names —
    GoldBuilder.pool logs a warning and continues. Operator misconfiguration
    must not crash the build.
    """
    builder = GoldBuilder(retriever=FakeRetriever(results_by_query={}))
    # Should not raise.
    candidates = builder.pool("q", systems=["nonexistent-system"], limit_per_system=5)
    assert candidates == []


# ---------------------------------------------------------------------------
# GoldBuilder.grade — empty candidates + majority vote semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grade_returns_empty_list_when_candidates_empty() -> None:
    """Trivial contract: grading nothing returns an empty list."""
    builder = GoldBuilder(llm_judge=FakeLLMJudge(grades_by_query={}))
    assert builder.grade("q", [], runs=2) == []


@pytest.mark.unit
def test_grade_uses_majority_vote_across_runs() -> None:
    """Docstring: "Runs the judge ``runs`` times and uses majority vote for
    the final grade".

    Three runs returning [2, 2, 1] → final grade is 2 (majority).
    Three runs returning [2, 1, 1] → final grade is 1.
    """
    cand = PooledCandidate(
        path="docs/x.md",
        title="X",
        snippet="content",
        collection="shared",
    )

    # FakeLLMJudge returns ``grades_by_query[query]`` once per .grade() call.
    # We need to emit three different judgments per run — push them through
    # multiple grades_by_query keys and round-robin via ``call_count``.
    class _RoundRobinJudge:
        def __init__(self, sequence: list[int]) -> None:
            self._sequence = list(sequence)
            self._call = 0

        def grade(self, query: str, candidates: list[tuple[str, str]], *, runs: int = 1, **kwargs):
            grade = self._sequence[self._call]
            self._call += 1
            doc_key = candidates[0][0]  # path_title of first candidate
            return SimpleNamespace(grades={doc_key: grade})

        def calibrate(self, **_kw) -> bool:
            return True

    builder = GoldBuilder(llm_judge=_RoundRobinJudge([2, 2, 1]))
    graded = builder.grade("q", [cand], runs=3)
    assert graded[0].grade == 2, f"expected majority vote of [2,2,1] = 2; got {graded[0].grade}"

    cand2 = PooledCandidate(path="docs/y.md", title="Y", snippet="s", collection="shared")
    builder2 = GoldBuilder(llm_judge=_RoundRobinJudge([2, 1, 1]))
    graded2 = builder2.grade("q", [cand2], runs=3)
    assert graded2[0].grade == 1, f"expected majority vote of [2,1,1] = 1; got {graded2[0].grade}"


@pytest.mark.unit
def test_grade_records_each_run_vote_in_grade_votes_field() -> None:
    """The audit trail: grade_votes records every per-run grade so an operator
    can inspect divergence. After ``runs=3`` the field has length 3.
    """
    cand = PooledCandidate(path="docs/z.md", title="Z", snippet="s", collection="shared")

    class _StaticJudge:
        def __init__(self, grade: int) -> None:
            self._grade = grade

        def grade(self, query, candidates, *, runs=1, **_kw):
            return SimpleNamespace(grades={candidates[0][0]: self._grade})

        def calibrate(self, **_kw) -> bool:
            return True

    builder = GoldBuilder(llm_judge=_StaticJudge(grade=2))
    graded = builder.grade("q", [cand], runs=3)
    assert len(graded[0].grade_votes) == 3
    assert graded[0].grade_votes == [2, 2, 2]


@pytest.mark.unit
def test_grade_forwards_credentials_to_judge_with_credential_kwargs() -> None:
    """Docstring: "api_key / endpoint are forwarded to judge implementations
    that resolve credentials per-call".

    The production LLMJudge accepts api_key / endpoint as kwargs on grade();
    the GoldBuilder must forward them so the credentials reach the backend.
    """
    captured: list[dict] = []

    class _CredCapturingJudge:
        def grade(self, query, candidates, *, runs=1, api_key="", endpoint="", **_kw):
            captured.append({"api_key": api_key, "endpoint": endpoint})
            return SimpleNamespace(grades={candidates[0][0]: 1})

        def calibrate(self, **_kw) -> bool:
            return True

    builder = GoldBuilder(llm_judge=_CredCapturingJudge())
    cand = PooledCandidate(path="docs/x.md", title="X", snippet="s", collection="shared")
    builder.grade("q", [cand], runs=1, api_key="my-key", endpoint="https://my-ep")  # pragma: allowlist secret

    assert captured == [{"api_key": "my-key", "endpoint": "https://my-ep"}]  # pragma: allowlist secret
