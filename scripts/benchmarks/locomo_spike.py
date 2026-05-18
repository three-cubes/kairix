"""LoCoMo recall spike for kairix — minimum viable benchmark harness.

Measures whether kairix can recall facts from prior multi-session dialogue when
asked a question later. Same task shape as mem0's memory-benchmarks LoCoMo
suite, but standalone (no mem0 harness coupling) and using kairix's existing
LLM backend for the judge call (no separate API key).

This is a *spike*, not production: defensive schema handling, no parallelism,
serial question execution, single-conversation default. The goal is end-to-end
signal in <1 hour and <$5 of judge calls before deciding whether to scale up.

Usage (smallest run — 1 conversation, 10 questions):

    python scripts/benchmarks/locomo_spike.py \\
        --vault-root /tmp/kairix-locomo-vault \\
        --num-conversations 1 \\
        --max-questions 10 \\
        --output-csv /tmp/locomo-spike-results.csv \\
        --reset-vault

Requirements:

    pip install datasets

    Plus kairix installed (``pip install -e .`` from the kairix repo root) and
    a working kairix LLM backend (``provider:`` set in ``kairix.config.yaml``).
    The script reuses whatever provider is configured for the judge call.

Output:

    A CSV with one row per question: conv_id, question, ground_truth,
    kairix_response, judge_correct, judge_score, judge_reasoning.
    Plus a summary printed to stdout: total questions, pass rate, mean score.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("locomo_spike")

# Judge prompt — adapted from mem0/memory-benchmarks judge semantics. Kept
# inline so the spike is self-contained; if we adopt the harness long-term
# we'll vendor mem0's exact prompt under attribution for apples-to-apples
# comparison.
JUDGE_PROMPT = """\
You are evaluating whether a memory system's response correctly
answers a question based on prior conversation context.

Question:
{question}

Ground truth answer:
{ground_truth}

System response:
{response}

Score the system's response:
- Does it correctly answer the question?
- Is it factually consistent with the ground truth?
- Partial credit is appropriate for partially correct answers.

Respond with a single JSON object ONLY (no prose around it):
{{"correct": true|false, "score": 0.0-1.0, "reasoning": "one-sentence rationale"}}
"""

# kairix prep command — using L0 tier for fastest synthesised answer. L1 would
# do deeper retrieval at higher cost; not needed for spike-level signal.
_PREP_TIER = "l0"

# Per-question timeout for the kairix prep call. 60s is generous; production
# pipeline is sub-5s warm.
_PREP_TIMEOUT_S = 60

# Embed timeout — needs to be larger because the worker processes every file
# in the vault. 10 min handles ~10 conversations x 30 sessions each.
_EMBED_TIMEOUT_S = 600


# ----------------------------------------------------------------------------
# Dataset loading
# ----------------------------------------------------------------------------


LOCOMO_DATA_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def load_locomo(num_conversations: int, local_path: Path | None = None) -> list[dict[str, Any]]:
    """Load the first N LoCoMo conversations.

    LoCoMo is shipped as a JSON file in the snap-research/locomo GitHub repo
    (10 records, each with `sample_id`, `conversation`, `qa`, etc.). When
    ``local_path`` is given we read from disk; otherwise we fetch the raw
    JSON from GitHub.
    """
    import json
    import urllib.request

    if local_path and local_path.exists():
        LOGGER.info("Loading LoCoMo from local file %s", local_path)
        data = json.loads(local_path.read_text(encoding="utf-8"))
    else:
        LOGGER.info("Fetching LoCoMo JSON from %s", LOCOMO_DATA_URL)
        with urllib.request.urlopen(LOCOMO_DATA_URL, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"Expected LoCoMo to be a JSON list of records; got {type(data).__name__}")
    n = min(num_conversations, len(data))
    LOGGER.info("Got %d conversations; using first %d.", len(data), n)
    return data[:n]


def _extract_sessions(conversation: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Extract sessions (each a list of turns) from a LoCoMo record.

    LoCoMo's shape: ``conversation`` is a dict with keys like ``session_1``,
    ``session_2``, … (each a list of turn-dicts with ``speaker``, ``text``,
    ``dia_id``) interleaved with ``session_N_date_time`` strings. We pull
    out the session lists in numeric order.
    """
    conv = conversation.get("conversation") or {}
    sessions: list[tuple[int, list[dict[str, Any]]]] = []
    for key, value in conv.items():
        if not key.startswith("session_") or key.endswith("date_time"):
            continue
        suffix = key.removeprefix("session_")
        if not suffix.isdigit():
            continue
        if not isinstance(value, list):
            continue
        sessions.append((int(suffix), value))
    sessions.sort(key=lambda t: t[0])
    if not sessions:
        raise ValueError(f"Could not find sessions in LoCoMo record. conversation keys: {sorted(conv.keys())}")
    return [s for _, s in sessions]


