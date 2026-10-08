"""Unit tests for :mod:`kairix.quality.eval.suite_runner`.

Every test is sabotage-proven (mutate prod → fail → restore → pass).
Tests drive :class:`SuiteRunner` end-to-end with fakes from
``tests/fakes.py`` — no monkeypatching, no internal-attribute
reassignment (F1 clean).

Coverage:

- ``discover_suite`` finds session-NNN.jsonl + ground-truth files
- ``discover_suite`` raises actionable ValueError on missing files
- ``discover_suite`` tolerates missing ``ground-truth-facts.json``
- ``run`` dispatches each session through the ingest path
- ``run`` scores each query, emits per-category breakdown
- ``run`` computes extractor F1 against ground truth
- ``run`` tolerates ground-truth-facts.json absent (skip F1)
- Judge failures (backend error, malformed / non-finite reply) are
  recorded and excluded from aggregates, never scored 0.0
- F1 calculation is correct (precision/recall/F1)
- Substring matching on values is case-insensitive
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kairix.paths import KairixPaths
from kairix.quality.eval.suite_runner import SuiteResult, SuiteRunner, SuiteSpec
from tests.fakes import (
    FakeFactExtractor,
    FakeFactRecord,
    FakeFactStore,
    FakeLLMBackend,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paths(tmp_path: Path) -> KairixPaths:
    """Construct a KairixPaths pinned to tmp_path — never reads env."""
    return KairixPaths(
        document_root=tmp_path / "vault",
        db_path=tmp_path / "kairix.db",
        log_dir=tmp_path / "logs",
        workspace_root=tmp_path / "workspaces",
    )


def _write_session(path: Path, turns: list[dict[str, object]]) -> None:
    """Write a session-NNN.jsonl file with the given turn list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    """Write a JSON file at ``path`` from a Python object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_runner(
    *,
    tmp_path: Path,
    chat_response: str = "1.0",
    scripted_facts: list | None = None,
) -> tuple[SuiteRunner, FakeFactStore, FakeFactExtractor, FakeLLMBackend]:
    """Build a SuiteRunner pre-wired with fakes for one test scenario."""
    store = FakeFactStore()
    extractor = FakeFactExtractor(scripted_facts=scripted_facts or [])
    llm = FakeLLMBackend(chat_response=chat_response)
    runner = SuiteRunner(
        fact_store=store,
        fact_extractor=extractor,
        llm=llm,
        paths=_paths(tmp_path),
    )
    return runner, store, extractor, llm


def _lay_out_minimal_suite(suite_dir: Path) -> None:
    """Create a minimal-but-valid suite directory under ``suite_dir``."""
    _write_session(
        suite_dir / "session-001.jsonl",
        [
            {"id": "s001-t001", "speaker": "agent-alpha", "content": "starting"},
            {"id": "s001-t002", "speaker": "agent-beta", "content": "ack"},
        ],
    )
    _write_json(
        suite_dir / "ground-truth-queries.json",
        [
            {
                "question": "What did agent-alpha say?",
                "answer": "starting",
                "category": "single-hop",
            }
        ],
    )


# ---------------------------------------------------------------------------
# discover_suite
# ---------------------------------------------------------------------------


def test_discover_suite_finds_sessions_and_queries(tmp_path: Path) -> None:
    """Sabotage-proof: rename ``session-*.jsonl`` glob to ``chat-*.jsonl``
    in the runner and discovery fails on a valid suite."""
    suite_dir = tmp_path / "engagement-alpha"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    spec = runner.discover_suite(suite_dir)

    assert spec.name == "engagement-alpha"
    assert spec.path == suite_dir
    assert len(spec.session_paths) == 1
    assert spec.session_paths[0].name == "session-001.jsonl"
    assert len(spec.queries) == 1
    assert spec.queries[0]["question"] == "What did agent-alpha say?"
    assert spec.ground_truth_facts is None


def test_discover_suite_finds_ground_truth_facts_when_present(tmp_path: Path) -> None:
    """Sabotage-proof: drop the ``ground-truth-facts.json`` read in the
    runner and this test fails because ``ground_truth_facts is None``."""
    suite_dir = tmp_path / "engagement-beta"
    _lay_out_minimal_suite(suite_dir)
    _write_json(
        suite_dir / "ground-truth-facts.json",
        [{"entity": "agent-alpha", "attribute": "role", "value": "lead"}],
    )

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    spec = runner.discover_suite(suite_dir)

    assert spec.ground_truth_facts is not None
    assert spec.ground_truth_facts[0]["entity"] == "agent-alpha"


def test_discover_suite_missing_queries_file_raises_actionable_error(tmp_path: Path) -> None:
    """Sabotage-proof: remove the ground-truth-queries.json check and
    this test fails because no ValueError is raised."""
    suite_dir = tmp_path / "broken"
    _write_session(suite_dir / "session-001.jsonl", [{"id": "x", "content": "y"}])

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        runner.discover_suite(suite_dir)
    msg = str(excinfo.value)
    assert "ground-truth-queries.json" in msg
    assert "fix:" in msg
    assert "next:" in msg


def test_discover_suite_missing_directory_raises_actionable_error(tmp_path: Path) -> None:
    """Sabotage-proof: drop the directory-existence check and this fails
    because a non-existent path no longer raises ValueError."""
    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        runner.discover_suite(tmp_path / "does-not-exist")
    assert "does not exist" in str(excinfo.value)
    assert "fix:" in str(excinfo.value)


def test_discover_suite_no_sessions_raises_actionable_error(tmp_path: Path) -> None:
    """Sabotage-proof: drop the empty-sessions check and this fails."""
    suite_dir = tmp_path / "no-sessions"
    suite_dir.mkdir()
    _write_json(suite_dir / "ground-truth-queries.json", [{"question": "q", "answer": "a"}])

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        runner.discover_suite(suite_dir)
    assert "session-*.jsonl" in str(excinfo.value)
    assert "fix:" in str(excinfo.value)


def test_discover_suite_invalid_json_raises_actionable_error(tmp_path: Path) -> None:
    """Sabotage-proof: drop the JSONDecodeError catch and the raised
    exception type changes, breaking this assertion."""
    suite_dir = tmp_path / "bad-json"
    _write_session(suite_dir / "session-001.jsonl", [{"id": "x", "content": "y"}])
    (suite_dir / "ground-truth-queries.json").write_text("not-json{", encoding="utf-8")

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        runner.discover_suite(suite_dir)
    assert "valid JSON" in str(excinfo.value)
    assert "fix:" in str(excinfo.value)


def test_discover_suite_non_list_json_raises_actionable_error(tmp_path: Path) -> None:
    """Sabotage-proof: drop the list-shape check and this assertion fails."""
    suite_dir = tmp_path / "wrong-shape"
    _write_session(suite_dir / "session-001.jsonl", [{"id": "x", "content": "y"}])
    _write_json(suite_dir / "ground-truth-queries.json", {"oops": "should-be-list"})

    runner, _, _, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        runner.discover_suite(suite_dir)
    assert "JSON array" in str(excinfo.value)


# ---------------------------------------------------------------------------
# run — query scoring
# ---------------------------------------------------------------------------


def test_run_dispatches_sessions_through_extractor(tmp_path: Path) -> None:
    """Sabotage-proof: skip the ``_ingest_sessions`` call in ``run`` and
    ``extractor.calls`` is empty, failing this assertion."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_session(
        suite_dir / "session-002.jsonl",
        [{"id": "s002-t001", "speaker": "agent-alpha", "content": "more"}],
    )

    runner, _, extractor, _ = _make_runner(tmp_path=tmp_path)
    spec = runner.discover_suite(suite_dir)
    runner.run(spec)

    # One extract call per non-empty session.
    assert len(extractor.calls) == 2


