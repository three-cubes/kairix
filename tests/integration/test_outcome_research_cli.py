"""F30 outcome test — ``kairix research`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the research CLI's
half of the paydown — the existing unit tests in
``tests/research/test_cli.py`` continue to cover the function-call
seam (in-process ``deps=ResearchDeps(...)`` injection); this test adds
the F30-required subprocess outcome assertion.

The research use case has no filesystem state (it routes through
LangGraph + the embed/search stack) — there is no ``--document-root``
analogue to add. F30 paydown for this surface is purely the outcome
test; the production CLI is unchanged.

Boundary chain exercised (happy ``--help`` path):

  subprocess([kairix, research, --help])
    → kairix/agents/research/cli.py:build_parser
    → argparse prints usage → exit 0
    → stdout names the ``query`` positional + ``--max-turns`` + ``--json``

Boundary chain exercised (no-LLM error envelope):

  subprocess([kairix, research, "<query>", --json])
    → kairix/agents/research/cli.py:main
    → kairix.use_cases.research.run_research_use_case
    → kairix.agents.research.graph.run_research
    → LangGraph invoke raises (config / provider not resolvable)
    → run_research catches, returns the error envelope shape
    → research_output_to_envelope → JSON to stdout → exit 1

The error envelope shape (query / synthesis / gaps / confidence / turns
/ error) is the contract the operator's tooling parses. Asserting on
its structure — not just returncode — is the F30 requirement.

Sabotage-proof (both executed):
  (a) Mutated ``kairix/agents/research/cli.py``'s JSON-printing branch
      ``print(json.dumps(research_output_to_envelope(out), indent=2))``
      to ``print("{}")`` — the JSON envelope test then fails to find
      ``query`` / ``error`` keys in the parsed envelope. Restored.
  (b) Mutated the argparse description from
      ``"Iterative research over the knowledge store with LLM synthesis."``
      to ``"MUTATED-SABOTAGE-DESCRIPTION"`` — the help test's
      ``"Iterative research" in proc.stdout`` assertion fails. Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def _subprocess_env_without_llm_keys() -> dict[str, str]:
    """Return a subprocess env that strips LLM credential vars.

    Determinism rationale: the research path can hit a live LLM if the
    developer's shell has a real key. Stripping the keys forces the
    graceful error-envelope branch every time the test runs locally or
    in CI. Passing an explicit env dict to ``subprocess.run`` is F2-
    clean (F2 targets pytest monkeypatch on KAIRIX_* keys, not the
    subprocess ``env=`` parameter).
    """
    env = dict(os.environ)
    env.pop("KAIRIX_LLM_API_KEY", None)
    env.pop("KAIRIX_AZURE_API_KEY", None)
    return env


def test_research_cli_subprocess_help_outcome() -> None:
    """``kairix research --help`` exits 0 with the usage block on stdout.

    Proves the binary surface boots, the argparse tree is intact, and
    the operator-facing flag list reaches stdout. F30 contract:
    subprocess + stdout content assertion.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "research", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_subprocess_env_without_llm_keys(),
    )

    assert proc.returncode == 0, (
        f"research --help exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "Iterative research" in proc.stdout, f"help text missing description: {proc.stdout!r}"
    assert "--max-turns" in proc.stdout, f"help text missing --max-turns: {proc.stdout!r}"
    assert "--json" in proc.stdout, f"help text missing --json: {proc.stdout!r}"
    assert "query" in proc.stdout, f"help text missing query positional: {proc.stdout!r}"


def test_research_cli_subprocess_no_provider_json_envelope_outcome() -> None:
    """With no LLM credential, ``kairix research <query> --json`` must
    emit the well-formed error envelope on stdout and exit 1.

    Asserts on the JSON envelope's required keys (query / synthesis /
    gaps / confidence / turns / error) — the contract the operator's
    tooling parses. NOT a returncode-only assertion.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "research", "what is the F30 contract?", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_subprocess_env_without_llm_keys(),
    )

    assert proc.returncode == 1, (
        f"expected exit 1 (no-LLM error path), got {proc.returncode}.\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    # Envelope contract — the agent harness parses these keys.
    for key in ("query", "synthesis", "retrieved_chunks", "gaps", "confidence", "turns", "error"):
        assert key in envelope, f"missing {key!r} key in envelope: {sorted(envelope.keys())}"
    assert envelope["query"] == "what is the F30 contract?", f"query echoed wrong: {envelope}"
    assert envelope["error"], f"expected non-empty error on no-LLM path: {envelope}"
    assert envelope["synthesis"] == "", f"expected empty synthesis on error path: {envelope!r}"
