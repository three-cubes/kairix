"""Tests for scripts/benchmarks/locomo_spike.py — P6 thin-adapter harness.

Covers the ``convert_locomo_conversation_to_suite`` adapter (happy path,
missing-date handling, invalid conversation shape) and the smoke-test
subprocess invocation (skipped without KAIRIX_KV_NAME).

Sabotage proofs documented in each test docstring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from kairix.quality.scoring.types import JudgeFailedError
from tests.fakes import FakeLLMBackend

pytestmark = pytest.mark.unit

_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "benchmarks" / "locomo_spike.py"


def _load_locomo_spike() -> ModuleType:
    """Import the harness script as a module via importlib.

    Registers the module in ``sys.modules`` before ``exec_module`` so
    Python 3.14's ``dataclasses._is_type`` lookup finds the module
    namespace via ``cls.__module__``. Without the registration,
    decorating a ``@dataclass`` inside the loaded module raises
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
    """
    module_name = "locomo_spike_under_test"
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib stubs omit exec_module
    return mod


_mod = _load_locomo_spike()
convert_locomo_conversation_to_suite = _mod.convert_locomo_conversation_to_suite
load_locomo_json = _mod.load_locomo_json
judge_response = _mod._judge_response
aggregate_rows = _mod._aggregate_rows


# ---------------------------------------------------------------------------
# Test fixtures — minimal-but-real LoCoMo-shaped data
# ---------------------------------------------------------------------------


def _minimal_locomo_conv(*, sample_id: str = "conv-test") -> dict[str, Any]:
    """Build a minimal LoCoMo-shaped conversation dict.

    Two sessions, three turns each, with date_time pinned on session_1 only
    so the missing-date branch in _emit_session_files is exercised.
    """
    return {
        "sample_id": sample_id,
        "conversation": {
            "session_1": [
                {"speaker": "Alice", "dia_id": "D1:1", "text": "Hello there."},
                {"speaker": "Bob", "dia_id": "D1:2", "text": "Hi Alice."},
                {"speaker": "Alice", "dia_id": "D1:3", "text": "I went to the support group yesterday."},
            ],
            "session_1_date_time": "10:00 am on 8 May, 2023",
            "session_2": [
                {"speaker": "Alice", "dia_id": "D2:1", "text": "Painting was fun."},
                {"speaker": "Bob", "dia_id": "D2:2", "text": "When did you paint?"},
            ],
            # Note: session_2_date_time intentionally absent — exercises the
            # no-sidecar branch in _emit_session_files.
        },
        "qa": [
            {
                "question": "When did Alice go to the support group?",
                "answer": "7 May 2023",
                "category": 2,
                "evidence": ["D1:3"],
            },
            {
                "question": "What is the answer to life?",
                "answer": "42",
                "category": 4,
                "evidence": [],
            },
        ],
    }


# ---------------------------------------------------------------------------
# convert_locomo_conversation_to_suite — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_convert_writes_session_jsonl_per_session(tmp_path: Path) -> None:
    """Happy-path: two LoCoMo sessions produce two session-NNN.jsonl files.

    Sabotage-proof: change ``_emit_session_files`` to skip every session
    (``return 0``) → assertion fails (no session-001.jsonl). Restored.
    """
    suite_dir = tmp_path / "conv-test"
    convert_locomo_conversation_to_suite(
        _minimal_locomo_conv(),
        suite_dir=suite_dir,
        suite_name="conv-test",
    )

    assert (suite_dir / "session-001.jsonl").exists()
    assert (suite_dir / "session-002.jsonl").exists()


@pytest.mark.unit
def test_convert_writes_metadata_sidecar_when_date_time_present(tmp_path: Path) -> None:
    """Session with date_time → ``.metadata.json`` sidecar carries it.

    Sabotage-proof: remove the ``if session.date_time:`` guard in
    ``_emit_session_files`` and replace with ``if False:`` → the sidecar
    is never written and the assertion fails. Restored.
    """
    suite_dir = tmp_path / "conv-test"
    convert_locomo_conversation_to_suite(
        _minimal_locomo_conv(),
        suite_dir=suite_dir,
        suite_name="conv-test",
    )

    sidecar = suite_dir / "session-001.jsonl.metadata.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["date_time"] == "10:00 am on 8 May, 2023"
    assert payload["session_id"] == "conv-test-s001"


@pytest.mark.unit
def test_convert_omits_sidecar_when_date_time_missing(tmp_path: Path) -> None:
    """Session without date_time → no sidecar file emitted.

    Sabotage-proof: drop the ``if session.date_time:`` guard so sidecars
    are written unconditionally → session-002.jsonl.metadata.json exists
    and the assertion fails. Restored.
    """
    suite_dir = tmp_path / "conv-test"
    convert_locomo_conversation_to_suite(
        _minimal_locomo_conv(),
        suite_dir=suite_dir,
        suite_name="conv-test",
    )

    assert (suite_dir / "session-002.jsonl").exists()
    assert not (suite_dir / "session-002.jsonl.metadata.json").exists()


@pytest.mark.unit
def test_convert_writes_ground_truth_queries(tmp_path: Path) -> None:
    """Queries land in ground-truth-queries.json with the SuiteRunner schema.

    Sabotage-proof: change ``_emit_queries`` to write an empty list ``[]``
    → assertion ``len(queries) == 2`` fails. Restored.
    """
    suite_dir = tmp_path / "conv-test"
    convert_locomo_conversation_to_suite(
        _minimal_locomo_conv(),
        suite_dir=suite_dir,
        suite_name="conv-test",
    )

    queries_path = suite_dir / "ground-truth-queries.json"
    assert queries_path.exists()
    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    assert len(queries) == 2
    assert queries[0]["question"] == "When did Alice go to the support group?"
    assert queries[0]["answer"] == "7 May 2023"
    # category=2 in LoCoMo maps to "temporal" in the SuiteRunner taxonomy
    assert queries[0]["category"] == "temporal"


@pytest.mark.unit
def test_convert_writes_suite_yaml_with_unified_fields(tmp_path: Path) -> None:
    """suite.yaml carries the unified P4-shape metadata + cases.

    Sabotage-proof: remove ``"default_scope"`` from the ``meta`` dict in
    ``_emit_suite_yaml`` → assertion fails. Restored.
    """
    suite_dir = tmp_path / "conv-test"
    yaml_path = convert_locomo_conversation_to_suite(
        _minimal_locomo_conv(),
        suite_dir=suite_dir,
        suite_name="conv-test",
    )

    assert yaml_path == suite_dir / "suite.yaml"
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    meta = doc["meta"]
    assert meta["name"] == "conv-test"
    assert meta["default_scope"] == "shared+agent"
    assert meta["default_agent"] == "locomo-agent"
    assert "locomo" in meta["focus_areas"]

    cases = doc["cases"]
    assert len(cases) == 2
    # category=2 in LoCoMo maps to "temporal" in the benchmark taxonomy too
    assert cases[0]["category"] == "temporal"
    assert cases[0]["expected_answer"] == "7 May 2023"
    assert cases[0]["score_method"] == "llm"


@pytest.mark.unit
def test_convert_caps_questions_per_conv(tmp_path: Path) -> None:
    """`questions_per_conv` truncates the QA list.

    Sabotage-proof: drop the ``queries[:questions_per_conv]`` slice and
    return the full list → assertion fails (3 > 1). Restored.
    """
    conv = _minimal_locomo_conv()
    # Add a third QA so the cap test sees a real truncation
    conv["qa"].append({"question": "Extra?", "answer": "yes", "category": 1, "evidence": []})

    suite_dir = tmp_path / "conv-cap"
    convert_locomo_conversation_to_suite(
        conv,
        suite_dir=suite_dir,
        suite_name="conv-cap",
        questions_per_conv=1,
    )

    queries = json.loads((suite_dir / "ground-truth-queries.json").read_text(encoding="utf-8"))
    assert len(queries) == 1


# ---------------------------------------------------------------------------
# convert_locomo_conversation_to_suite — error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_convert_rejects_non_dict_input(tmp_path: Path) -> None:
    """Passing a list (not a dict) raises ValueError with actionable markers.

    Sabotage-proof: remove the ``isinstance(locomo_conv, dict)`` guard at
    the top of the adapter → the function crashes with KeyError/AttributeError
    instead of ValueError, and the ``"fix:"`` assertion fails. Restored.
    """
    with pytest.raises(ValueError) as exc:
        convert_locomo_conversation_to_suite(
            ["not", "a", "dict"],  # type: ignore[arg-type]  # exercising the type-guard
            suite_dir=tmp_path / "bad",
            suite_name="bad",
        )
    assert "fix:" in str(exc.value)
    assert "next:" in str(exc.value)


@pytest.mark.unit
def test_convert_rejects_conversation_without_sessions(tmp_path: Path) -> None:
    """A conversation with no session_N keys raises ValueError with markers.

    Sabotage-proof: drop the ``if not sessions:`` guard → KeyError downstream
    instead of the typed ValueError, and the marker assertion fails. Restored.
    """
    empty_conv = {"sample_id": "empty", "conversation": {}, "qa": [{"question": "?", "answer": "."}]}
    with pytest.raises(ValueError) as exc:
        convert_locomo_conversation_to_suite(
            empty_conv,
            suite_dir=tmp_path / "empty",
            suite_name="empty",
        )
    assert "no sessions" in str(exc.value)
    assert "fix:" in str(exc.value)


@pytest.mark.unit
def test_convert_rejects_conversation_without_qa(tmp_path: Path) -> None:
    """A conversation with sessions but no usable QA raises ValueError.

    Sabotage-proof: comment out the ``if not queries:`` guard → the call
    succeeds emitting an empty ground-truth-queries.json, and the
    ``raises`` assertion fails. Restored.
    """
    no_qa_conv = {
        "sample_id": "no-qa",
        "conversation": {
            "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "hello"}],
            "session_1_date_time": "9 am on 1 Jan, 2024",
        },
        "qa": [],
    }
    with pytest.raises(ValueError) as exc:
        convert_locomo_conversation_to_suite(
            no_qa_conv,
            suite_dir=tmp_path / "no-qa",
            suite_name="no-qa",
        )
    assert "no QA pairs" in str(exc.value)


# ---------------------------------------------------------------------------
# load_locomo_json
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_locomo_json_reads_local_file(tmp_path: Path) -> None:
    """Local file present → loaded directly without network.

    Sabotage-proof: change the ``locomo_path.exists()`` branch to always
    take the URL fetch path → the test fails with a network call (or
    OSError on offline boxes). Restored.
    """
    payload = [{"sample_id": "fake", "conversation": {}, "qa": []}]
    local = tmp_path / "locomo.json"
    local.write_text(json.dumps(payload), encoding="utf-8")

    data = load_locomo_json(local)
    assert data == payload


@pytest.mark.unit
def test_load_locomo_json_rejects_non_list(tmp_path: Path) -> None:
    """A LoCoMo file that isn't a JSON list raises ValueError with markers.

    Sabotage-proof: drop the ``isinstance(data, list)`` guard → no error
    raised, and the test fails on the missing exception. Restored.
    """
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        load_locomo_json(bad)
    assert "JSON list" in str(exc.value)
    assert "fix:" in str(exc.value)


# ---------------------------------------------------------------------------
# Smoke test — full pipeline against a kairix subprocess
# ---------------------------------------------------------------------------


_KV_NAME = os.environ.get("KAIRIX_KV_NAME")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(
    not _KV_NAME,
    reason=(
        "Smoke test needs KAIRIX_KV_NAME for Azure-Foundry secret resolution. "
        "fix: source the kairix secrets file before running. "
        "next: re-run with KAIRIX_KV_NAME=<your-key-vault-name>."
    ),
)
def test_smoke_subprocess_pipeline_does_not_crash(tmp_path: Path) -> None:
    """End-to-end: harness invocation against a 1-conv minimal input doesn't crash.

    Validates the subprocess pipeline at the harness boundary — we don't
    assert on pass-rate (the smoke run is too small to be statistically
    meaningful), only that the harness exits cleanly and produces the
    aggregate JSON envelope.

    Sabotage-proof: change ``main`` to ``return 99`` unconditionally → the
    subprocess returncode assertion fails. Restored.
    """
    locomo_path = Path("/tmp/locomo10.json")
    if not locomo_path.exists():
        pytest.skip(
            "Smoke test needs /tmp/locomo10.json. "
            "fix: download from "
            "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json. "
            "next: re-run after the download."
        )

    output_dir = tmp_path / "smoke-out"
    result = subprocess.run(  # nosec B603 — fixed args, hermetic tmp_path
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--locomo-path",
            str(locomo_path),
            "--conversations",
            "conv-26",
            "--questions-per-conv",
            "3",
            "--output-dir",
            str(output_dir),
            "--backend",
            "kairix-cli",
        ],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )

    # The harness should return 0 even when the inner kairix subprocess
    # reports per-question failures — it's the harness's job to aggregate,
    # not to gate on quality.
    assert result.returncode == 0, f"harness crashed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    aggregate_json = output_dir / "aggregate.json"
    assert aggregate_json.exists(), f"aggregate JSON missing; stderr: {result.stderr[-1000:]}"
    payload = json.loads(aggregate_json.read_text(encoding="utf-8"))
    assert payload["totals"]["n_conversations"] == 1


# ---------------------------------------------------------------------------
# mem0 judge — failures are typed, never scored 0.0
# ---------------------------------------------------------------------------


def test_judge_response_parses_json_verdict() -> None:
    """A well-formed verdict is parsed (score clamped, reasoning kept)."""
    backend = FakeLLMBackend(chat_response='{"correct": true, "score": 0.9, "reasoning": "matches"}')
    score, correct, reasoning = judge_response("q", "a", "resp", backend=backend)
    assert score == pytest.approx(0.9)
    assert correct is True
    assert reasoning == "matches"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "backend_error"),
        ("not json at all", "unparseable_response"),
        ('{"correct": false, "reasoning": "no score"}', "unparseable_response"),
        ('{"correct": true, "score": NaN}', "unparseable_response"),
        ('{"correct": true, "score": Infinity}', "unparseable_response"),
    ],
)
def test_judge_response_raises_typed_failure_instead_of_zero(reply: str, reason: str) -> None:
    """Empty, non-JSON, score-less and non-finite replies are judge failures.

    Sabotage-proof: restore the old ``return 0.0, False, ...`` fallbacks in
    ``_judge_response`` (and drop the ``math.isfinite`` guard) — every leg
    returns a score instead of raising and fails. Restored.
    """
    with pytest.raises(JudgeFailedError) as excinfo:
        judge_response("q", "a", "resp", backend=FakeLLMBackend(chat_response=reply))
    assert excinfo.value.reason == reason


def test_judge_response_reports_raising_backend() -> None:
    """A raising backend is a ``backend_error`` judge failure.

    Sabotage-proof: return ``0.0, False, ...`` from the ``except`` around
    ``backend.chat`` — no exception is raised and the test fails. Restored.
    """
    with pytest.raises(JudgeFailedError) as excinfo:
        judge_response(
            "q",
            "a",
            "resp",
            backend=FakeLLMBackend(chat_raises=TimeoutError("timed out; api-key credential-sentinel-7f3a")),
        )
    assert excinfo.value.reason == "backend_error"
    assert excinfo.value.detail == "backend raised TimeoutError"
    assert "credential-sentinel-7f3a" not in str(excinfo.value)


def test_aggregate_rows_excludes_judge_failures() -> None:
    """Unscored rows (judge failures) are left out of every aggregate and counted.

    Sabotage-proof: aggregate over all rows (``scored = rows`` with a 0.0
    for ``None``) — the mean drops to 0.5 and n_questions becomes 2.
    Restored.
    """
    rows = [
        {"category": "single-hop", "score": 1.0, "pass": True},
        {"category": "single-hop", "score": None, "pass": None, "judge_failure": "backend_error"},
    ]
    summary = aggregate_rows(rows)
    assert summary["n_questions"] == 1
    assert summary["n_passed"] == 1
    assert summary["mean_score"] == pytest.approx(1.0)
    assert summary["per_category"]["single-hop"]["n"] == pytest.approx(1.0)
    assert summary["judge_failures"] == 1
