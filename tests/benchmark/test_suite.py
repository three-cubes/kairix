"""
Tests for kairix.quality.benchmark.suite and kairix.quality.benchmark.runner.

Covers:
  - load_suite(): valid YAML loads correctly
  - validate_suite(): missing gold path returns error string
  - validate_suite(): duplicate gold paths returns error string
  - exact-method scoring driven through ``run_benchmark`` (case-insensitive,
    suffix shortening, top-5 cutoff, empty gold/paths)
  - run_benchmark(): DI retrieve_fn, correct weighted total calculation
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkDeps, BenchmarkResult, run_benchmark
from kairix.quality.benchmark.suite import (
    BenchmarkCase,
    BenchmarkSuite,
    load_suite,
    validate_suite,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_suite_yaml(tmp_path: Path) -> Path:
    """Create a minimal valid suite YAML file."""
    content = textwrap.dedent("""\
        meta:
          agent: test-agent
          collections:
            - vault
          version: "1.0"
          created: "2026-03-23"

        cases:
          - id: R01
            category: recall
            query: "Arize Phoenix observability"
            gold_path: "01-projects/arize/report.md"
            score_method: exact
            notes: "Test recall case"

          - id: C01
            category: conceptual
            query: "what is the memory architecture"
            gold_path: null
            score_method: llm
            notes: null
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB mimicking the kairix documents table."""
    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            collection TEXT,
            path TEXT,
            title TEXT,
            hash TEXT,
            created_at TEXT,
            modified_at TEXT,
            active INTEGER DEFAULT 1
        )
        """)
    # Insert some test documents
    db.executemany(
        "INSERT INTO documents (collection, path, title, active) VALUES (?, ?, ?, 1)",
        [
            ("vault", "01-projects/arize/report.md", "Arize Report"),
            ("vault", "04-agent-knowledge/builder/rules.md", "Builder Rules"),
            (
                "vault",
                "01-projects/kairix-platform/architecture.md",
                "Kairix Architecture",
            ),
        ],
    )
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Fake retrieve helper
# ---------------------------------------------------------------------------