def test_run_persists_extracted_facts_in_store(tmp_path: Path) -> None:
    """Sabotage-proof: drop the ``fact_store.add(fact)`` call in
    ``_ingest_sessions`` and the store stays empty, failing the assertion."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    scripted = [
        FakeFactRecord(
            id="f-001",
            entity="agent-alpha",
            attribute="role",
            value="lead",
            namespace="shared",
        )
    ]
    runner, store, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=scripted)
    spec = runner.discover_suite(suite_dir)
    runner.run(spec)

    # FakeFactStore exposes private state for tests; search() proves the round-trip.
    hits = store.search("agent-alpha role")
    assert len(hits) == 1
    assert hits[0].record.id == "f-001"


def test_run_scores_queries_and_groups_by_category(tmp_path: Path) -> None:
    """Sabotage-proof: drop the per-category bucketing in ``_score_queries``
    and ``per_category`` becomes empty, failing this assertion."""
    suite_dir = tmp_path / "scenario"
    _write_session(
        suite_dir / "session-001.jsonl",
        [{"id": "s001-t001", "speaker": "agent-alpha", "content": "starting"}],
    )
    _write_json(
        suite_dir / "ground-truth-queries.json",
        [
            {"question": "Q1?", "answer": "A1", "category": "single-hop"},
            {"question": "Q2?", "answer": "A2", "category": "single-hop"},
            {"question": "Q3?", "answer": "A3", "category": "multi-hop"},
        ],
    )

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="0.8")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.n_questions == 3
    assert result.n_passed == 3  # 0.8 >= 0.5 threshold for all three
    assert result.mean_score == pytest.approx(0.8)
    assert set(result.per_category.keys()) == {"single-hop", "multi-hop"}
    assert result.per_category["single-hop"]["n"] == 2
    assert result.per_category["multi-hop"]["n"] == 1


def test_run_failed_queries_are_not_counted_as_passed(tmp_path: Path) -> None:
    """Sabotage-proof: invert the >= comparison in the pass-threshold check
    and the pass count flips, failing this assertion."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    # Low judge score → below the 0.5 pass threshold.
    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="0.2")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.n_questions == 1
    assert result.n_passed == 0
    assert result.mean_score == pytest.approx(0.2)


