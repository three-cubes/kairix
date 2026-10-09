"""CLI binding tests for `kairix warm`.

The Python API is tested in test_runner.py. These tests cover the CLI
shell: exit codes (0 ok, 1 partial failure), text vs JSON output, and
the affordance text in --help.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from kairix.platform.warm import cli as warm_cli
from kairix.platform.warm.cli import WarmCliDeps
from kairix.platform.warm.runner import WarmFailure, WarmResult, WarmStep

pytestmark = pytest.mark.unit


def _capture(argv: list[str], result: WarmResult) -> tuple[int, str]:
    """Run the CLI with ``result`` injected through the ``WarmCliDeps`` seam (F1)."""
    buf = io.StringIO()
    deps = WarmCliDeps(run_warm=lambda **_kwargs: result)
    with redirect_stdout(buf):
        rc = warm_cli.main(argv, deps=deps)
    return rc, buf.getvalue()


def _ok_result() -> WarmResult:
    return WarmResult(
        steps=[
            WarmStep(name="build_search_pipeline", ok=True, duration_s=0.5),
            WarmStep(name="probe_search", ok=True, duration_s=0.1),
            WarmStep(name="ensure_sqlite_stats", ok=True, duration_s=0.02, detail="stats already present, skipped"),
            WarmStep(name="open_graph_client", ok=True, duration_s=0.05),
        ],
        ok=True,
        total_duration_s=0.65,
    )


def _partial_failure_result() -> WarmResult:
    return WarmResult(
        steps=[
            WarmStep(name="build_search_pipeline", ok=True, duration_s=0.5),
            WarmStep(name="probe_search", ok=True, duration_s=0.1),
            WarmStep(name="ensure_sqlite_stats", ok=True, duration_s=0.02, detail="stats already present, skipped"),
            WarmStep(name="open_graph_client", ok=False, duration_s=0.0, detail="Neo4j unreachable"),
        ],
        failures=[WarmFailure(step="open_graph_client", detail="Neo4j unreachable")],
        ok=False,
        total_duration_s=0.6,
    )


def test_all_steps_ok_exits_zero() -> None:
    rc, stdout = _capture([], _ok_result())
    assert rc == 0
    assert "warm-up complete" in stdout


def test_partial_failure_exits_one_with_affordance() -> None:
    """A failing step exits 1 and emits the F21 affordance markers."""
    rc, stdout = _capture([], _partial_failure_result())
    assert rc == 1
    assert "warm-up partial" in stdout
    assert "fix:" in stdout
    assert "next:" in stdout
    assert "Neo4j unreachable" in stdout


def test_json_mode_emits_envelope() -> None:
    rc, stdout = _capture(["--json"], _ok_result())
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert len(payload["steps"]) == 4
    assert payload["total_duration_s"] == 0.65


def test_help_text_names_mcp_equivalent() -> None:
    """CLI --help must name the MCP equivalent (operational-tests design pattern 3)."""
    parser = warm_cli.build_parser()
    help_text = parser.format_help()
    assert "MCP equivalent:" in help_text
    assert "tool_warm" in help_text


def test_top_level_cli_dispatches_warm() -> None:
    """The top-level `kairix` CLI knows about the 'warm' command."""
    from kairix.cli import COMMANDS

    assert "warm" in COMMANDS
    module_path, fn_name, accepts_args = COMMANDS["warm"]
    assert module_path == "kairix.platform.warm.cli"
    assert fn_name == "main"
    assert accepts_args is True


# ---------------------------------------------------------------------------
# build_pipeline_builder_for_paths — F30 subprocess seam for --db-path /
# --document-root. The CLI returns None when no overrides are supplied
# (production callers leave the flags off; run_warm uses its default
# _step_build_pipeline) and a callable otherwise. The callable threads an
# explicit KairixPaths overlay into build_search_pipeline.
# ---------------------------------------------------------------------------


def test_pipeline_builder_returns_none_when_no_overrides() -> None:
    """Without --db-path or --document-root, the builder is None so the
    default ``_step_build_pipeline`` runs unchanged. Sabotage-proof: if
    the function ever returned a non-None callable here, production
    container start-up would invoke build_search_pipeline twice."""
    assert warm_cli.build_pipeline_builder_for_paths(None, None) is None


def test_pipeline_builder_returned_when_db_path_supplied(tmp_path) -> None:
    """A --db-path override yields a callable that build_search_pipeline
    can be invoked through."""
    builder = warm_cli.build_pipeline_builder_for_paths(str(tmp_path / "index.sqlite"), None)
    assert builder is not None
    assert callable(builder)


def test_pipeline_builder_returned_when_document_root_supplied(tmp_path) -> None:
    """A --document-root override alone yields a callable too."""
    builder = warm_cli.build_pipeline_builder_for_paths(None, str(tmp_path))
    assert builder is not None
    assert callable(builder)


def test_paths_overlay_threads_both_args(tmp_path) -> None:
    """The pure ``resolve_paths_overlay`` helper produces a KairixPaths
    whose db_path + document_root reflect the CLI args. The unset
    fields fall back to the resolved defaults so the override is
    additive.

    Sabotage: if the helper dropped one of the args on the floor (e.g.
    used defaults.db_path instead of the supplied db_path), the
    returned overlay would not match the tmp_path inputs.
    """
    db = tmp_path / "tmp-idx.sqlite"
    doc = tmp_path / "tmp-vault"
    doc.mkdir()

    overlay = warm_cli.resolve_paths_overlay(str(db), str(doc))
    assert overlay.db_path == db, f"db_path not threaded: {overlay.db_path!r} != {db!r}"
    assert overlay.document_root == doc, f"document_root not threaded: {overlay.document_root!r} != {doc!r}"


def test_paths_overlay_falls_back_to_defaults_for_unset_args(tmp_path) -> None:
    """When only --db-path is supplied, document_root falls back to the
    resolved default; and vice-versa. Confirms the overlay is additive,
    not destructive."""
    from kairix.paths import KairixPaths

    defaults = KairixPaths.resolve()
    db_only = warm_cli.resolve_paths_overlay(str(tmp_path / "x.sqlite"), None)
    assert db_only.document_root == defaults.document_root
    doc_only = warm_cli.resolve_paths_overlay(None, str(tmp_path / "vault"))
    assert doc_only.db_path == defaults.db_path