def _mock_retrieve_result(
    paths: list[str],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Build a fake retrieve return value: (paths, snippets, metadata)."""
    snippets = ["snippet"] * len(paths)
    meta = {
        "intent": "semantic",
        "bm25_count": len(paths),
        "vec_count": len(paths),
        "fused_count": len(paths),
        "vec_failed": False,
        "latency_ms": 50.0,
    }
    return paths, snippets, meta


def _make_retrieve_fn(
    results_by_call: list[tuple[list[str], list[str], dict[str, Any]]],
) -> object:
    """Return a retrieve_fn that returns successive results from the list."""
    call_idx = [0]

    def _fn(**kwargs: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        idx = min(call_idx[0], len(results_by_call) - 1)
        call_idx[0] += 1
        return results_by_call[idx]

    return _fn


# ---------------------------------------------------------------------------
# load_suite() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_suite_valid_yaml_loads_correctly(minimal_suite_yaml: Path) -> None:
    """Valid YAML file loads into BenchmarkSuite with correct structure."""
    suite = load_suite(str(minimal_suite_yaml))

    assert isinstance(suite, BenchmarkSuite)
    assert suite.meta["agent"] == "test-agent"
    assert suite.meta["collections"] == ["vault"]
    assert len(suite.cases) == 2

    r01 = suite.cases[0]
    assert isinstance(r01, BenchmarkCase)
    assert r01.id == "R01"
    assert r01.category == "recall"
    assert r01.query == "Arize Phoenix observability"
    assert r01.gold_path == "01-projects/arize/report.md"
    assert r01.score_method == "exact"
    assert r01.notes == "Test recall case"

    c01 = suite.cases[1]
    assert c01.id == "C01"
    assert c01.category == "conceptual"
    assert c01.gold_path is None
    assert c01.score_method == "llm"
    assert c01.notes is None


@pytest.mark.unit
def test_load_suite_missing_file_raises_file_not_found() -> None:
    """Missing file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_suite("/nonexistent/path/suite.yaml")


@pytest.mark.unit
def test_load_suite_invalid_yaml_raises_value_error(tmp_path: Path) -> None:
    """Invalid YAML raises ValueError."""
    p = tmp_path / "bad.yaml"
    p.write_text("cases: [{{invalid")
    with pytest.raises(ValueError, match="YAML parse error"):
        load_suite(str(p))


@pytest.mark.unit
def test_load_suite_missing_required_field_raises_value_error(tmp_path: Path) -> None:
    """Missing required field raises ValueError with details."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: R01
            category: recall
            # query is missing
            gold_path: "some/path.md"
            score_method: exact
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    with pytest.raises(ValueError, match="query"):
        load_suite(str(p))


@pytest.mark.unit
def test_load_suite_invalid_category_raises_value_error(tmp_path: Path) -> None:
    """Invalid category raises ValueError."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: X01
            category: invalid_category
            query: "test query"
            gold_path: null
            score_method: llm
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    with pytest.raises(ValueError, match="invalid_category"):
        load_suite(str(p))


@pytest.mark.unit
def test_load_suite_all_categories_accepted(tmp_path: Path) -> None:
    """All valid categories are accepted."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: R01
            category: recall
            query: "test recall"
            gold_path: "some/path.md"
            score_method: exact
          - id: T01
            category: temporal
            query: "test temporal"
            gold_path: null
            score_method: llm
          - id: E01
            category: entity
            query: "test entity"
            gold_path: null
            score_method: llm
          - id: C01
            category: conceptual
            query: "test conceptual"
            gold_path: null
            score_method: llm
          - id: M01
            category: multi_hop
            query: "test multi_hop"
            gold_path: null
            score_method: llm
          - id: P01
            category: procedural
            query: "test procedural"
            gold_path: null
            score_method: llm
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))
    assert len(suite.cases) == 6
    categories = [c.category for c in suite.cases]
    assert "recall" in categories
    assert "temporal" in categories
    assert "multi_hop" in categories


# ---------------------------------------------------------------------------
# validate_suite() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_suite_all_valid_returns_empty_list(
    minimal_suite_yaml: Path,
    in_memory_db: sqlite3.Connection,
) -> None:
    """Valid suite with all gold paths in the index returns empty errors."""
    suite = load_suite(str(minimal_suite_yaml))
    errors = validate_suite(suite, in_memory_db)
    assert errors == []


@pytest.mark.unit
def test_validate_suite_missing_gold_path_returns_error(
    in_memory_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Missing gold path returns an error string describing the problem."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: R01
            category: recall
            query: "something specific"
            gold_path: "path/that/does/not/exist.md"
            score_method: exact
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))

    errors = validate_suite(suite, in_memory_db)

    assert len(errors) >= 1
    assert any("R01" in e for e in errors)
    assert any("path/that/does/not/exist.md" in e for e in errors)
    assert any("not found" in e.lower() for e in errors)


@pytest.mark.unit
def test_validate_suite_duplicate_gold_paths_returns_error(
    in_memory_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Two cases with the same gold path return an error string."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: R01
            category: recall
            query: "first query about arize"
            gold_path: "01-projects/arize/report.md"
            score_method: exact
          - id: R02
            category: recall
            query: "second query about arize"
            gold_path: "01-projects/arize/report.md"
            score_method: exact
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))

    errors = validate_suite(suite, in_memory_db)

    # Should have at least one error mentioning duplicate
    assert len(errors) >= 1
    assert any("duplicate" in e.lower() or ("R01" in e and "R02" in e) for e in errors), (
        f"Expected duplicate error, got: {errors}"
    )


@pytest.mark.unit
def test_validate_suite_non_recall_cases_not_validated(
    in_memory_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Non-recall cases without gold paths do not generate errors."""
    content = textwrap.dedent("""\
        meta:
          agent: test
        cases:
          - id: T01
            category: temporal
            query: "what happened last week"
            gold_path: null
            score_method: llm
          - id: C01
            category: conceptual
            query: "what is the architecture"
            gold_path: null
            score_method: llm
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))

    errors = validate_suite(suite, in_memory_db)
    assert errors == []


# ---------------------------------------------------------------------------
# Exact-method scoring driven through run_benchmark
#
# Each test builds a single-case suite with score_method="exact" and a fixed
# retrieve_fn output, then asserts on result.cases[0]["score"]. This drives
# the same code path that production runs hit, instead of importing the
# scoring helper directly.
# ---------------------------------------------------------------------------


def _exact_case(case_id: str, gold_path: str) -> BenchmarkCase:
    return BenchmarkCase(
        id=case_id,
        category="recall",
        query="q",
        gold_path=gold_path,
        score_method="exact",
    )


def _run_single_exact_case(gold_path: str, retrieved_paths: list[str]) -> float:
    """Run a one-case exact-method benchmark and return the raw score."""
    suite = BenchmarkSuite(
        meta={"agent": "test", "collections": ["vault"]},
        cases=[_exact_case("R01", gold_path)],
    )
    result = run_benchmark(
        suite,
        system="hybrid",
        agent="test",
        deps=BenchmarkDeps(retrieve=_make_retrieve_fn([_mock_retrieve_result(retrieved_paths)])),
    )
    return float(result.cases[0]["score"])


@pytest.mark.unit
def test_exact_score_is_1_when_gold_path_substring_appears_in_top5_results() -> None:
    """Gold appears as case-insensitive substring of a top-5 result → score 1.0."""
    score = _run_single_exact_case(
        gold_path="01-Projects/Arize/Research-Report.md",
        retrieved_paths=[
            "vault/01-projects/arize/research-report.md",
            "vault/02-areas/foo/bar.md",
            "vault/04-knowledge/rules.md",
        ],
    )
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_score_match_is_case_insensitive() -> None:
    """An all-uppercase result still matches a lower-case gold."""
    score = _run_single_exact_case(
        gold_path="path/to/doc.md",
        retrieved_paths=["vault/PATH/TO/DOC.MD"],
    )
    assert score == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_score_is_0_when_gold_path_does_not_appear() -> None:
    """No retrieved path contains the gold path → score 0.0."""
    score = _run_single_exact_case(
        gold_path="01-projects/totally-different/report.md",
        retrieved_paths=[
            "vault/01-projects/something-else/doc.md",
            "vault/02-areas/foo/bar.md",
        ],
    )
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_score_only_considers_top5_results() -> None:
    """A match at position 6 does NOT contribute — exact method has a top-5 cutoff."""
    score = _run_single_exact_case(
        gold_path="unique-report-xyz-9999.md",
        retrieved_paths=[
            "vault/doc1.md",
            "vault/doc2.md",
            "vault/doc3.md",
            "vault/doc4.md",
            "vault/doc5.md",
            "vault/unique-report-xyz-9999.md",  # position 6 — outside the top-5 window
        ],
    )
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_score_is_0_when_retriever_returns_empty_paths() -> None:
    """Empty retrieved paths → score 0.0."""
    score = _run_single_exact_case(gold_path="some/path.md", retrieved_paths=[])
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_score_is_0_when_case_gold_path_is_empty_string() -> None:
    """A misconfigured case with gold_path="" still scores 0 (never raises)."""
    score = _run_single_exact_case(gold_path="", retrieved_paths=["vault/some/path.md"])
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# run_benchmark() tests — DI retrieve_fn
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_benchmark_mocked_retrieval_correct_scores() -> None:
    """
    run_benchmark with fake retrieve_fn returns correct scores.

    R01 gold path appears in results -> score=1.0
    R02 gold path does NOT appear -> score=0.0
    Recall category score: (1.0 + 0.0) / 2 = 0.5
    """
    suite = BenchmarkSuite(
        meta={"agent": "test", "collections": ["vault"]},
        cases=[
            BenchmarkCase(
                id="R01",
                category="recall",
                query="Arize Phoenix observability",
                gold_path="01-projects/arize/report.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="R02",
                category="recall",
                query="Kairix architecture",
                gold_path="01-projects/kairix-platform/architecture.md",
                score_method="exact",
            ),
        ],
    )

    r01_paths = ["vault/01-projects/arize/report.md", "vault/other.md"]
    r02_paths = ["vault/something-else.md"]

    retrieve_results = [
        _mock_retrieve_result(r01_paths),
        _mock_retrieve_result(r02_paths),
    ]

    result = run_benchmark(
        suite,
        system="hybrid",
        agent="test",
        deps=BenchmarkDeps(retrieve=_make_retrieve_fn(retrieve_results)),
    )

    assert isinstance(result, BenchmarkResult)
    assert len(result.cases) == 2

    r01_case = next(c for c in result.cases if c["id"] == "R01")
    r02_case = next(c for c in result.cases if c["id"] == "R02")

    assert r01_case["score"] == pytest.approx(1.0)
    assert r02_case["score"] == pytest.approx(0.0)

    # Recall category score: (1.0 + 0.0) / 2 = 0.5
    assert result.summary["category_scores"]["recall"] == pytest.approx(0.5)
    # weighted_total = recall_weight * recall_score = 0.25 * 0.5 = 0.125
    # (other categories score 0.0, contributing 0.0 to the weighted sum)
    assert result.summary["weighted_total"] == pytest.approx(0.125)


@pytest.mark.unit
def test_run_benchmark_weighted_total_calculation() -> None:
    """
    Weighted total is correctly computed from per-category scores.

    recall=1.0 (weight 0.25), all others=0.0
    weighted_total = 0.25 * 1.0 + 0.20 * 0.0 + ... = 0.25
    """
    suite = BenchmarkSuite(
        meta={"agent": "test", "collections": ["vault"]},
        cases=[
            BenchmarkCase(
                id="R01",
                category="recall",
                query="q1",
                gold_path="p1.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="T01",
                category="temporal",
                query="q2",
                gold_path=None,
                score_method="llm",
            ),
            BenchmarkCase(
                id="E01",
                category="entity",
                query="q3",
                gold_path=None,
                score_method="llm",
            ),
            BenchmarkCase(
                id="C01",
                category="conceptual",
                query="q4",
                gold_path=None,
                score_method="llm",
            ),
            BenchmarkCase(
                id="M01",
                category="multi_hop",
                query="q5",
                gold_path=None,
                score_method="llm",
            ),
            BenchmarkCase(
                id="P01",
                category="procedural",
                query="q6",
                gold_path=None,
                score_method="llm",
            ),
        ],
    )

    # R01: gold path p1.md appears in results -> score=1.0
    # All others: empty paths -> _llm_judge with no chat_fn defaults to 0.0 (no Azure creds)
    retrieve_results = [
        _mock_retrieve_result(["p1.md"]),  # R01 — exact match hits
        _mock_retrieve_result([]),  # T01
        _mock_retrieve_result([]),  # E01
        _mock_retrieve_result([]),  # C01
        _mock_retrieve_result([]),  # M01
        _mock_retrieve_result([]),  # P01
    ]

    result = run_benchmark(
        suite,
        system="hybrid",
        agent="test",
        deps=BenchmarkDeps(retrieve=_make_retrieve_fn(retrieve_results)),
    )

    assert result.summary["category_scores"]["recall"] == pytest.approx(1.0)
    assert result.summary["category_scores"]["temporal"] == pytest.approx(0.0)
    assert result.summary["category_scores"]["entity"] == pytest.approx(0.0)
    # weighted_total = 0.25*1.0 + 0.20*0.0 + 0.20*0.0 + 0.15*0.0 + 0.10*0.0 + 0.10*0.0 = 0.25
    assert result.summary["weighted_total"] == pytest.approx(0.25)


@pytest.mark.unit
def test_run_benchmark_all_scores_1_gives_weighted_total_1() -> None:
    """When all cases score 1.0, weighted total is 1.0."""
    # Use all exact score_method cases so scoring works without Azure creds.
    suite_exact = BenchmarkSuite(
        meta={"agent": "test", "collections": ["vault"]},
        cases=[
            BenchmarkCase(
                id="R01",
                category="recall",
                query="q1",
                gold_path="path/to/doc.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="T01",
                category="temporal",
                query="q2",
                gold_path="path/to/temporal.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="E01",
                category="entity",
                query="q3",
                gold_path="path/to/entity.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="C01",
                category="conceptual",
                query="q4",
                gold_path="path/to/concept.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="M01",
                category="multi_hop",
                query="q5",
                gold_path="path/to/multi.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="P01",
                category="procedural",
                query="q6",
                gold_path="path/to/proc.md",
                score_method="exact",
            ),
        ],
    )

    retrieve_results = [
        _mock_retrieve_result(["path/to/doc.md"]),
        _mock_retrieve_result(["path/to/temporal.md"]),
        _mock_retrieve_result(["path/to/entity.md"]),
        _mock_retrieve_result(["path/to/concept.md"]),
        _mock_retrieve_result(["path/to/multi.md"]),
        _mock_retrieve_result(["path/to/proc.md"]),
    ]

    result = run_benchmark(
        suite_exact,
        system="hybrid",
        agent="test",
        deps=BenchmarkDeps(retrieve=_make_retrieve_fn(retrieve_results)),
    )

    assert result.summary["category_scores"]["recall"] == pytest.approx(1.0)
    assert result.summary["category_scores"]["temporal"] == pytest.approx(1.0)
    # All 6 categories = 1.0, all weights sum to 1.0 -> weighted_total = 1.0
    assert result.summary["weighted_total"] == pytest.approx(1.0)


@pytest.mark.unit
def test_run_benchmark_saves_json_to_output_dir(tmp_path: Path) -> None:
    """Output dir is created and JSON file is saved."""
    suite = BenchmarkSuite(
        meta={"agent": "test", "name": "test-suite", "collections": ["vault"]},
        cases=[
            BenchmarkCase(
                id="R01",
                category="recall",
                query="q1",
                gold_path="p.md",
                score_method="exact",
            ),
        ],
    )
    output_dir = str(tmp_path / "results")

    run_benchmark(
        suite,
        system="bm25",
        agent="test",
        output_dir=output_dir,
        deps=BenchmarkDeps(retrieve=_make_retrieve_fn([_mock_retrieve_result([])])),
    )

    json_files = list(Path(output_dir).glob("*.json"))
    assert len(json_files) == 1

    with open(json_files[0]) as f:
        saved = json.load(f)

    assert "meta" in saved
    assert "summary" in saved
    assert "diagnostics" in saved
    assert "cases" in saved
    assert saved["meta"]["system"] == "bm25"


# ---------------------------------------------------------------------------
# gold_titles — suite loading and validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_suite_parses_gold_titles(tmp_path: Path) -> None:
    """gold_titles field is parsed into BenchmarkCase.gold_titles list."""
    content = textwrap.dedent("""\
        meta:
          name: test
          version: "1.0"
        cases:
          - id: E01
            category: entity
            query: "who is Jordan Blake"
            score_method: ndcg
            gold_titles:
              - title: jordan-blake
                relevance: 2
              - title: team-overview
                relevance: 1
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))
    assert len(suite.cases) == 1
    case = suite.cases[0]
    assert case.gold_titles is not None
    assert len(case.gold_titles) == 2
    assert case.gold_titles[0]["title"] == "jordan-blake"
    assert case.gold_titles[0]["relevance"] == 2