def test_run_categorises_unknown_category_label(tmp_path: Path) -> None:
    """Sabotage-proof: remove the unknown-category fallback and this test
    fails because the literal category leaks into per_category keys."""
    suite_dir = tmp_path / "scenario"
    _write_session(suite_dir / "session-001.jsonl", [{"id": "x", "content": "y"}])
    _write_json(
        suite_dir / "ground-truth-queries.json",
        [{"question": "q", "answer": "a", "category": "made-up"}],
    )

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="1.0")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert "uncategorised" in result.per_category
    assert "made-up" not in result.per_category


# ---------------------------------------------------------------------------
# run — extractor F1
# ---------------------------------------------------------------------------


def test_run_computes_extractor_f1_when_ground_truth_present(tmp_path: Path) -> None:
    """Sabotage-proof: drop the F1 calculation and this fails because
    ``per_extraction_f1 is None`` instead of 1.0."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(
        suite_dir / "ground-truth-facts.json",
        [
            {"entity": "agent-alpha", "attribute": "role", "value": "lead"},
            {"entity": "agent-beta", "attribute": "role", "value": "support"},
        ],
    )

    scripted = [
        FakeFactRecord(id="f-001", entity="agent-alpha", attribute="role", value="lead engineer"),
        FakeFactRecord(id="f-002", entity="agent-beta", attribute="role", value="support"),
    ]
    runner, _, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=scripted)
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    # Both ground-truth facts matched via substring on value.
    assert result.per_extraction_f1 == pytest.approx(1.0)
    assert result.extraction_precision == pytest.approx(1.0)
    assert result.extraction_recall == pytest.approx(1.0)


def test_run_extractor_f1_partial_recall(tmp_path: Path) -> None:
    """Sabotage-proof: mutate the substring direction in the matcher
    (extracted in GT instead of GT in extracted) and this fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(
        suite_dir / "ground-truth-facts.json",
        [
            {"entity": "agent-alpha", "attribute": "role", "value": "lead"},
            {"entity": "agent-beta", "attribute": "role", "value": "support"},
        ],
    )

    # Only one of the two ground-truth facts is recovered.
    scripted = [
        FakeFactRecord(id="f-001", entity="agent-alpha", attribute="role", value="lead engineer"),
    ]
    runner, _, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=scripted)
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    # 1 match / 1 extracted = precision 1.0. 1 match / 2 GT = recall 0.5.
    assert result.extraction_precision == pytest.approx(1.0)
    assert result.extraction_recall == pytest.approx(0.5)
    assert result.per_extraction_f1 == pytest.approx(2 * 1.0 * 0.5 / (1.0 + 0.5))


def test_run_extractor_f1_skipped_when_ground_truth_absent(tmp_path: Path) -> None:
    """Sabotage-proof: replace the ``None`` short-circuit with a 0.0 and
    this fails because per_extraction_f1 is no longer None."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    # No ground-truth-facts.json on disk.

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="1.0")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.per_extraction_f1 is None
    assert result.extraction_precision is None
    assert result.extraction_recall is None
    # Query metrics still emitted.
    assert result.n_questions == 1


def test_run_extractor_f1_no_extracted_no_gt_is_perfect(tmp_path: Path) -> None:
    """Sabotage-proof: invert the gt_total==ext_total==0 branch and this fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(suite_dir / "ground-truth-facts.json", [])

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=[])
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.per_extraction_f1 == pytest.approx(1.0)


