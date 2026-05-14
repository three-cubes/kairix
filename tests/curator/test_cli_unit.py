"""Unit tests for the curator CLI's remaining branches.

The BDD layer covers --format/--staleness-days/health happy paths via an
injected FakeNeo4jClient. This module fills the unit gaps:

- the no-injection branch (``neo4j_client is None`` → real ``get_client``
  is called), driven by replacing the lazily-imported client module's
  factory with a stub so we touch the production import path without
  patching kairix internals.
- the ``--output FILE`` write-to-file branch.
- the ``__main__`` guard.
"""

from __future__ import annotations

import io
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.neo4j_mock import FakeNeo4jClient


def _drive(args: list[str], **kw: Any) -> tuple[str, str, int]:
    """Drive curator.cli.main, return (stdout, stderr, exit_code)."""
    from kairix.agents.curator.cli import main as curator_main

    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            curator_main(args, **kw)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.mark.unit
def test_health_writes_output_file_when_output_flag_given(tmp_path: Path) -> None:
    out_path = tmp_path / "report.md"
    stdout, _stderr, code = _drive(
        ["health", "--output", str(out_path), "--format", "text"],
        neo4j_client=FakeNeo4jClient(entities=[]),
    )
    assert code == 0
    assert out_path.exists(), "expected --output to create the file"
    body = out_path.read_text(encoding="utf-8")
    # Sanity: file isn't empty, but stdout only confirms the write.
    assert body, "report file was empty"
    assert f"Health report written to {out_path}" in stdout


@pytest.mark.unit
def test_health_writes_output_file_json_format(tmp_path: Path) -> None:
    out_path = tmp_path / "report.json"
    stdout, _stderr, code = _drive(
        ["health", "--output", str(out_path), "--format", "json"],
        neo4j_client=FakeNeo4jClient(entities=[]),
    )
    assert code == 0
    import json

    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert "Health report written to" in stdout


@pytest.mark.unit
def test_health_resolves_default_client_when_none_injected(monkeypatch) -> None:
    """When called with no neo4j_client kw, the CLI must call get_client.

    We swap the ``get_client`` symbol *on the kairix.knowledge.graph.client*
    module so the lazy import inside ``_health_cmd`` resolves to our fake
    factory. This is not patching kairix internals — we're configuring the
    boundary collaborator the production code is explicitly designed to
    look up by name.
    """
    import kairix.knowledge.graph.client as graph_client

    fake = FakeNeo4jClient(entities=[])
    monkeypatch.setattr(graph_client, "get_client", lambda: fake)

    stdout, _stderr, code = _drive(["health", "--format", "json"])
    assert code == 0
    # The fake produced a non-error report (no entities → empty health body).
    import json

    parsed = json.loads(stdout)
    assert isinstance(parsed, dict)


@pytest.mark.unit
def test_module_main_guard_runs_with_argv() -> None:
    """Execute the ``if __name__ == "__main__": main()`` block (line 93).

    runpy fakes the script-invocation path in-process. argv is set so the
    health subcommand parses; we expect the CLI to invoke main(), then
    its no-args path to raise SystemExit(2) (argparse).
    """
    old_argv = sys.argv
    try:
        sys.argv = ["kairix-curator"]  # missing required subcommand → exit 2
        err = io.StringIO()
        with pytest.raises(SystemExit) as info, redirect_stderr(err):
            runpy.run_module("kairix.agents.curator.cli", run_name="__main__")
        # argparse exits 2 when a required positional is missing.
        assert int(info.value.code or 0) == 2
    finally:
        sys.argv = old_argv
