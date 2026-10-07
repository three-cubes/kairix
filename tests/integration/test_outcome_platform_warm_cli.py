"""F30 outcome test — ``kairix warm`` subprocess surface.

Wave 0 paydown (Group E). Closes the F30 gap on
``kairix/platform/warm/cli.py``: existing unit + BDD tests drive
``run_warm()`` in-process with injected fakes; this test adds the
F30-required subprocess outcome assertion against the real warm CLI
binary surface.

The warm CLI gains ``--db-path PATH`` and ``--document-root PATH`` here
so the subprocess test can drive the warm-up against a tmp sandbox
without setting any ``KAIRIX_*`` env vars (F2-clean by construction).
The CLI threads both args into a :class:`kairix.paths.KairixPaths`
overlay and passes that to ``build_search_pipeline(paths=...)`` via
the ``WarmDeps.build_pipeline`` seam on ``run_warm``.

Boundary chain exercised:

  subprocess([kairix, warm, --json,
              --db-path <tmp>/index.sqlite,
              --document-root <tmp>])
    → kairix/cli.py dispatch
    → kairix/platform/warm/cli.py:main
    → _build_pipeline_builder_for_paths → KairixPaths overlay
    → run_warm(deps=WarmDeps(build_pipeline=<lambda using overlay>))
    → JSON envelope to stdout

Outcome assertion: the warm envelope is well-formed JSON with the
canonical warm-up step names (build_search_pipeline, probe_search,
warm_cross_encoder, ensure_sqlite_stats, open_graph_client) and every
required field per ``WarmResult.to_envelope``.
The build step lands ``ok=false`` in this CI-clean tmp environment
because no ``kairix.config.yaml`` provider is configured; that's
deliberate — the outcome test confirms the envelope SHAPE the operator
reads, not the warm-up's success against a deployed kairix.

Sabotage-proof (executed locally):
    Removed the ``--db-path`` argparse entry from ``_build_parser`` —
    the subprocess then exited with ``returncode == 2`` and stderr
    "unrecognized arguments: --db-path ...", so the assertion on a
    parseable JSON envelope failed. Restored after observing the
    failure.

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


_EXPECTED_STEP_NAMES = {
    "build_search_pipeline",
    "probe_search",
    "warm_cross_encoder",
    "open_graph_client",
    "ensure_sqlite_stats",
}


def test_warm_cli_subprocess_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix warm`` binary surface against a tmp sandbox.

    Asserts on the JSON envelope content operators + healthchecks
    consume — NOT on returncode alone, NOT on internal fake call-counts.
    F30 contract: subprocess + stdout assertion.

    The envelope is well-formed regardless of whether the underlying
    warm-up succeeded against the tmp sandbox; that's the point — the
    outcome test pins the binary's REPORT shape, not the deploy state.
    """
    sandbox = tmp_path / "kairix-sandbox"
    sandbox.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "warm",
            "--json",
            "--db-path",
            str(sandbox / "index.sqlite"),
            "--document-root",
            str(sandbox),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    # In a tmp sandbox the build_search_pipeline step lands ok=False
    # (no provider configured), so returncode is 1 by design. The F30
    # contract is on stdout content, not the exit code.
    assert proc.returncode in (0, 1), (
        f"unexpected exit {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    # Top-level envelope shape — matches WarmResult.to_envelope.
    assert set(envelope.keys()) >= {"ok", "total_duration_s", "steps", "failures"}, (
        f"envelope missing required keys: {sorted(envelope.keys())}"
    )
    assert isinstance(envelope["steps"], list) and envelope["steps"], f"steps not a non-empty list: {envelope!r}"

    # Each step carries the WarmStep field shape.
    step_names = {s["name"] for s in envelope["steps"]}
    assert step_names == _EXPECTED_STEP_NAMES, (
        f"step name set mismatch: got {step_names!r}, expected {_EXPECTED_STEP_NAMES!r}"
    )
    for step in envelope["steps"]:
        assert set(step.keys()) >= {"name", "ok", "duration_s", "detail"}, (
            f"step missing required keys: {sorted(step.keys())}"
        )
        assert isinstance(step["ok"], bool), f"step.ok not bool: {step!r}"
        assert isinstance(step["duration_s"], (int, float)), f"step.duration_s not numeric: {step!r}"


def test_warm_cli_subprocess_emits_text_report_without_json_flag(tmp_path: Path) -> None:
    """Without ``--json``, the CLI emits the human-readable operator
    report to stdout. Asserts on the canonical text-mode markers so a
    regression that drops the per-step lines (or the header) trips here.

    Closes the binary-surface text-mode contract the unit tests cover
    only in-process by calling ``_format_text`` directly.
    """
    sandbox = tmp_path / "kairix-sandbox"
    sandbox.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "warm",
            "--db-path",
            str(sandbox / "index.sqlite"),
            "--document-root",
            str(sandbox),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    stdout = proc.stdout
    # Header line.
    assert stdout.startswith("warm: total="), f"stdout missing header: {stdout[:200]!r}"
    # Every step name appears in the per-step section.
    for name in _EXPECTED_STEP_NAMES:
        assert name in stdout, f"step name {name!r} missing from text report: {stdout!r}"