def _extract_questions(conversation: dict[str, Any]) -> list[dict[str, str]]:
    """Extract (question, ground_truth) pairs from a LoCoMo record's ``qa`` list."""
    raw = conversation.get("qa") or []
    out: list[dict[str, str]] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        question = q.get("question")
        # LoCoMo answers are stored under "answer"; a small number of category-5
        # adversarial questions use "adversarial_answer". Either is acceptable
        # as ground truth for the recall task.
        ground_truth = q.get("answer") or q.get("adversarial_answer")
        if not question or not ground_truth:
            continue
        out.append({"question": str(question), "ground_truth": str(ground_truth)})
    if not out:
        raise ValueError(f"Could not find QA pairs in LoCoMo record. Available keys: {sorted(conversation.keys())}")
    return out


# ----------------------------------------------------------------------------
# Vault writing
# ----------------------------------------------------------------------------


def write_sessions_to_vault(
    sessions: list[list[dict[str, Any]]],
    vault_path: Path,
    conv_id: str,
) -> int:
    """Write each session as a separate markdown file under ``vault_path``.

    Returns the number of session files written.
    """
    conv_dir = vault_path / f"conv-{conv_id}"
    conv_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for i, session in enumerate(sessions, start=1):
        path = conv_dir / f"session-{i:03d}.md"
        path.write_text(_format_session_as_markdown(session, conv_id, i), encoding="utf-8")
        written += 1
    return written


def _format_session_as_markdown(session: list[dict[str, Any]], conv_id: str, session_num: int) -> str:
    """Render a list of turns as a single markdown file.

    Convention: each turn becomes ``**Speaker**: content`` followed by a
    blank line. The frontmatter records conv_id + session_num so kairix's
    retrieval can use them as metadata if needed later.
    """
    lines: list[str] = [
        "---",
        f"conv_id: {conv_id}",
        f"session_num: {session_num}",
        "source: locomo",
        "---",
        "",
        f"# Conversation {conv_id} — Session {session_num}",
        "",
    ]
    for turn in session:
        if not isinstance(turn, dict):
            continue
        speaker = turn.get("speaker") or turn.get("speaker_id") or turn.get("role") or "unknown"
        content = turn.get("text") or turn.get("content") or turn.get("utterance") or ""
        if not content:
            continue
        lines.append(f"**{speaker}**: {content}")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Kairix calls
# ----------------------------------------------------------------------------


