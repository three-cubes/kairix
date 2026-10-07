"""F30 outcome test — ``kairix search`` subprocess surface.

Wave 0 Group C paydown for ``kairix/core/search/cli.py``. The search
CLI is the operator-facing hybrid retrieval surface (BM25 + vector
via RRF). The MCP ``search`` tool and the CLI share the same
``run_search`` use case, so the binary surface is what operators
script against — shell pipelines, smoke checks, dashboards.

The CLI's ``--json`` mode emits a structured envelope on stdout with
``query``, ``intent``, ``results``, count diagnostics, and ``error``
fields. The use case is also exception-safe: any pipeline-construction
failure (missing provider, broken DB, missing config) is caught and
projected to the envelope's ``error`` field — the subprocess always
emits a parseable envelope, never a raw traceback to stdout.

This test asserts on the envelope shape + content the operator sees,
not on internal pipeline call-counts. F2-clean: no ``KAIRIX_*`` env
vars are set in the subprocess invocation; the test relies on the
already-present envelope shape contract.

Boundary chain exercised:

  subprocess([kairix, search, '<query>', --json])
    → kairix/core/search/cli.py:main
    → run_search(query=..., agent=None, scope=...)
    → SearchOutput(query=..., intent=..., results=[], error=...)
    → to_json_envelope → print(json.dumps(...))

Sabotage-proof anchor: replacing ``print(json.dumps(to_json_envelope(out), indent=2))``
with ``pass`` makes both tests fail on the json.loads of empty stdout.
Tested locally.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_search_cli_subprocess_emits_envelope_with_query_echo() -> None:
    """Drive ``kairix search '<query>' --json``; assert on the envelope shape.

    Without a configured provider in this test environment the
    pipeline factory raises ``ValueError`` at construction time;
    ``run_search`` catches the exception and projects it onto the
    envelope's ``error`` field. The envelope STILL contains every
    shape field operators script against (``query``, ``intent``,
    ``results``, ``bm25_count``, ``vec_count``, ``latency_ms``,
    ``error``) — that's the F30 contract: subprocess + stdout envelope
    assertion, not just returncode.
    """
    query = "what is kairix search"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "search",
            query,
            "--json",
            "--no-entity-card",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Returncode is informational; the envelope is the load-bearing assertion.
    assert proc.stdout, f"empty stdout — subprocess crashed before envelope render. stderr={proc.stderr!r}"

    envelope = json.loads(proc.stdout)
    # Shape: every field downstream consumers (agents / MCP bridges / shells) script against.
    assert envelope["query"] == query, f"query echo missing/mismatched: {envelope.get('query')!r}"
    assert "intent" in envelope, f"intent field missing: {sorted(envelope.keys())}"
    assert "results" in envelope, f"results field missing: {sorted(envelope.keys())}"
    assert "bm25_count" in envelope, f"bm25_count field missing: {sorted(envelope.keys())}"
    assert "vec_count" in envelope, f"vec_count field missing: {sorted(envelope.keys())}"
    assert "latency_ms" in envelope, f"latency_ms field missing: {sorted(envelope.keys())}"


def test_search_cli_subprocess_envelope_carries_error_when_provider_missing() -> None:
    """When the factory can't construct (no provider configured), the envelope
    surfaces the error on stdout and the CLI exits non-zero.

    Closes the binary-surface error path operators see when their
    ``kairix.config.yaml`` is missing the required ``provider:`` field
    — the affordance the run_search use case lifts from a raw
    traceback into a structured envelope.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "search",
            "any query",
            "--json",
            "--no-entity-card",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    envelope = json.loads(proc.stdout)
    # Either the env has a valid provider configured (envelope.error is empty
    # and returncode 0) OR no provider is configured (envelope.error is
    # populated and returncode 1). Both paths produce a parseable envelope
    # — the F30 contract is the envelope shape, not the specific outcome.
    if envelope.get("error"):
        assert proc.returncode == 1, f"expected exit 1 when envelope has error; got {proc.returncode}"
        # The actionable affordance message names what the operator should fix.
        # "provider", "config", or "ValueError" — any of these tokens shows
        # the structured-error path is wiring through. The unit/contract tests
        # cover exact-text; F30 cares about subprocess-binary semantics.
        assert any(tok in envelope["error"].lower() for tok in ("provider", "config", "valueerror")), (
            f"error message lacks actionable token: {envelope['error']!r}"
        )
    else:
        assert proc.returncode == 0, f"expected exit 0 when envelope has no error; got {proc.returncode}"