def test_run_extractor_f1_extracted_but_no_gt_is_zero(tmp_path: Path) -> None:
    """Sabotage-proof: drop the `gt_total == 0` branch and this assertion fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(suite_dir / "ground-truth-facts.json", [])

    scripted = [FakeFactRecord(id="f-001", entity="x", attribute="y", value="z")]
    runner, _, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=scripted)
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.per_extraction_f1 == pytest.approx(0.0)
    assert result.extraction_precision == pytest.approx(0.0)


def test_run_extractor_f1_substring_match_is_case_insensitive(tmp_path: Path) -> None:
    """Sabotage-proof: drop the ``.lower()`` calls in the matcher and this fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(
        suite_dir / "ground-truth-facts.json",
        [{"entity": "Agent-Alpha", "attribute": "Role", "value": "LEAD"}],
    )

    scripted = [FakeFactRecord(id="f-001", entity="agent-alpha", attribute="role", value="senior lead")]
    runner, _, _, _ = _make_runner(tmp_path=tmp_path, scripted_facts=scripted)
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.per_extraction_f1 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# LLM-judge response parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["not a number", "nan", "inf", "-inf"])
def test_run_records_unparseable_judge_reply_as_failure_not_zero(tmp_path: Path, reply: str) -> None:
    """A non-numeric or non-finite judge reply leaves the question unscored:
    excluded from n_questions / mean / per_category, counted in
    ``judge_failures``, and the row names the cause.

    Sabotage-proof: restore ``_parse_score``'s old fallbacks (``return 0.0``
    when no float is found, no ``math.isfinite`` guard) — "not a number"
    scores 0.0, "nan" poisons the mean, "inf" clamps to a 1.0 pass, and
    every leg fails. Restored.
    """
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response=reply)
    result = runner.run(runner.discover_suite(suite_dir))

    assert result.judge_failures == 1
    assert result.n_questions == 0
    assert result.n_passed == 0
    assert result.mean_score == pytest.approx(0.0)
    assert result.per_category == {}
    row = result.rows[0]
    assert row["score"] is None
    assert row["pass"] is None
    assert row["judge_failure"] == "unparseable_response"
    assert reply not in row["judge_error"]


def test_run_excludes_backend_failure_from_aggregates(tmp_path: Path) -> None:
    """``LLMBackend.chat`` returns ``""`` on failure. With one question
    judged 1.0 and one hitting a backend failure, the mean is 1.0 over one
    scored question — not 0.5 over two.

    Sabotage-proof: drop the empty-reply ``JudgeFailedError`` in
    ``SuiteRunner._judge`` (and let ``_parse_score("")`` fall back to 0.0)
    — the mean drops to 0.5 and n_questions is 2. Restored.
    """
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)
    _write_json(
        suite_dir / "ground-truth-queries.json",
        [
            {"question": "q1", "answer": "a1", "category": "single-hop"},
            {"question": "q2", "answer": "a2", "category": "single-hop"},
        ],
    )

    runner = SuiteRunner(
        fact_store=FakeFactStore(),
        fact_extractor=FakeFactExtractor(scripted_facts=[]),
        llm=FakeLLMBackend(chat_responses=["1.0", ""]),
        paths=_paths(tmp_path),
    )
    result = runner.run(runner.discover_suite(suite_dir))

    assert result.mean_score == pytest.approx(1.0)
    assert result.n_questions == 1
    assert result.n_passed == 1
    assert result.judge_failures == 1
    assert result.per_category["single-hop"]["n"] == pytest.approx(1.0)
    assert result.rows[1]["judge_failure"] == "backend_error"


@pytest.mark.parametrize("reply", ["", "   \n"])
def test_run_classifies_blank_judge_reply_as_backend_error(tmp_path: Path, reply: str) -> None:
    """An empty or whitespace-only reply is the backend's failure shape
    (``LLMBackend.chat`` returns ``""``), so it is reported as
    ``backend_error`` — not as an unparseable answer.

    Sabotage-proof: change ``if not response or not response.strip()`` to
    ``and`` in ``SuiteRunner._judge`` — the whitespace leg falls through to
    ``_parse_score`` and is reported as ``unparseable_response``. Restored.
    """
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response=reply)
    result = runner.run(runner.discover_suite(suite_dir))

    assert result.judge_failures == 1
    assert result.rows[0]["judge_failure"] == "backend_error"


