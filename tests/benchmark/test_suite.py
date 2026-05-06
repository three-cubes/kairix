"""
Tests for kairix.quality.benchmark.suite and kairix.quality.benchmark.runner.

Covers:
  - load_suite(): valid YAML loads correctly
  - validate_suite(): missing gold path returns error string
  - validate_suite(): duplicate gold paths returns error string
  - _exact_match(): case-insensitive substring match
  - run_benchmark(): DI retrieve_fn, correct weighted total calculation
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkResult, _exact_match, run_benchmark
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
# _exact_match() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_match_returns_1_when_gold_in_top5() -> None:
    """Returns 1.0 when gold path appears in top-5 results (case-insensitive)."""
    paths = [
        "vault/01-projects/arize/research-report.md",
        "vault/02-areas/foo/bar.md",
        "vault/04-knowledge/rules.md",
    ]
    gold = "01-Projects/Arize/Research-Report.md"
    assert _exact_match(paths, gold) == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_match_case_insensitive() -> None:
    """Match is case-insensitive."""
    paths = ["vault/PATH/TO/DOC.MD"]
    gold = "path/to/doc.md"
    assert _exact_match(paths, gold) == pytest.approx(1.0)


@pytest.mark.unit
def test_exact_match_returns_0_when_gold_not_in_paths() -> None:
    """Returns 0.0 when gold path is not in the retrieved paths."""
    paths = [
        "vault/01-projects/something-else/doc.md",
        "vault/02-areas/foo/bar.md",
    ]
    gold = "01-projects/totally-different/report.md"
    assert _exact_match(paths, gold) == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_only_checks_top5() -> None:
    """Only checks the top 5 results, not beyond."""
    paths = [
        "vault/doc1.md",
        "vault/doc2.md",
        "vault/doc3.md",
        "vault/doc4.md",
        "vault/doc5.md",
        "vault/unique-report-xyz-9999.md",  # position 6 — should NOT match
    ]
    gold = "unique-report-xyz-9999.md"
    assert _exact_match(paths, gold) == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_empty_paths_returns_0() -> None:
    """Empty paths list returns 0.0."""
    assert _exact_match([], "some/path.md") == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_empty_gold_returns_0() -> None:
    """Empty gold path returns 0.0."""
    assert _exact_match(["some/path.md"], "") == pytest.approx(0.0)


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
        retrieve_fn=_make_retrieve_fn(retrieve_results),
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
        retrieve_fn=_make_retrieve_fn(retrieve_results),
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
        retrieve_fn=_make_retrieve_fn(retrieve_results),
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
        retrieve_fn=_make_retrieve_fn([_mock_retrieve_result([])]),
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
    errors = validate_suite(suite, in_memory_db, strict=True)
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