@pytest.mark.unit
def test_gold_title_field_parsed(tmp_path: Path) -> None:
    """gold_title (single) is parsed for exact/fuzzy cases."""
    content = textwrap.dedent("""\
        meta:
          name: test
          version: "1.0"
        cases:
          - id: R01
            category: recall
            query: "engineering patterns"
            score_method: exact
            gold_title: patterns
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))
    assert suite.cases[0].gold_title == "patterns"
    # gold_path derived from gold_title for backwards compat
    assert suite.cases[0].gold_path == "patterns"


@pytest.mark.unit
def test_gold_title_validated_requires_title_and_relevance(tmp_path: Path) -> None:
    """A gold_titles entry missing 'title' raises ValueError."""
    content = textwrap.dedent("""\
        meta:
          name: test
          version: "1.0"
        cases:
          - id: E01
            category: entity
            query: "who is Jordan Blake"
            score_method: ndcg
            gold_titles:
              - relevance: 2
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    with pytest.raises(ValueError, match="title"):
        load_suite(str(p))


@pytest.mark.unit
def test_duplicate_gold_titles_detected(tmp_path: Path, in_memory_db: sqlite3.Connection) -> None:
    """Same gold_title used in two recall cases -> validation error."""
    suite = BenchmarkSuite(
        meta={"name": "test", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="R01",
                category="recall",
                query="q1",
                gold_path="patterns",
                score_method="exact",
                gold_title="patterns",
            ),
            BenchmarkCase(
                id="R02",
                category="recall",
                query="q2",
                gold_path="patterns",
                score_method="exact",
                gold_title="patterns",
            ),
        ],
    )
    errors = validate_suite(suite, in_memory_db)
    assert any("Duplicate gold_title" in e for e in errors)