def test_run_records_raising_backend_as_judge_failure(tmp_path: Path) -> None:
    """A backend that raises (despite the Protocol's never-raises contract)
    is recorded as a judge failure instead of aborting the whole suite.

    Sabotage-proof: drop the ``except Exception`` around ``self._llm.chat``
    in ``SuiteRunner._judge`` — the TimeoutError aborts ``run`` and the
    test fails. Restored.
    """
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner = SuiteRunner(
        fact_store=FakeFactStore(),
        fact_extractor=FakeFactExtractor(scripted_facts=[]),
        llm=FakeLLMBackend(chat_raises=TimeoutError("judge timed out")),
        paths=_paths(tmp_path),
    )
    result = runner.run(runner.discover_suite(suite_dir))

    assert result.judge_failures == 1
    assert result.rows[0]["judge_failure"] == "backend_error"
    assert "TimeoutError: judge timed out" in result.rows[0]["judge_error"]


def test_run_clamps_judge_response_to_unit_interval(tmp_path: Path) -> None:
    """Sabotage-proof: drop the clamping and this fails because mean_score > 1.0."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="9.9")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.mean_score == pytest.approx(1.0)


def test_run_parses_embedded_float_in_judge_response(tmp_path: Path) -> None:
    """Sabotage-proof: drop the token-scan in ``_first_float_in`` and this fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="Score: 0.7 (graded)")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.mean_score == pytest.approx(0.7)


def test_run_treats_negative_judge_response_as_zero(tmp_path: Path) -> None:
    """Sabotage-proof: drop the ``value < 0.0`` clamp and this fails."""
    suite_dir = tmp_path / "scenario"
    _lay_out_minimal_suite(suite_dir)

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="-0.4")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert result.mean_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Session reading
# ---------------------------------------------------------------------------


def test_run_skips_malformed_session_lines_without_raising(tmp_path: Path) -> None:
    """Sabotage-proof: drop the JSONDecodeError catch in ``_read_session``
    and this test fails with an unhandled exception."""
    suite_dir = tmp_path / "scenario"
    # Mix valid + malformed lines.
    (suite_dir).mkdir()
    (suite_dir / "session-001.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "t1", "content": "hello"}),
                "not-json{",
                "",  # blank
                json.dumps({"id": "t2", "content": "world"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(suite_dir / "ground-truth-queries.json", [{"question": "q", "answer": "a"}])

    runner, _, extractor, _ = _make_runner(tmp_path=tmp_path)
    spec = runner.discover_suite(suite_dir)
    runner.run(spec)

    # Only 2 valid turns make it through.
    assert len(extractor.calls) == 1
    assert len(extractor.calls[0]["turns"]) == 2


def test_run_handles_empty_session_file_without_extraction(tmp_path: Path) -> None:
    """Sabotage-proof: drop the empty-turns guard in ``_ingest_sessions``
    and this test fails because an extract() call is recorded."""
    suite_dir = tmp_path / "scenario"
    suite_dir.mkdir()
    (suite_dir / "session-001.jsonl").write_text("\n", encoding="utf-8")
    _write_json(suite_dir / "ground-truth-queries.json", [{"question": "q", "answer": "a"}])

    runner, _, extractor, _ = _make_runner(tmp_path=tmp_path)
    spec = runner.discover_suite(suite_dir)
    runner.run(spec)

    # Empty session → no extractor call.
    assert extractor.calls == []


# ---------------------------------------------------------------------------
# SuiteSpec / SuiteResult shape
# ---------------------------------------------------------------------------


def test_suite_spec_is_frozen_dataclass_safe_to_pass_around() -> None:
    """SuiteSpec must be hashable/immutable so callers can cache results."""
    spec = SuiteSpec(
        name="x",
        path=Path("/tmp/x"),
        session_paths=(Path("/tmp/x/session-001.jsonl"),),
        queries=({"question": "q", "answer": "a"},),
        ground_truth_facts=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "y"  # type: ignore[misc]  # frozen dataclass — mypy refuses the reassignment


def test_suite_result_carries_rows_for_per_question_drilldown(tmp_path: Path) -> None:
    """Sabotage-proof: drop ``rows`` from the SuiteResult return and this
    assertion fails because the list is empty."""
    suite_dir = tmp_path / "scenario"
    _write_session(suite_dir / "session-001.jsonl", [{"id": "x", "content": "y"}])
    _write_json(
        suite_dir / "ground-truth-queries.json",
        [
            {"question": "Q1?", "answer": "A1", "category": "single-hop"},
            {"question": "Q2?", "answer": "A2", "category": "multi-hop"},
        ],
    )

    runner, _, _, _ = _make_runner(tmp_path=tmp_path, chat_response="1.0")
    spec = runner.discover_suite(suite_dir)
    result = runner.run(spec)

    assert isinstance(result, SuiteResult)
    assert len(result.rows) == 2
    assert {r["question"] for r in result.rows} == {"Q1?", "Q2?"}
    assert all(r["pass"] for r in result.rows)
