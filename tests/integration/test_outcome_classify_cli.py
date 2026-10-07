"""F30 outcome test — ``kairix classify`` subprocess surface.

Wave 0 Group C paydown for ``kairix/core/classify/cli.py``. The
classify CLI is the rule-driven path-routing surface: agents pipe a
content snippet in and the CLI emits a JSON envelope picking the
target document path. Operators run this on the command line and
agents call it from automation; both consume the stdout JSON.

``classify`` is rule-based for deterministic patterns (normative
language → procedural-rule; ``decided:`` → semantic-decision; etc.),
so the happy-path test drives the real rule classifier with
``--no-llm`` set — no network, no LLM, no env-var override. The
error-path test drives the invalid-agent guard, which short-circuits
before the classifier runs.

F2-clean by construction: content is passed as a positional argument
and ``--no-llm`` disables the LLM fallback. No ``KAIRIX_*`` env vars
are set in the subprocess invocation.

Boundary chain exercised (happy path):

  subprocess([kairix, classify, '<content>', --agent, shared, --no-llm])
    → kairix/core/classify/cli.py:main
    → classify_content(content, agent="shared")
    → ClassificationResult(type="procedural-rule", target_path=..., ...)
    → json.dumps({"type": "procedural-rule", ...}) → stdout

Sabotage-proof anchor: replacing ``print(json.dumps(output))`` with a
silent return makes the happy-path stdout JSON assertion fail. For
the error path, removing the f"Error: invalid agent {agent!r}..."
print → the stderr-content assertion fails. Tested locally.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_classify_cli_subprocess_emits_json_envelope_for_rule_hit() -> None:
    """Drive ``kairix classify`` with rule-hitting content; assert on stdout JSON.

    "never commit ..." hits the procedural-rule normative-language
    pattern; classify_content returns ``type=procedural-rule`` with
    high confidence. The CLI emits the JSON envelope downstream
    consumers parse — agents, MCP bridges, shell pipelines.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "classify",
            "never commit secrets to the repository",
            "--agent",
            "builder",
            "--no-llm",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"classify exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    assert envelope["type"] == "procedural-rule", f"unexpected type: {envelope}"
    assert "target_path" in envelope, f"target_path missing: {envelope}"
    assert envelope["confidence"] >= 0.5, f"low-confidence rule hit: {envelope}"
    assert "reason" in envelope, f"reason missing: {envelope}"


def test_classify_cli_subprocess_rejects_unknown_agent() -> None:
    """An agent name outside VALID_AGENTS → exit 1 + actionable stderr.

    The CLI must short-circuit before invoking the classifier so a
    typo'd ``--agent`` value doesn't get partway through the pipeline.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "classify",
            "something to classify",
            "--agent",
            "not-a-real-agent",
            "--no-llm",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1 for invalid agent; got {proc.returncode}, stderr={proc.stderr!r}"
    assert "not-a-real-agent" in proc.stderr, f"stderr should name the invalid agent: {proc.stderr!r}"
    assert "invalid agent" in proc.stderr.lower(), f"stderr should mention 'invalid agent': {proc.stderr!r}"