@pytest.mark.unit
def test_gold_titles_highest_relevance_derives_gold_path(tmp_path: Path) -> None:
    """gold_path auto-derived from the highest-relevance gold_titles entry."""
    content = textwrap.dedent("""\
        meta:
          name: test
          version: "1.0"
        cases:
          - id: M01
            category: multi_hop
            query: "projects and their status"
            score_method: ndcg
            gold_titles:
              - title: status
                relevance: 1
              - title: projects
                relevance: 2
        """)
    p = tmp_path / "suite.yaml"
    p.write_text(content)
    suite = load_suite(str(p))
    # gold_path derived from the relevance-2 entry
    assert suite.cases[0].gold_path == "projects"


@pytest.mark.unit
def test_gold_titles_with_unquoted_iso_date_coerces_to_str(tmp_path: Path) -> None:
    """Regression for #103. PyYAML parses unquoted ISO date titles as datetime.date,
    which crashes downstream scoring (str.endswith on a date object).

    The suite loader must coerce title/path refs to str at the boundary."""
    import textwrap

    from kairix.quality.benchmark.suite import load_suite

    content = textwrap.dedent("""\
        meta:
          name: dated
          version: "1.0"
        cases:
          - id: D01
            category: recall
            query: "what happened on this day"
            score_method: ndcg
            gold_titles:
              - title: 2026-04-07
                relevance: 2
              - title: 2026-04-08
                relevance: 1
    """)
    p = tmp_path / "dated-suite.yaml"
    p.write_text(content)

    suite = load_suite(str(p))
    case = suite.cases[0]
    assert case.gold_titles is not None
    assert all(isinstance(g["title"], str) for g in case.gold_titles)
    assert case.gold_titles[0]["title"] == "2026-04-07"


