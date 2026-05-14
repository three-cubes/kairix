"""Unit tests for ``kairix.agents.briefing.cli`` pure helpers + main orchestrator.

The CLI is a thin adapter — argv parsing + run_brief + stdout formatting.
Logic belongs to ``run_brief``. These tests drive each pure helper
directly and the ``main`` orchestrator with a ``BriefDeps`` injection.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from kairix.agents.briefing.cli import build_parser, format_output, main
from kairix.core.health import HealthDeps
from kairix.use_cases.brief import BriefDeps, BriefOutput

pytestmark = pytest.mark.unit


def _healthy_health_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_minimal_invocation() -> None:
    args = build_parser().parse_args(["builder"])
    assert args.agent == "builder"
    assert args.print_output is False
    assert args.memory_root is None


def test_build_parser_accepts_print_and_memory_root() -> None:
    args = build_parser().parse_args(["shape", "--print", "--memory-root", "/path/to/agents"])
    assert args.agent == "shape"
    assert args.print_output is True
    assert args.memory_root == "/path/to/agents"


def test_build_parser_requires_agent() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------


def test_format_output_short_content_returned_unchanged() -> None:
    out = BriefOutput(agent="builder", content="line 1\nline 2", path="/p", preview="line 1\nline 2")
    assert format_output(out, print_full=False) == "line 1\nline 2"


def test_format_output_long_content_truncates_with_remainder() -> None:
    lines = [f"line {i}" for i in range(45)]
    out = BriefOutput(agent="builder", content="\n".join(lines), path="/var/brief.md", preview="\n".join(lines[:30]))
    rendered = format_output(out, print_full=False)
    assert "line 0" in rendered
    assert "line 29" in rendered
    assert "line 30" not in rendered
    assert "15 more lines" in rendered
    assert "/var/brief.md" in rendered


def test_format_output_print_full_returns_complete_content() -> None:
    out = BriefOutput(agent="builder", content="\n".join([f"l {i}" for i in range(50)]))
    assert "l 49" in format_output(out, print_full=True)


def test_format_output_empty_content_returns_empty_string() -> None:
    out = BriefOutput(agent="x", content="", error="InvalidAgent: 'x'")
    assert format_output(out, print_full=False) == ""


# ---------------------------------------------------------------------------
# main orchestrator — driven through deps. No monkeypatch / no @patch.
# ---------------------------------------------------------------------------


def _build_deps(content: str = "x", out_dir: Path = Path("/tmp/brief")) -> BriefDeps:
    return BriefDeps(
        generate_fn=lambda agent, **_: content,
        briefing_dir_fn=lambda: out_dir,
        health_deps=_healthy_health_deps(),
    )


def _run(argv: list[str], deps: BriefDeps | None = None) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            main(argv, deps=deps)
    except SystemExit as e:  # NOSONAR — CLI test captures exit code; reraising would defeat the test
        exit_code = int(e.code) if e.code is not None else 0
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


def test_main_invalid_agent_exits_nonzero() -> None:
    exit_code, _stdout, stderr = _run(["rogue"], _build_deps())
    assert exit_code == 1
    assert "Error generating briefing" in stderr
    assert "InvalidAgent" in stderr


def test_main_happy_path_prints_path_and_preview() -> None:
    deps = _build_deps(
        content="\n".join([f"row {i}" for i in range(40)]),
        out_dir=Path("/tmp/brief"),
    )
    exit_code, stdout, stderr = _run(["builder"], deps)
    assert exit_code == 0
    assert "Briefing written to" in stderr
    assert "/tmp/brief/builder-latest.md" in stderr
    assert "row 0" in stdout
    assert "10 more lines" in stdout


def test_main_print_flag_emits_full_content() -> None:
    deps = _build_deps(content="\n".join([f"r{i}" for i in range(50)]))
    exit_code, stdout, _stderr = _run(["shape", "--print"], deps)
    assert exit_code == 0
    assert "r49" in stdout


# --memory-root operator flag is end-to-end behaviour covered at integration;
# no unit test here (would require env-var manipulation forbidden by F2/F4).
