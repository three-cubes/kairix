"""Step definitions for setup_cli.feature.

Drives ``kairix.platform.setup.cli.main`` and captures stdout, stderr,
and exit code. The non-interactive JSON scenario relies on the wizard's
documented contract that --non-interactive plus --preset plus --path
provides every input the wizard needs (no prompts, no live LLM check).
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

_DOCUMENTED_FLAGS = (
    "--output",
    "--non-interactive",
    "--json",
    "--preset",
    "--path",
)


@dataclass
class _SetupCliCtx:
    document_root: Path | None = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def setup_cli_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SetupCliCtx:
    # Setup wizard writes state under XDG_CONFIG_HOME — redirect to tmp.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CI", raising=False)
    return _SetupCliCtx()


@given("a temporary document root with one markdown file")
def _given_doc_root(setup_cli_ctx: _SetupCliCtx, tmp_path: Path) -> None:
    docroot = tmp_path / "docs"
    docroot.mkdir()
    (docroot / "hello.md").write_text("# Hello\n")
    setup_cli_ctx.document_root = docroot


def _run_setup(setup_cli_ctx: _SetupCliCtx, args: list[str]) -> None:
    from kairix.platform.setup.cli import main as setup_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            setup_main(args)
        setup_cli_ctx.exit_code = 0
    except SystemExit as e:
        setup_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    setup_cli_ctx.stdout = out.getvalue()
    setup_cli_ctx.stderr = err.getvalue()
    if "--json" in args:
        try:
            setup_cli_ctx.json_output = json.loads(setup_cli_ctx.stdout)
        except json.JSONDecodeError:
            setup_cli_ctx.json_output = {}


@when(parsers.parse("the operator runs the setup CLI with `{argv}`"))
def _run_setup_argv(setup_cli_ctx: _SetupCliCtx, argv: str) -> None:
    if setup_cli_ctx.document_root is not None:
        argv = argv.replace("TMP", str(setup_cli_ctx.document_root))
    _run_setup(setup_cli_ctx, shlex.split(argv))


@then(parsers.parse("the setup CLI exits with status {code:d}"))
def _assert_setup_exit(setup_cli_ctx: _SetupCliCtx, code: int) -> None:
    assert setup_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {setup_cli_ctx.exit_code}; "
        f"stdout={setup_cli_ctx.stdout[:200]!r} stderr={setup_cli_ctx.stderr[:200]!r}"
    )


@then("the help output names every documented flag")
def _assert_help_lists_flags(setup_cli_ctx: _SetupCliCtx) -> None:
    out = setup_cli_ctx.stdout + setup_cli_ctx.stderr
    for flag in _DOCUMENTED_FLAGS:
        assert flag in out, f"flag {flag!r} missing from --help output:\n{out}"


@then("stderr names the bad preset")
def _assert_stderr_names_bad_preset(setup_cli_ctx: _SetupCliCtx) -> None:
    assert "not-a-preset" in setup_cli_ctx.stderr, f"stderr did not name bad preset: {setup_cli_ctx.stderr!r}"


@then("the setup CLI stdout is parseable JSON")
def _assert_setup_json_parseable(setup_cli_ctx: _SetupCliCtx) -> None:
    assert setup_cli_ctx.json_output, (
        f"stdout was not parseable JSON; got {setup_cli_ctx.stdout[:500]!r} stderr={setup_cli_ctx.stderr[:200]!r}"
    )


@then(parsers.parse('the JSON config has a "{section}" section'))
def _assert_json_has_section(setup_cli_ctx: _SetupCliCtx, section: str) -> None:
    assert section in setup_cli_ctx.json_output, f"missing {section!r} in JSON output: {setup_cli_ctx.json_output}"