@pytest.mark.unit
def test_gold_paths_with_unquoted_iso_date_coerces_to_str(tmp_path: Path) -> None:
    """Regression for #103. Same coercion guarantee for gold_paths."""
    import textwrap

    from kairix.quality.benchmark.suite import load_suite

    content = textwrap.dedent("""\
        meta:
          name: dated
          version: "1.0"
        cases:
          - id: D02
            category: recall
            query: "what happened on this day"
            score_method: ndcg
            gold_paths:
              - path: 2026-04-07
                relevance: 2
    """)
    p = tmp_path / "dated-suite.yaml"
    p.write_text(content)

    suite = load_suite(str(p))
    case = suite.cases[0]
    assert case.gold_paths is not None
    assert all(isinstance(g["path"], str) for g in case.gold_paths)


@pytest.mark.unit
def test_all_bundled_suites_load_without_errors() -> None:
    """Every YAML in suites/ must load via load_suite — guards against the
    #104 footgun where a bundled suite fails schema validation only at runtime.

    Adds a CI gate that catches missing gold fields, type errors, and other
    structural issues at load-time so first-run quick-start can never break
    on a shipped suite.
    """
    from pathlib import Path

    from kairix.quality.benchmark.suite import load_suite

    repo_root = Path(__file__).resolve().parents[2]
    suites_dir = repo_root / "suites"
    assert suites_dir.is_dir(), f"expected bundled suites dir at {suites_dir}"

    yaml_files = sorted(suites_dir.glob("*.yaml")) + sorted(suites_dir.glob("*.yml"))
    assert yaml_files, "no bundled suites found — has the suites/ dir moved?"

    failures: list[str] = []
    for suite_path in yaml_files:
        try:
            suite = load_suite(str(suite_path))
            assert suite.cases, f"{suite_path.name} loaded with zero cases"
        except Exception as exc:
            failures.append(f"{suite_path.name}: {type(exc).__name__}: {exc}")

    assert not failures, "Bundled suites failed to load:\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# Branch-coverage tests for previously-uncovered validation paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_suite_raises_when_yaml_root_is_not_a_mapping(tmp_path: Path) -> None:
    """A YAML file whose root is a list is rejected with a clear error (line 63)."""
    bad = tmp_path / "list-root.yaml"
    bad.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_suite(str(bad))


