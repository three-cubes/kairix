"""LoCoMo benchmark harness — thin adapter that delegates to ``kairix eval``.

Phase P6 of the unified eval/benchmark plan. Today's LoCoMo harness used to
reimplement ingestion (its own markdown formatter, its own ``kairix embed``
chain, its own data-dir lifecycle). That diverged from the production
``kairix ingest-chat`` / ``kairix eval`` pipeline.

After Wave 2/3 the unified architecture exposes:

* ``kairix/quality/eval/suite_runner.py:SuiteRunner`` — discovers and runs
  a conversation-suite directory (``session-*.jsonl`` + ground-truth files).
* ``kairix eval <suite-path>`` (entry: ``kairix.use_cases.eval_suite``) —
  the operator CLI on top of ``SuiteRunner``.
* ``kairix benchmark run <suite.yaml>`` — the unified YAML-driven runner
  (P5 lands the unified mode/scope/metrics flags; P-Ingest3 wires it
  through ``SuiteRunner`` in parallel with this commit).

This module is now a thin orchestrator:

1. Load LoCoMo JSON (``/tmp/locomo10.json`` or
   ``reference-library/conversations/locomo/locomo10.json``).
2. For each conversation, convert it into a kairix-compatible suite layout
   under a per-conversation directory:

   ```
   <out>/<conv-id>/
       session-001.jsonl
       session-001.jsonl.metadata.json      # { date_time, session_id }
       ...
       ground-truth-queries.json            # questions + expected_answer
       ground-truth-facts.json              # optional (LoCoMo doesn't ship facts)
       suite.yaml                           # unified benchmark suite descriptor
   ```

3. Invoke ``kairix eval <suite-dir>`` (canonical CLI today) or
   ``kairix benchmark run <suite.yaml>`` (preferred once SuiteRunner wiring
   lands) via subprocess. Capture stdout+stderr to log files.
4. Aggregate per-conversation results and print a single summary.

The harness keeps the ``mem0`` backend as a peer for apples-to-apples
comparison — unchanged ingest path on that side.

Constraints obeyed:

* No reimplemented embed / ingest path on the kairix side.
* No production-code touches under ``kairix/``.
* F21 actionable markers on every operator-facing error.
* F22 path conventions for the test directory.

Usage (3-question smoke on conv-26):

    python scripts/benchmarks/locomo_spike.py \
        --locomo-path /tmp/locomo10.json \
        --conversations conv-26 \
        --questions-per-conv 3 \
        --output-dir /tmp/locomo-p6-smoke \
        --backend kairix-cli
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.quality.completeness import judge_failures, partial_warning
from kairix.quality.redaction import describe_exception
from kairix.quality.scoring.types import (
    JUDGE_FAILURE_BACKEND_ERROR,
    JUDGE_FAILURE_UNPARSEABLE,
    JudgeFailedError,
)

LOGGER = logging.getLogger("locomo_spike")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCOMO_DATA_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

_KAIRIX_CLI_BACKEND = "kairix-cli"
_MEM0_BACKEND = "mem0"
_SUPPORTED_BACKENDS = (_KAIRIX_CLI_BACKEND, _MEM0_BACKEND)

# LoCoMo encodes category as an integer 1-5. Two target taxonomies need
# the mapping — the SuiteRunner-shape ``ground-truth-queries.json`` uses
# string category names from ``_KNOWN_CATEGORIES``, and the unified
# benchmark ``suite.yaml`` uses kairix's CATEGORY_WEIGHTS keys. Keep
# both maps here so the conversion adapter is the single source of truth.
_LOCOMO_CAT_TO_SUITE_RUNNER: dict[int, str] = {
    1: "single-hop",
    2: "temporal",
    3: "multi-hop",
    4: "open-domain",
    5: "adversarial",
}
_LOCOMO_CAT_TO_BENCHMARK: dict[int, str] = {
    1: "recall",
    2: "temporal",
    3: "multi_hop",
    4: "conceptual",
    5: "conceptual",
}

# Subprocess timeout for the kairix subprocess invocation. LoCoMo
# conversations can have 30+ sessions and 30+ questions; the LLM-judge
# round trip dominates. 20 min is generous and matches the prior
# ``kairix embed`` timeout budget.
_RUN_TIMEOUT_S = 1200

# Pass-threshold for the per-question score (mirrors SuiteRunner's own
# threshold; kept here so aggregate.pass_rate is comparable when the harness
# falls back to a JSON-only result file).
_PASS_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def load_locomo_json(locomo_path: Path | None) -> list[dict[str, Any]]:
    """Load LoCoMo conversations from disk or the canonical GitHub URL.

    Raises:
        ValueError: with ``fix:`` / ``next:`` markers when the file is
            unreadable or does not carry a JSON list.
    """
    if locomo_path is not None and locomo_path.exists():
        LOGGER.info("Loading LoCoMo from local file %s", locomo_path)
        data = json.loads(locomo_path.read_text(encoding="utf-8"))
    else:
        LOGGER.info("Fetching LoCoMo JSON from %s", LOCOMO_DATA_URL)
        with urllib.request.urlopen(LOCOMO_DATA_URL, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    if not isinstance(data, list):
        raise ValueError(
            f"LoCoMo data must be a JSON list; got {type(data).__name__}. "
            f"fix: pass a valid locomo10.json. "
            f"next: download from {LOCOMO_DATA_URL}"
        )
    return data


def _filter_conversations(
    data: list[dict[str, Any]],
    selected: list[str] | None,
) -> list[dict[str, Any]]:
    """Filter ``data`` to the conversations named in ``selected``."""
    if not selected:
        return data
    wanted = set(selected)
    out = [c for c in data if str(c.get("sample_id", "")) in wanted]
    missing = wanted - {str(c.get("sample_id", "")) for c in out}
    if missing:
        raise ValueError(
            f"Conversation id(s) {sorted(missing)} not found in LoCoMo data. "
            f"fix: pass ids that exist in the loaded JSON. "
            f"next: omit --conversations to run all of them."
        )
    return out


# ---------------------------------------------------------------------------
# LoCoMo -> suite-layout adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedSession:
    """Internal helper — one extracted LoCoMo session ready for emit."""

    session_num: int
    date_time: str | None
    turns: tuple[dict[str, Any], ...]


def _parse_sessions(locomo_conv: dict[str, Any]) -> list[_ParsedSession]:
    """Pull every ``session_N`` + ``session_N_date_time`` pair from a LoCoMo record."""
    conv = locomo_conv.get("conversation") or {}
    out: list[_ParsedSession] = []
    for key, value in conv.items():
        if not key.startswith("session_") or key.endswith("date_time"):
            continue
        suffix = key.removeprefix("session_")
        if not suffix.isdigit() or not isinstance(value, list):
            continue
        raw_dt = conv.get(f"{key}_date_time")
        date_time = str(raw_dt).strip() if isinstance(raw_dt, str) and raw_dt.strip() else None
        turns = tuple(t for t in value if isinstance(t, dict))
        out.append(_ParsedSession(session_num=int(suffix), date_time=date_time, turns=turns))
    out.sort(key=lambda s: s.session_num)
    return out


def _turn_to_jsonl_record(
    turn: dict[str, Any],
    *,
    conv_id: str,
    session_num: int,
    turn_idx: int,
    date_time: str | None,
) -> dict[str, Any] | None:
    """Convert one LoCoMo turn into the canonical session-jsonl shape."""
    speaker = turn.get("speaker") or turn.get("speaker_id") or turn.get("role") or "unknown"
    content = turn.get("text") or turn.get("content") or turn.get("utterance") or ""
    if not content:
        return None
    turn_id = turn.get("dia_id") or f"{conv_id}-s{session_num:03d}-t{turn_idx:03d}"
    record: dict[str, Any] = {
        "id": str(turn_id),
        "speaker": str(speaker),
        "content": str(content),
    }
    if date_time:
        record["timestamp"] = date_time
    return record


def _emit_session_files(
    sessions: list[_ParsedSession],
    *,
    suite_dir: Path,
    conv_id: str,
) -> int:
    """Write each session as ``session-NNN.jsonl`` + ``.metadata.json`` sidecar."""
    written = 0
    for session in sessions:
        session_path = suite_dir / f"session-{session.session_num:03d}.jsonl"
        records: list[dict[str, Any]] = []
        for idx, turn in enumerate(session.turns, start=1):
            rec = _turn_to_jsonl_record(
                turn,
                conv_id=conv_id,
                session_num=session.session_num,
                turn_idx=idx,
                date_time=session.date_time,
            )
            if rec is not None:
                records.append(rec)
        if not records:
            continue
        with session_path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        if session.date_time:
            sidecar = suite_dir / f"session-{session.session_num:03d}.jsonl.metadata.json"
            sidecar.write_text(
                json.dumps(
                    {
                        "session_id": f"{conv_id}-s{session.session_num:03d}",
                        "date_time": session.date_time,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        written += 1
    return written


def _emit_queries(
    locomo_conv: dict[str, Any],
    *,
    suite_dir: Path,
    questions_per_conv: int,
) -> list[dict[str, Any]]:
    """Write ``ground-truth-queries.json`` for ``SuiteRunner`` consumption."""
    raw_qa = locomo_conv.get("qa") or []
    queries: list[dict[str, Any]] = []
    for q in raw_qa:
        if not isinstance(q, dict):
            continue
        question = q.get("question")
        answer = q.get("answer") or q.get("adversarial_answer")
        if not question or not answer:
            continue
        cat_int = q.get("category")
        suite_category = _LOCOMO_CAT_TO_SUITE_RUNNER.get(
            cat_int if isinstance(cat_int, int) else 0,
            "open-domain",
        )
        queries.append(
            {
                "question": str(question),
                "answer": str(answer),
                "category": suite_category,
                "locomo_category": cat_int,
                "evidence_turn_ids": list(q.get("evidence", []) or []),
            }
        )
    queries = queries[:questions_per_conv]
    (suite_dir / "ground-truth-queries.json").write_text(
        json.dumps(queries, indent=2),
        encoding="utf-8",
    )
    return queries


def _emit_suite_yaml(
    *,
    suite_dir: Path,
    suite_name: str,
    queries: list[dict[str, Any]],
) -> Path:
    """Write the unified ``suite.yaml`` benchmark descriptor."""
    yaml_path = suite_dir / "suite.yaml"
    cases: list[dict[str, Any]] = []
    for i, q in enumerate(queries, start=1):
        cat_int = q.get("locomo_category")
        bm_category = _LOCOMO_CAT_TO_BENCHMARK.get(
            cat_int if isinstance(cat_int, int) else 0,
            "conceptual",
        )
        case: dict[str, Any] = {
            "id": f"L-q{i:02d}",
            "category": bm_category,
            "query": q["question"],
            "score_method": "llm",
            "expected_answer": q["answer"],
        }
        cases.append(case)
    suite_doc = {
        "meta": {
            "name": suite_name,
            "version": "2026-05-21",
            "collections": ["conversational"],
            "default_scope": "shared+agent",
            "default_agent": "locomo-agent",
            "focus_areas": ["conversational-multi-session", "locomo"],
            "description": ("LoCoMo conversation corpus - generated by scripts/benchmarks/locomo_spike.py"),
        },
        "cases": cases,
    }
    with yaml_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(suite_doc, fh, sort_keys=False, default_flow_style=False)
    return yaml_path


def convert_locomo_conversation_to_suite(
    locomo_conv: dict[str, Any],
    *,
    suite_dir: Path,
    suite_name: str,
    questions_per_conv: int = 30,
) -> Path:
    """Convert one LoCoMo conversation into a kairix suite directory.

    Writes:

      ``<suite_dir>/session-NNN.jsonl``           — one per session
      ``<suite_dir>/session-NNN.jsonl.metadata.json`` — { date_time, session_id }
      ``<suite_dir>/ground-truth-queries.json``  — SuiteRunner consumes
      ``<suite_dir>/suite.yaml``                 — BenchmarkSuite consumes

    Returns the path to ``suite.yaml`` so the caller can either invoke
    ``kairix benchmark run <suite.yaml>`` (preferred once P-Ingest3 routes
    it through SuiteRunner) or fall back to ``kairix eval <suite_dir>``
    (canonical today).

    Per Spike A1, the highest-leverage data point is the session-level
    ``date_time`` (closes 54% of LoCoMo cat=temporal misses); it's written
    in three places: the per-turn ``timestamp`` field, the session
    sidecar JSON, and (implicitly via the test fixture's session ids)
    the SearchPipeline's recency-weighted retrieval signal.

    Raises:
        ValueError: with actionable markers when the LoCoMo conversation
            has no sessions or no usable QA pairs.
    """
    if not isinstance(locomo_conv, dict):
        raise ValueError(
            f"LoCoMo conversation must be a dict; got {type(locomo_conv).__name__}. "
            f"fix: pass a record loaded from locomo10.json. "
            f"next: see scripts/benchmarks/locomo_spike.py:load_locomo_json."
        )

    suite_dir.mkdir(parents=True, exist_ok=True)

    sessions = _parse_sessions(locomo_conv)
    if not sessions:
        raise ValueError(
            f"LoCoMo conversation {locomo_conv.get('sample_id')!r} has no sessions. "
            f"fix: check the conversation.session_<N> keys in the source JSON. "
            f"next: re-export with the canonical LoCoMo schema."
        )

    n_sessions = _emit_session_files(sessions, suite_dir=suite_dir, conv_id=suite_name)
    if n_sessions == 0:
        raise ValueError(
            f"LoCoMo conversation {locomo_conv.get('sample_id')!r} parsed but "
            f"emitted zero session files (all turns lacked usable text). "
            f"fix: verify the speaker/text fields on each turn. "
            f"next: skip this conversation via --conversations."
        )

    queries = _emit_queries(
        locomo_conv,
        suite_dir=suite_dir,
        questions_per_conv=questions_per_conv,
    )
    if not queries:
        raise ValueError(
            f"LoCoMo conversation {locomo_conv.get('sample_id')!r} has no QA pairs. "
            f"fix: confirm the 'qa' field is a non-empty list. "
            f"next: pick a different conversation."
        )

    return _emit_suite_yaml(
        suite_dir=suite_dir,
        suite_name=suite_name,
        queries=queries,
    )


# ---------------------------------------------------------------------------
# Subprocess orchestration — kairix CLI
# ---------------------------------------------------------------------------


def _build_subprocess_env(*, data_dir: Path) -> dict[str, str]:
    """Build the env the kairix subprocess sees.

    Spike A1 finding: overriding only ``KAIRIX_DOCUMENT_ROOT`` is unsafe
    in environments where ``KAIRIX_DB_PATH`` / ``KAIRIX_DATA_DIR`` are
    baked into the deployment env. This helper pins the whole path set
    under ``data_dir`` so the run is hermetic.
    """
    env = dict(os.environ)
    env["KAIRIX_DATA_DIR"] = str(data_dir)
    env["KAIRIX_DB_PATH"] = str(data_dir / "index.sqlite")
    env["KAIRIX_WORKSPACE_ROOT"] = str(data_dir / "workspaces")
    env.setdefault("KAIRIX_TRACE", "1")
    return env


def _run_kairix_subprocess(
    *,
    args: list[str],
    env: dict[str, str],
    stdout_log: Path,
    stderr_log: Path,
) -> int:
    """Run ``args`` as a subprocess, streaming stdout+stderr to log files.

    Spike A1 finding: ``capture_output=True`` swallows stderr behind a
    successful return — operators lose the KAIRIX_TRACE breadcrumbs.
    Stream both streams to separate logs so the trace survives.
    """
    LOGGER.info("Running: %s", " ".join(args))
    with stdout_log.open("w", encoding="utf-8") as out_fh, stderr_log.open("w", encoding="utf-8") as err_fh:
        proc = subprocess.run(
            args,
            env=env,
            stdout=out_fh,
            stderr=err_fh,
            timeout=_RUN_TIMEOUT_S,
            check=False,
        )
    return proc.returncode


def _invoke_suite_run(
    *,
    suite_dir: Path,
    data_dir: Path,
    output_json: Path,
) -> dict[str, Any] | None:
    """Invoke ``kairix eval <suite_dir> --json`` and return the parsed result.

    Returns ``None`` when the subprocess crashed or emitted invalid JSON;
    the harness still aggregates the rest of the conversations.
    """
    stdout_log = suite_dir / "stdout.log"
    stderr_log = suite_dir / "stderr.log"
    cli_args = [
        sys.executable,
        "-m",
        "kairix.cli",
        "eval",
        str(suite_dir),
        "--json",
    ]
    rc = _run_kairix_subprocess(
        args=cli_args,
        env=_build_subprocess_env(data_dir=data_dir),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    if rc != 0:
        LOGGER.warning(
            "kairix subprocess exited rc=%d on %s. "
            "fix: inspect %s and %s for the trace. "
            "next: re-run with KAIRIX_TRACE=1 for verbose breadcrumbs.",
            rc,
            suite_dir.name,
            stdout_log,
            stderr_log,
        )
    try:
        raw_stdout = stdout_log.read_text(encoding="utf-8")
        start = raw_stdout.find("{")
        end = raw_stdout.rfind("}")
        if 0 <= start < end:
            parsed = json.loads(raw_stdout[start : end + 1])
            if isinstance(parsed, dict):
                payload: dict[str, Any] = parsed
                output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                return payload
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not parse kairix JSON output for %s: %s", suite_dir.name, exc)
    return None


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


@dataclass
class _ConvResult:
    """Per-conversation aggregate row."""

    backend: str
    conv_id: str
    n_questions: int
    n_passed: int
    mean_score: float
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    judge_failures: int = 0


def _result_to_conv(
    *,
    backend: str,
    conv_id: str,
    result: dict[str, Any] | None,
) -> _ConvResult:
    """Adapt a ``SuiteResult``-shaped dict to a per-conversation row."""
    if not result:
        return _ConvResult(
            backend=backend,
            conv_id=conv_id,
            n_questions=0,
            n_passed=0,
            mean_score=0.0,
        )
    return _ConvResult(
        backend=backend,
        conv_id=conv_id,
        n_questions=int(result.get("n_questions", 0)),
        n_passed=int(result.get("n_passed", 0)),
        mean_score=float(result.get("mean_score", 0.0)),
        per_category=dict(result.get("per_category") or {}),
        rows=list(result.get("rows") or []),
        judge_failures=judge_failures(result),
    )


def _run_kairix_cli_backend(
    locomo_conv: dict[str, Any],
    *,
    conv_id: str,
    suite_dir: Path,
    data_dir: Path,
    questions_per_conv: int,
) -> _ConvResult:
    """Convert + invoke ``kairix eval`` for one LoCoMo conversation."""
    convert_locomo_conversation_to_suite(
        locomo_conv,
        suite_dir=suite_dir,
        suite_name=conv_id,
        questions_per_conv=questions_per_conv,
    )
    result_path = suite_dir / "result.json"
    result = _invoke_suite_run(
        suite_dir=suite_dir,
        data_dir=data_dir,
        output_json=result_path,
    )
    return _result_to_conv(backend=_KAIRIX_CLI_BACKEND, conv_id=conv_id, result=result)


def _build_mem0_memory() -> Any:
    """Configure mem0 against the same Foundry endpoint kairix uses."""
    try:
        # mem0ai is an optional runtime dep (not in pyproject); the harness
        # surfaces the actionable ImportError below when it's missing.
        from mem0 import Memory
    except ImportError as exc:
        raise RuntimeError(
            "mem0 backend requires 'mem0ai' to be installed. "
            "fix: pip install mem0ai qdrant-client. "
            "next: rerun the spike with --backend mem0"
        ) from exc

    llm_api_key = os.environ.get("KAIRIX_LLM_API_KEY", "").strip()
    llm_endpoint = os.environ.get("KAIRIX_LLM_ENDPOINT", "").strip()
    llm_model = os.environ.get("KAIRIX_LLM_MODEL", "").strip() or "gpt-5.4-mini"
    embed_api_key = os.environ.get("KAIRIX_EMBED_API_KEY", llm_api_key).strip()
    embed_endpoint = os.environ.get("KAIRIX_EMBED_ENDPOINT", llm_endpoint).strip()
    embed_model = os.environ.get("KAIRIX_EMBED_MODEL", "").strip() or "text-embedding-3-large"

    if not llm_api_key or not llm_endpoint:
        raise RuntimeError(
            "mem0 backend requires KAIRIX_LLM_API_KEY + KAIRIX_LLM_ENDPOINT in env. "
            "fix: source the kairix secrets file before invoking the spike. "
            "next: try `--backend kairix-cli` if you only want the kairix side."
        )

    embed_base_url = embed_endpoint
    if not embed_base_url.rstrip("/").endswith("/openai/v1"):
        embed_base_url = embed_base_url.rstrip("/") + "/openai/v1"

    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": llm_model,
                "api_key": llm_api_key,
                "openai_base_url": llm_endpoint,
                "temperature": 0.3,
                "max_tokens": 800,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": embed_model,
                "api_key": embed_api_key,
                "openai_base_url": embed_base_url,
                "embedding_dims": 1536,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "locomo_spike",
                "embedding_model_dims": 1536,
                "path": "/tmp/qdrant-locomo-spike",
                "on_disk": False,
            },
        },
    }
    return Memory.from_config(config)


def _synthesise_answer_from_memories(
    question: str,
    memories: list[dict[str, Any]],
) -> str:
    """Build an answer string from mem0 search results using the LLM backend."""
    from kairix.platform.llm import get_default_backend

    backend = get_default_backend()
    if not memories:
        return "No relevant memories found."
    bullets = "\n".join(f"- {m.get('memory') or m.get('text') or ''}" for m in memories[:10])
    prompt = (
        "Answer the question concisely (1-2 sentences) using ONLY the listed memories. "
        "If the memories do not contain the answer, say so explicitly.\n\n"
        f"Question: {question}\n\n"
        f"Memories:\n{bullets}\n\n"
        "Answer:"
    )
    try:
        return backend.chat([{"role": "user", "content": prompt}], max_tokens=200).strip()
    except Exception as exc:
        return f"ERROR: synthesis {describe_exception(exc)}"


def _judge_response(
    question: str,
    ground_truth: str,
    response: str,
    *,
    backend: Any,
) -> tuple[float, bool, str]:
    """Score a single mem0 response with the LLM judge.

    ``backend`` is an ``LLMBackend`` (``chat(messages, max_tokens) -> str``);
    the mem0 runner passes kairix's configured default backend.

    Raises:
        JudgeFailedError: the backend raised or returned an empty reply
            (``backend_error``), or the reply is not a JSON object carrying
            a finite numeric ``score`` (``unparseable_response``). A judge
            failure is never scored 0.0 — the caller records the question
            as unscored and excludes it from the aggregates.
    """
    prompt = (
        "You are evaluating whether a memory system's response correctly "
        "answers a question based on prior conversation context.\n\n"
        f"Question:\n{question}\n\nGround truth answer:\n{ground_truth}\n\n"
        f"System response:\n{response}\n\n"
        "Respond with a single JSON object ONLY (no prose around it):\n"
        '{"correct": true|false, "score": 0.0-1.0, "reasoning": "one-sentence rationale"}'
    )
    try:
        raw = backend.chat([{"role": "user", "content": prompt}], max_tokens=300)
    except Exception as exc:
        raise JudgeFailedError.from_backend_exception(exc) from exc

    raw = (raw or "").strip()
    if not raw:
        raise JudgeFailedError(JUDGE_FAILURE_BACKEND_ERROR, "empty judge reply (LLM backend failure)")
    start = raw.find("{")
    end = raw.rfind("}")
    parsed: Any = None
    if 0 <= start < end:
        try:
            parsed = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            parsed = None
    if not isinstance(parsed, dict):
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, f"no JSON object in reply ({len(raw)} chars)")
    try:
        score = float(parsed["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, "reply has no numeric 'score'") from exc
    if not math.isfinite(score):
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, "non-finite score")
    score = max(0.0, min(1.0, score))
    correct = bool(parsed.get("correct", score >= _PASS_THRESHOLD))
    reasoning = str(parsed.get("reasoning", ""))[:300]
    return score, correct, reasoning


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-question rows into the ``SuiteResult``-shaped summary.

    Rows whose ``score`` is ``None`` (judge failures) are excluded from
    ``n_questions`` / ``n_passed`` / ``mean_score`` / ``per_category`` and
    counted in ``judge_failures`` — matching ``SuiteRunner.run``.
    """
    scored = [r for r in rows if r["score"] is not None]
    per_cat_scores: dict[str, list[float]] = {}
    for r in scored:
        per_cat_scores.setdefault(r["category"], []).append(r["score"])
    n_questions = len(scored)
    return {
        "n_questions": n_questions,
        "n_passed": sum(1 for r in scored if r["pass"]),
        "mean_score": (sum(r["score"] for r in scored) / n_questions) if n_questions else 0.0,
        "per_category": {
            cat: {
                "n": float(len(scores)),
                "passed": float(sum(1 for sc in scores if sc >= _PASS_THRESHOLD)),
                "mean": sum(scores) / len(scores),
            }
            for cat, scores in per_cat_scores.items()
        },
        "judge_failures": len(rows) - n_questions,
    }


def _run_mem0_backend(
    locomo_conv: dict[str, Any],
    *,
    conv_id: str,
    suite_dir: Path,
    data_dir: Path,
    questions_per_conv: int,
) -> _ConvResult:
    """Mem0 backend — ingest turns via ``Memory.add``; recall via ``Memory.search``.

    Preserved as a peer backend per the brief: apples-to-apples
    comparison vs. the kairix-cli path. The suite directory is still
    populated (so the operator can sanity-check what mem0 saw) but the
    kairix subprocess isn't invoked on this path.
    """
    del data_dir  # mem0 owns its own vector store; data_dir is kairix-only
    convert_locomo_conversation_to_suite(
        locomo_conv,
        suite_dir=suite_dir,
        suite_name=conv_id,
        questions_per_conv=questions_per_conv,
    )

    memory = _build_mem0_memory()
    user_id = f"locomo-{conv_id}"

    sessions = _parse_sessions(locomo_conv)
    turn_count = 0
    for session in sessions:
        for turn in session.turns:
            text = turn.get("text") or turn.get("content") or ""
            if not text:
                continue
            speaker = turn.get("speaker") or turn.get("role") or "unknown"
            metadata: dict[str, Any] = {"speaker": str(speaker)}
            if session.date_time:
                metadata["date_time"] = session.date_time
            try:
                memory.add(text, user_id=user_id, metadata=metadata)
                turn_count += 1
            except Exception as exc:
                LOGGER.warning("mem0 add failed for turn (speaker=%s): %s", speaker, exc)
    LOGGER.info("mem0 ingested %d turns for conv_id=%s", turn_count, conv_id)

    queries_path = suite_dir / "ground-truth-queries.json"
    queries = json.loads(queries_path.read_text(encoding="utf-8"))

    from kairix.platform.llm import get_default_backend

    judge_backend = get_default_backend()
    rows: list[dict[str, Any]] = []
    for i, qa in enumerate(queries, start=1):
        LOGGER.info("[Q %d/%d] %s", i, len(queries), str(qa["question"])[:80])
        try:
            search_result = memory.search(qa["question"], filters={"user_id": user_id}, top_k=10)
            mems = search_result.get("results") if isinstance(search_result, dict) else search_result
            response = _synthesise_answer_from_memories(qa["question"], mems or [])
        except Exception as exc:
            response = f"ERROR: mem0 search {describe_exception(exc)}"
        row: dict[str, Any] = {
            "question": qa["question"],
            "answer": qa["answer"],
            "category": str(qa.get("category", "open-domain")),
            "response": response,
        }
        try:
            score, passed, reasoning = _judge_response(qa["question"], qa["answer"], response, backend=judge_backend)
            row.update({"score": score, "pass": passed, "reasoning": reasoning})
        except JudgeFailedError as exc:
            # Unscored — never folded into the aggregates as a 0.0 "wrong".
            row.update({"score": None, "pass": None, "judge_failure": exc.reason, "judge_error": exc.detail})
        rows.append(row)

    summary = _aggregate_rows(rows)
    suite_result_payload = {"suite_name": conv_id, **summary, "rows": rows}
    (suite_dir / "result.json").write_text(json.dumps(suite_result_payload, indent=2), encoding="utf-8")
    return _ConvResult(
        backend=_MEM0_BACKEND,
        conv_id=conv_id,
        n_questions=summary["n_questions"],
        n_passed=summary["n_passed"],
        mean_score=summary["mean_score"],
        per_category=summary["per_category"],
        rows=rows,
        judge_failures=summary["judge_failures"],
    )


_BackendRunner = Callable[..., _ConvResult]
_BACKEND_DISPATCH: dict[str, _BackendRunner] = {
    _KAIRIX_CLI_BACKEND: _run_kairix_cli_backend,
    _MEM0_BACKEND: _run_mem0_backend,
}


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


def _print_summary(conv_results: list[_ConvResult]) -> None:
    """Render the cross-conversation summary table."""
    if not conv_results:
        print("No conversations scored.")
        return
    backends = sorted({c.backend for c in conv_results})
    total_q = sum(c.n_questions for c in conv_results)
    total_p = sum(c.n_passed for c in conv_results)
    overall_mean = sum(c.mean_score * c.n_questions for c in conv_results) / total_q if total_q else 0.0
    print()
    print("=" * 70)
    print("LoCoMo benchmark — P6 harness summary")
    print("=" * 70)
    print(f"Backend(s)      : {', '.join(backends)}")
    print(f"Conversations   : {len(conv_results)}")
    print(f"Questions       : {total_q}")
    if total_q:
        print(f"Passes          : {total_p}/{total_q} ({100 * total_p / total_q:.1f}%)")
    else:
        print("Passes          : 0/0")
    print(f"Mean score      : {overall_mean:.3f}")
    n_judge_failures = sum(c.judge_failures for c in conv_results)
    if n_judge_failures:
        print(partial_warning("this LoCoMo run", n_judge_failures))
        print(f"Judge failures  : {n_judge_failures} question(s) unscored — excluded from the numbers above")
    print()
    print(f"  {'conv':<14}  {'backend':<12}  {'passed':>6}  {'questions':>9}  {'mean':>6}")
    print(f"  {'-' * 14}  {'-' * 12}  {'-' * 6}  {'-' * 9}  {'-' * 6}")
    for c in conv_results:
        print(f"  {c.conv_id:<14}  {c.backend:<12}  {c.n_passed:>6}  {c.n_questions:>9}  {c.mean_score:>6.3f}")
    print("=" * 70)


def _write_aggregate_json(conv_results: list[_ConvResult], output_json: Path) -> None:
    """Persist the aggregate result as canonical JSON."""
    payload = {
        "backends": sorted({c.backend for c in conv_results}),
        "conversations": [
            {
                "backend": c.backend,
                "conv_id": c.conv_id,
                "n_questions": c.n_questions,
                "n_passed": c.n_passed,
                "mean_score": c.mean_score,
                "per_category": c.per_category,
                "rows": c.rows,
                "judge_failures": c.judge_failures,
            }
            for c in conv_results
        ],
        "totals": {
            "n_conversations": len(conv_results),
            "n_questions": sum(c.n_questions for c in conv_results),
            "n_passed": sum(c.n_passed for c in conv_results),
            "judge_failures": sum(c.judge_failures for c in conv_results),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_one_conversation(
    locomo_conv: dict[str, Any],
    *,
    backend: str,
    output_root: Path,
    questions_per_conv: int,
) -> _ConvResult:
    """Reset per-conv state and dispatch to the chosen backend."""
    conv_id = str(locomo_conv.get("sample_id") or "unknown")
    suite_dir = output_root / conv_id
    data_dir = output_root / f"{conv_id}-data"
    if suite_dir.exists():
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True, exist_ok=True)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    runner = _BACKEND_DISPATCH.get(backend)
    if runner is None:
        raise ValueError(
            f"unknown backend {backend!r}; supported: {sorted(_BACKEND_DISPATCH)}. "
            f"fix: pass --backend with one of those names. "
            f"next: see scripts/benchmarks/locomo_spike.py for the dispatch table."
        )
    return runner(
        locomo_conv,
        conv_id=conv_id,
        suite_dir=suite_dir,
        data_dir=data_dir,
        questions_per_conv=questions_per_conv,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LoCoMo benchmark — P6 thin-adapter harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--locomo-path",
        type=Path,
        default=Path("/tmp/locomo10.json"),
        help="Path to locomo10.json (default: /tmp/locomo10.json; fetched from GitHub if missing).",
    )
    parser.add_argument(
        "--conversations",
        default=None,
        help="Comma-separated LoCoMo sample_ids to run (default: all in the JSON).",
    )
    parser.add_argument(
        "--questions-per-conv",
        type=int,
        default=30,
        help="Cap on questions per conversation (default: 30).",
    )
    parser.add_argument(
        "--backend",
        choices=_SUPPORTED_BACKENDS,
        default=_KAIRIX_CLI_BACKEND,
        help="Memory backend under benchmark (default: kairix-cli).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Per-conversation suite + log root (default: mktemp under /tmp).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Path to write the aggregate JSON result (default: <output-dir>/aggregate.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    output_root = args.output_dir or Path(f"/tmp/locomo-p6-{os.getpid()}")
    output_root.mkdir(parents=True, exist_ok=True)
    aggregate_json = args.output_json or (output_root / "aggregate.json")

    try:
        data = load_locomo_json(args.locomo_path if args.locomo_path else None)
    except (OSError, ValueError) as exc:
        LOGGER.error("Failed to load LoCoMo dataset: %s", exc)
        return 2

    selected = [s.strip() for s in args.conversations.split(",")] if args.conversations else None
    try:
        data = _filter_conversations(data, selected)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    conv_results: list[_ConvResult] = []
    for conv in data:
        try:
            result = _run_one_conversation(
                conv,
                backend=args.backend,
                output_root=output_root,
                questions_per_conv=args.questions_per_conv,
            )
            conv_results.append(result)
        except Exception as exc:
            LOGGER.exception("Conversation %s failed: %s", conv.get("sample_id"), exc)

    _print_summary(conv_results)
    _write_aggregate_json(conv_results, aggregate_json)
    LOGGER.info("Wrote aggregate JSON to %s", aggregate_json)
    LOGGER.info("Per-conversation artefacts under %s", output_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