def _spike_env(vault_path: Path) -> dict[str, str]:
    """Build the per-spike subprocess env with the FULL path set overridden.

    Overriding only ``KAIRIX_DOCUMENT_ROOT`` is unsafe in environments where
    ``KAIRIX_DB_PATH`` / ``KAIRIX_DATA_DIR`` / ``KAIRIX_WORKSPACE_ROOT`` are
    set in the inherited env (e.g. the deployed container has these baked
    into the Dockerfile). Embed would walk the spike vault but write its
    SQLite + vectors into the production index, and prep would read the
    production index back — so answers come from the wrong corpus even
    though the doc root looks isolated. This helper pins the whole path
    set under ``vault_path``'s sibling directories so the spike run is
    fully isolated from any inherited deployment config.
    """
    spike_data = vault_path.parent / f"{vault_path.name}-data"
    spike_data.mkdir(parents=True, exist_ok=True)
    overrides: dict[str, str] = {
        "KAIRIX_DOCUMENT_ROOT": str(vault_path),
        "KAIRIX_DATA_DIR": str(spike_data),
        "KAIRIX_DB_PATH": str(spike_data / "index.sqlite"),
        "KAIRIX_WORKSPACE_ROOT": str(spike_data / "workspaces"),
    }
    return {**os.environ, **overrides}


