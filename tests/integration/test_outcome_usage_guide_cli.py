"""F30 outcome test — ``kairix usage-guide`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the usage-guide CLI's
half of the paydown — the existing unit tests in
``tests/agents/usage_guide/test_cli.py`` continue to cover the
function-call seam (in-process ``deps=UsageGuideDeps(...)`` injection);
this test adds the F30-required subprocess outcome assertion.

The ``--guide-path`` flag is the F2-clean subprocess seam — it was
already on the CLI before this paydown (the use case accepts an
explicit ``Path`` and bypasses the production resolver), so the only
change in this commit is adding the outcome test and removing the
baseline entry.

Boundary chain exercised:

  subprocess([kairix, usage-guide, [topic], --guide-path <tmp.md>, --json])
    → kairix/agents/usage_guide/cli.py:main
    → kairix.use_cases.usage_guide.run_usage_guide
    → resolve_guide_fn (defaults — but guide_path arg short-circuits it)
    → read_text(<tmp.md>) → extract_topic_sections (when topic given)
    → usage_guide_output_to_envelope
    → JSON to stdout

Sabotage-proof: confirmed by mutating
``out = run_usage_guide(args.topic, guide_path=args.guide_path, deps=deps)``
to ``out = run_usage_guide(args.topic, guide_path=None, deps=deps)``
inside the CLI's ``main`` — the use case then resolves the production
guide path instead of the tmp file, the topic-section extractor finds
no match for the unique sentinel ``F30-OUTCOME-SENTINEL`` and the
content assertion fails. Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_GUIDE_FIXTURE = """# Usage guide fixture

## Search
Use ``kairix search`` to query the knowledge store.
F30-OUTCOME-SENTINEL search-section

## Budget
The agent has a budget per turn. Avoid wasting tokens.
F30-OUTCOME-SENTINEL budget-section
"""


def _seed_guide(tmp_path: Path) -> Path:
    """Write the minimal usage-guide markdown fixture and return its path."""
    guide = tmp_path / "agent-usage-guide.md"
    guide.write_text(_GUIDE_FIXTURE, encoding="utf-8")
    return guide


def test_usage_guide_cli_subprocess_topic_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix usage-guide`` binary surface against a tmp guide.

    Asserts on the JSON envelope content (topic + content + error fields)
    the agent harness consumes — NOT on returncode alone, NOT on
    internal fake call-counts. The F30 contract: subprocess + stdout
    envelope assertion.
    """
    guide = _seed_guide(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "usage-guide",
            "budget",
            "--guide-path",
            str(guide),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"usage-guide exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    assert envelope["topic"] == "budget", f"envelope: {envelope}"
    assert "budget-section" in envelope["content"], (
        f"expected the budget section in content; got: {envelope.get('content', '')[:200]!r}"
    )
    assert "search-section" not in envelope["content"], (
        f"topic filter leaked the search section: {envelope.get('content', '')[:200]!r}"
    )
    assert envelope["error"] == "", f"unexpected error: {envelope.get('error')!r}"


def test_usage_guide_cli_subprocess_bundled_default_returns_content() -> None:
    """Drive ``kairix usage-guide`` with NO ``--guide-path`` — the bundled
    package-data guide (#466) must resolve out-of-box and return content.

    This is the production default: no operator action, no ``onboard
    guide`` step, no ``docs/`` copy. The subprocess runs the real
    installed package, so it proves the bundled guide ships and resolves
    end-to-end through the binary surface.

    Sabotage proof (executed locally): point ``_GUIDE_RESOURCE`` in
    ``kairix/use_cases/usage_guide.py`` at a non-existent ``data/`` file —
    the resolver no longer finds the bundled copy, the CLI prints the
    ``UsageGuideNotFound`` error and exits 1, so the ``returncode == 0`` +
    error-empty assertions fail. Restored.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "usage-guide",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"usage-guide (bundled default) exited {proc.returncode}\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    envelope = json.loads(proc.stdout)
    assert envelope["error"] == "", f"bundled default returned an error: {envelope.get('error')!r}"
    assert envelope["content"].strip(), f"bundled guide content was empty: {envelope!r}"


def test_usage_guide_cli_subprocess_missing_guide_emits_error(tmp_path: Path) -> None:
    """Pointing ``--guide-path`` at a non-existent file must surface a
    non-zero exit + a parseable error message on stdout (the use case
    returns ``UsageGuideNotFound`` and the text formatter prefixes it
    with ``error:``). Closes the binary-surface error path that the unit
    tests cover only in-process.
    """
    bogus = tmp_path / "does-not-exist.md"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "usage-guide",
            "--guide-path",
            str(bogus),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1 for missing guide, got {proc.returncode}. stderr={proc.stderr!r}"
    assert "error:" in proc.stdout, f"stdout missing 'error:' prefix: {proc.stdout!r}"
    assert "UsageGuideNotFound" in proc.stdout, f"stdout missing UsageGuideNotFound class name: {proc.stdout!r}"


def _bundled_guide_topic(topic: str) -> dict[str, object]:
    """Drive the real ``kairix usage-guide <topic>`` binary against the bundled
    guide (NO ``--guide-path``) and return the parsed JSON envelope.

    Proves the shipped guide — the one a self-training agent reads — surfaces
    the requested section, not merely a tmp fixture.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "usage-guide", topic, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"usage-guide {topic!r} exited {proc.returncode}\n--- stderr ---\n{proc.stderr}"
    envelope: dict[str, object] = json.loads(proc.stdout)
    assert envelope["error"] == "", f"unexpected error for topic {topic!r}: {envelope.get('error')!r}"
    return envelope


def test_bundled_guide_surfaces_expand_affordance() -> None:
    """PLA-299 F30 outcome: the bundled guide surfaces the ``expand`` capability
    through the real CLI, so a self-training agent discovers the
    ``search → expand`` retrieval loop.

    Sabotage-proof (executed): delete the `tool_expand` / `kairix expand`
    mentions from kairix/agents/usage_guide/data/agent-usage-guide.md — the
    topic slice no longer contains either token and this assertion fails.
    Restored.
    """
    content = str(_bundled_guide_topic("expand")["content"])
    assert "kairix expand" in content or "tool_expand" in content, (
        f"bundled guide's 'expand' slice must name the expand affordance; got: {content[:300]!r}"
    )


def test_bundled_guide_surfaces_write_loop() -> None:
    """PLA-299 F30 outcome: the bundled guide surfaces the write loop
    (``remember`` + ``ingest_chat``) through the real CLI.

    Sabotage-proof (executed): remove the "### Write loop" section from
    agent-usage-guide.md — the topic slice loses the remember/ingest-chat
    invocations and this assertion fails. Restored.
    """
    content = str(_bundled_guide_topic("write")["content"])
    assert "kairix remember" in content, f"write loop must name `kairix remember`; got: {content[:300]!r}"
    assert "kairix ingest-chat" in content, f"write loop must name `kairix ingest-chat`; got: {content[:300]!r}"