@pytest.mark.unit
def test_load_suite_raises_when_meta_is_not_a_mapping(tmp_path: Path) -> None:
    """A non-mapping ``meta`` value is rejected with a clear error (line 72)."""
    bad = tmp_path / "bad-meta.yaml"
    bad.write_text("meta: not-a-mapping\ncases: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'meta' must be a mapping"):
        load_suite(str(bad))


@pytest.mark.unit
def test_load_suite_raises_when_cases_is_not_a_list(tmp_path: Path) -> None:
    """A non-list ``cases`` value is rejected with a clear error (line 76)."""
    bad = tmp_path / "bad-cases.yaml"
    bad.write_text("meta: {}\ncases: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'cases' must be a list"):
        load_suite(str(bad))


@pytest.mark.unit
def test_load_suite_collects_every_per_case_required_field_error(tmp_path: Path) -> None:
    """A case missing id/category/score_method aggregates each error (lines 84, 88, 99, 101)."""
    bad = tmp_path / "missing-fields.yaml"
    bad.write_text(
        "meta: {}\n"
        "cases:\n"
        "  - query: q1\n"  # missing id, category, score_method
        "  - id: c2\n"
        "    category: not-a-real-category\n"  # invalid category
        "    query: q2\n"
        "    score_method: also-bogus\n"  # invalid score_method
        "    gold_path: x.md\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_suite(str(bad))
    msg = str(exc_info.value)
    assert "missing required field 'id'" in msg
    assert "missing required field 'category'" in msg
    assert "missing required field 'score_method'" in msg
    assert "invalid score_method" in msg


@pytest.mark.unit
def test_load_suite_aggregates_gold_titles_validation_errors(tmp_path: Path) -> None:
    """Gold-titles entries with various defects produce specific errors (lines 116, 120, 122)."""
    bad = tmp_path / "bad-gold.yaml"
    bad.write_text(
        "meta: {}\n"
        "cases:\n"
        "  - id: c1\n"
        "    category: recall\n"
        "    query: q\n"
        "    score_method: ndcg\n"
        "    gold_titles:\n"
        "      - not-a-mapping\n"  # gold_titles[0] must be a mapping
        "      - title: ok-title\n"  # gold_titles[1] missing relevance
        "      - title: another\n"
        "        relevance: 5\n",  # gold_titles[2] relevance must be 0/1/2
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_suite(str(bad))
    msg = str(exc_info.value)
    assert "gold_titles[0] must be a mapping" in msg
    assert "gold_titles[1] missing required field 'relevance'" in msg
    assert "gold_titles[2] relevance must be 0, 1, or 2" in msg


@pytest.mark.unit
def test_load_suite_recall_case_without_any_gold_reference_is_rejected(tmp_path: Path) -> None:
    """Recall case missing every gold-reference variant produces a single targeted error (lines 137-138)."""
    bad = tmp_path / "no-gold.yaml"
    bad.write_text(
        "meta: {}\ncases:\n  - id: c1\n    category: recall\n    query: q\n    score_method: ndcg\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="recall cases must have gold_path, gold_title"):
        load_suite(str(bad))


@pytest.mark.unit
def test_load_suite_non_dict_case_entry_is_rejected(tmp_path: Path) -> None:
    """A case that's a string (not a mapping) emits a specific error (lines 247-248)."""
    bad = tmp_path / "string-case.yaml"
    bad.write_text(
        "meta: {}\n"
        "cases:\n"
        "  - just-a-string\n"
        "  - id: c2\n"
        "    category: recall\n"
        "    query: q\n"
        "    score_method: ndcg\n"
        "    gold_path: x.md\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        load_suite(str(bad))


@pytest.mark.unit
def test_derive_gold_path_uses_highest_relevance_gold_paths_entry(tmp_path: Path) -> None:
    """When ``gold_paths`` has mixed relevance, the derived gold_path picks the highest entry (lines 156-158)."""
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        "meta: {}\n"
        "cases:\n"
        "  - id: c1\n"
        "    category: recall\n"
        "    query: q\n"
        "    score_method: ndcg\n"
        "    gold_paths:\n"
        "      - path: low.md\n"
        "        relevance: 1\n"
        "      - path: highest.md\n"
        "        relevance: 2\n"
        "      - path: zero.md\n"
        "        relevance: 0\n",
        encoding="utf-8",
    )
    suite = load_suite(str(suite_path))
    assert len(suite.cases) == 1
    # gold_path was derived from gold_paths' highest-relevance entry.
    assert suite.cases[0].gold_path == "highest.md"


@pytest.mark.unit
def test_load_suite_coerces_date_object_in_gold_paths_to_string(tmp_path: Path) -> None:
    """A non-dict scalar inside gold_paths passes through verbatim (lines 182-183)."""
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        "meta: {}\n"
        "cases:\n"
        "  - id: c1\n"
        "    category: recall\n"
        "    query: q\n"
        "    score_method: ndcg\n"
        "    gold_paths:\n"
        "      - path: 2026-04-07\n"  # unquoted ISO date — PyYAML parses as datetime.date
        "        relevance: 2\n"
        "      - just-a-string\n",  # non-dict — passes through unchanged
        encoding="utf-8",
    )
    suite = load_suite(str(suite_path))
    case = suite.cases[0]
    # Dict item's path field coerced to str.
    assert case.gold_paths is not None
    assert isinstance(case.gold_paths[0]["path"], str)
    assert case.gold_paths[0]["path"] == "2026-04-07"
    # Non-dict item passed through verbatim.
    assert case.gold_paths[1] == "just-a-string"


@pytest.mark.unit
def test_validate_suite_matches_gold_paths_via_progressive_suffix_shortening() -> None:
    """validate_suite reports an error iff the gold path's filename has no
    matching suffix in the documents table.

    Drives the suffix-shortening loop in suite._gold_path_in_index through
    validate_suite. Four single-case suites — three with shortenable matches
    against ``engineering/patterns/ralph-loop.md`` (full / leading-prefix /
    filename-only) all validate clean; the fourth, with no suffix match,
    surfaces as one error.
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT)")
    db.execute("INSERT INTO documents VALUES ('engineering/patterns/ralph-loop.md')")
    db.commit()

    def _suite_with(gold: str) -> BenchmarkSuite:
        return BenchmarkSuite(
            meta={"agent": "test", "collections": ["vault"]},
            cases=[_exact_case("R01", gold)],
        )

    # Full match: gold == indexed path → no error.
    assert validate_suite(_suite_with("engineering/patterns/ralph-loop.md"), db) == []
    # Leading prefix on the gold path → suffix shortening matches → no error.
    assert validate_suite(_suite_with("alt-prefix/engineering/patterns/ralph-loop.md"), db) == []
    # Different middle directory but same filename → final loop step matches → no error.
    assert validate_suite(_suite_with("different/dir/ralph-loop.md"), db) == []

    # Completely unrelated filename → no suffix matches → exactly one error string,
    # naming the case and the gold path.
    errors = validate_suite(_suite_with("completely/unrelated.md"), db)
    assert len(errors) == 1
    assert "R01" in errors[0]
    assert "completely/unrelated.md" in errors[0]