def run_kairix_embed(vault_path: Path) -> None:
    """Embed the vault by running ``kairix embed`` with the full path set isolated."""
    LOGGER.info("Running kairix embed against %s ...", vault_path)
    env = _spike_env(vault_path)
    result = subprocess.run(
        ["kairix", "embed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=_EMBED_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kairix embed failed (rc={result.returncode}): {result.stderr[-500:]}")
    LOGGER.info("kairix embed completed.")


def run_kairix_prep(question: str, vault_path: Path) -> str:
    """Call ``kairix prep`` for a single question and return the rendered output.

    Errors (timeout, non-zero exit) are caught and returned as the response
    string so the judge can score them as failures rather than crashing the
    whole run.
    """
    env = _spike_env(vault_path)
    try:
        result = subprocess.run(
            ["kairix", "prep", question, "--tier", _PREP_TIER],
            env=env,
            capture_output=True,
            text=True,
            timeout=_PREP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: kairix prep timed out after {_PREP_TIMEOUT_S}s"

    if result.returncode != 0:
        return f"ERROR: kairix prep rc={result.returncode}: {result.stderr[-200:]}"
    return result.stdout.strip()


# ----------------------------------------------------------------------------
# Judge
# ----------------------------------------------------------------------------


def judge_response(question: str, ground_truth: str, response: str) -> dict[str, Any]:
    """Use kairix's configured LLM backend to score a single response.

    Returns ``{correct: bool, score: float, reasoning: str}``. Falls back to
    a structured failure record on any error so one bad judge call doesn't
    abort the run.
    """
    from kairix.platform.llm import get_default_backend

    backend = get_default_backend()
    prompt = JUDGE_PROMPT.format(question=question, ground_truth=ground_truth, response=response)
    try:
        raw = backend.chat([{"role": "user", "content": prompt}], max_tokens=300)
    except Exception as exc:
        return {
            "correct": False,
            "score": 0.0,
            "reasoning": f"judge call failed: {type(exc).__name__}: {exc!s}",
        }

    return _parse_judge_output(raw)


def _parse_judge_output(raw: str) -> dict[str, Any]:
    """Extract the JSON object from the judge's response.

    LLMs sometimes wrap the JSON in prose or markdown fences; do a tolerant
    extraction before falling back to a structured failure record.
    """
    raw = raw.strip()
    # Strip common markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip("`").strip()

    # Try the obvious whole-string parse first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                "correct": bool(parsed.get("correct", False)),
                "score": float(parsed.get("score", 0.0)),
                "reasoning": str(parsed.get("reasoning", ""))[:300],
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Try extracting the first {...} substring
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return {
                    "correct": bool(parsed.get("correct", False)),
                    "score": float(parsed.get("score", 0.0)),
                    "reasoning": str(parsed.get("reasoning", ""))[:300],
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return {
        "correct": False,
        "score": 0.0,
        "reasoning": f"judge returned non-JSON: {raw[:200]}",
    }


# ----------------------------------------------------------------------------
# Main runner
# ----------------------------------------------------------------------------


def run_conversation(
    conversation: dict[str, Any],
    vault_path: Path,
    max_questions: int,
) -> list[dict[str, Any]]:
    """Embed one conversation's sessions, then run + judge its questions."""
    conv_id = str(conversation.get("sample_id") or conversation.get("id") or "unknown")

    LOGGER.info("=== Conversation %s ===", conv_id)
    sessions = _extract_sessions(conversation)
    questions = _extract_questions(conversation)
    if not sessions:
        LOGGER.warning("Conversation %s has no sessions; skipping.", conv_id)
        return []
    if not questions:
        LOGGER.warning("Conversation %s has no questions; skipping.", conv_id)
        return []

    questions = questions[:max_questions]
    LOGGER.info("Writing %d sessions, will ask %d questions.", len(sessions), len(questions))

    write_sessions_to_vault(sessions, vault_path, conv_id)
    run_kairix_embed(vault_path)

    rows: list[dict[str, Any]] = []
    for i, qa in enumerate(questions, start=1):
        LOGGER.info("[Q %d/%d] %s", i, len(questions), qa["question"][:80])
        response = run_kairix_prep(qa["question"], vault_path)
        judgment = judge_response(qa["question"], qa["ground_truth"], response)
        rows.append(
            {
                "conv_id": conv_id,
                "question": qa["question"],
                "ground_truth": qa["ground_truth"],
                "kairix_response": response,
                "judge_correct": judgment["correct"],
                "judge_score": judgment["score"],
                "judge_reasoning": judgment["reasoning"],
            }
        )
        LOGGER.info(
            "   -> correct=%s score=%.2f (%s)",
            judgment["correct"],
            judgment["score"],
            judgment["reasoning"][:100],
        )

    return rows


def write_results_csv(rows: list[dict[str, Any]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "conv_id",
        "question",
        "ground_truth",
        "kairix_response",
        "judge_correct",
        "judge_score",
        "judge_reasoning",
    ]
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_summary(rows: list[dict[str, Any]]) -> None:
    n = len(rows)
    if n == 0:
        print("No questions scored.")
        return
    pass_count = sum(1 for r in rows if r["judge_correct"])
    mean_score = sum(r["judge_score"] for r in rows) / n
    print()
    print("=" * 60)
    print("LoCoMo spike — summary")
    print("=" * 60)
    print(f"Questions scored : {n}")
    print(f"Pass rate        : {pass_count}/{n} ({100 * pass_count / n:.1f}%)")
    print(f"Mean judge score : {mean_score:.3f}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LoCoMo recall spike for kairix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--num-conversations", type=int, default=1)
    parser.add_argument("--max-questions", type=int, default=10)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument(
        "--locomo-json",
        type=Path,
        default=None,
        help="Local path to locomo10.json. Default: fetch from GitHub raw URL.",
    )
    parser.add_argument(
        "--reset-vault",
        action="store_true",
        help="Delete the vault root before starting. Recommended for spike runs.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.reset_vault and args.vault_root.exists():
        LOGGER.info("Resetting vault root %s", args.vault_root)
        shutil.rmtree(args.vault_root)
    args.vault_root.mkdir(parents=True, exist_ok=True)

    try:
        conversations = load_locomo(args.num_conversations, local_path=args.locomo_json)
    except Exception as exc:
        LOGGER.error("Failed to load LoCoMo dataset: %s", exc)
        LOGGER.error(
            "Either pass --locomo-json <path-to-locomo10.json> or ensure network access to %s.",
            LOCOMO_DATA_URL,
        )
        return 2

    all_rows: list[dict[str, Any]] = []
    for conv in conversations:
        try:
            rows = run_conversation(conv, args.vault_root, args.max_questions)
            all_rows.extend(rows)
        except Exception as exc:
            LOGGER.exception("Conversation run failed: %s", exc)

    write_results_csv(all_rows, args.output_csv)
    print_summary(all_rows)
    LOGGER.info("Wrote results to %s", args.output_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
