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
    state_path: Path
    document_root: Path | None = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def setup_cli_ctx(tmp_path: Path) -> _SetupCliCtx:
    return _SetupCliCtx(state_path=tmp_path / ".setup-state.json")


@given("a temporary document root with one markdown file")
def _given_doc_root(setup_cli_ctx: _SetupCliCtx, tmp_path: Path) -> None:
    docroot = tmp_path / "docs"
    docroot.mkdir()
    (docroot / "hello.md").write_text("# Hello\n")
    setup_cli_ctx.document_root = docroot


def _run_setup(setup_cli_ctx: _SetupCliCtx, args: list[str]) -> None:
    from kairix.platform.setup.cli import main as setup_main
    from kairix.platform.setup.prompts import SetupContext

    # Construct a deterministic SetupContext directly so the wizard
    # never reads $XDG_CONFIG_HOME, $CI, or sys.stdout.isatty(). Mirrors
    # how prod main() builds it from --non-interactive / --json, but
    # without env-var I/O.
    ctx = SetupContext(
        interactive=False,
        json_mode="--json" in args,
        state_path=setup_cli_ctx.state_path,
    )

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            setup_main(args, ctx=ctx)
        setup_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
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


@then('the JSON config "paths.document_root" matches the supplied path')
def _assert_paths_document_root(setup_cli_ctx: _SetupCliCtx) -> None:
    assert "paths" in setup_cli_ctx.json_output, f"missing 'paths' in JSON: {setup_cli_ctx.json_output}"
    paths = setup_cli_ctx.json_output["paths"]
    assert isinstance(paths, dict), f"'paths' must be an object, got {type(paths).__name__}"
    assert "document_root" in paths, f"missing 'document_root' in paths: {paths}"
    assert setup_cli_ctx.document_root is not None, (
        "fixture didn't set document_root — feature is missing the Given step"
    )
    assert paths["document_root"] == str(setup_cli_ctx.document_root), (
        f"expected paths.document_root={str(setup_cli_ctx.document_root)!r}; got {paths['document_root']!r}"
    )


@then(parsers.parse('the JSON config "{section}" section is a non-empty object'))
def _assert_section_non_empty_object(setup_cli_ctx: _SetupCliCtx, section: str) -> None:
    assert section in setup_cli_ctx.json_output, f"missing {section!r}: {setup_cli_ctx.json_output}"
    value = setup_cli_ctx.json_output[section]
    assert isinstance(value, dict), f"{section!r} must be an object, got {type(value).__name__}"
    assert value, f"{section!r} must be non-empty, got {value!r}"
