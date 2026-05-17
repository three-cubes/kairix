"""
Unit tests for the kairix top-level CLI dispatch path (kairix/cli.py).

The BDD top-level scenarios cover ``--help``, ``-h``, ``--version`` and the
unknown-command branch. They don't drive the real importlib dispatch path
(lines 80-91) because every subcommand has side effects. This module fills
that gap by pointing kairix.cli.COMMANDS at small no-op handler modules
provided here, so the dispatcher's import + call branches execute in unit
mode.
"""

from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stderr, redirect_stdout

import pytest

import kairix.cli as kairix_cli


def _install_fake_handler_module(name: str, *, accepts_args: bool, record: list[object]) -> str:
    """Register a synthetic module with sys.modules and return its dotted path."""
    module_path = f"tests._fake_kairix_handlers.{name}"

    def fake_main(argv: list[str] | None = None) -> int | None:
        record.append(argv)
        return 0 if accepts_args else None

    def fake_main_noargs() -> None:
        record.append("called-noargs")

    mod = types.ModuleType(module_path)
    mod.main = fake_main if accepts_args else fake_main_noargs  # type: ignore[attr-defined] — dynamically-assigned module attribute on a synthetic ModuleType
    sys.modules[module_path] = mod
    return module_path


@pytest.fixture
def patched_dispatch_table():
    """Build a synthetic ``COMMANDS`` table for tests that pin dispatch routing.

    Returns ``(record_list, commands_dict)``. The recorder captures each
    fake handler's invocation; the commands_dict is passed via
    ``kairix.cli.main(commands=...)`` — the public DI seam — so the test
    drives the routing logic without monkey-patching the module attribute.
    """
    record: list[object] = []
    fake_table = {
        "fake-args": (
            _install_fake_handler_module("with_args", accepts_args=True, record=record),
            "main",
            True,
        ),
        "fake-noargs": (
            _install_fake_handler_module("without_args", accepts_args=False, record=record),
            "main",
            False,
        ),
        "fake-nonzero": (
            "tests._fake_kairix_handlers.nonzero",
            "main",
            True,
        ),
    }

    nonzero_mod = types.ModuleType("tests._fake_kairix_handlers.nonzero")

    def main_nonzero(argv: list[str] | None = None) -> int:
        record.append(("nonzero", argv))
        return 7

    nonzero_mod.main = main_nonzero  # type: ignore[attr-defined] — dynamically-assigned module attribute on a synthetic ModuleType
    sys.modules["tests._fake_kairix_handlers.nonzero"] = nonzero_mod

    return record, fake_table


def _drive_main(argv: list[str], monkeypatch, commands: dict | None = None) -> tuple[str, str, int]:
    monkeypatch.setattr(sys, "argv", ["kairix", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            kairix_cli.main(commands=commands)
    except SystemExit as exit_signal:
        code = int(exit_signal.code) if exit_signal.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.mark.unit
def test_dispatch_accepts_args_path_calls_handler_with_remaining_argv(patched_dispatch_table, monkeypatch):
    record, commands = patched_dispatch_table
    _stdout, _stderr, code = _drive_main(["fake-args", "--flag", "value"], monkeypatch, commands=commands)
    # main() returned None-equivalent (0) from the fake; no SystemExit raised.
    assert code == 0
    assert record == [["--flag", "value"]]


@pytest.mark.unit
def test_dispatch_accepts_args_nonzero_return_triggers_sys_exit(patched_dispatch_table, monkeypatch):
    record, commands = patched_dispatch_table
    _stdout, _stderr, code = _drive_main(["fake-nonzero", "arg"], monkeypatch, commands=commands)
    # The fake returns 7; the dispatcher must propagate via sys.exit(7).
    assert code == 7
    assert record == [("nonzero", ["arg"])]


@pytest.mark.unit
def test_dispatch_no_args_path_calls_handler_without_passing_argv(patched_dispatch_table, monkeypatch):
    record, commands = patched_dispatch_table
    # Extra CLI tokens after the subcommand must be ignored by the no-args path.
    _stdout, _stderr, code = _drive_main(["fake-noargs", "ignored"], monkeypatch, commands=commands)
    assert code == 0
    assert record == ["called-noargs"]


@pytest.mark.unit
def test_no_argv_at_all_exits_1_and_prints_help(monkeypatch):
    # len(sys.argv) == 1 (the program name only) → exit 1, doc printed.
    stdout, _stderr, code = _drive_main([], monkeypatch)
    assert code == 1
    assert "Subcommands:" in stdout


@pytest.mark.unit
def test_version_alias_exit_zero(monkeypatch):
    for flag in ("--version", "-V", "version"):
        stdout, _stderr, code = _drive_main([flag], monkeypatch)
        assert code == 0, f"flag {flag!r} did not exit 0"
        assert stdout.startswith("kairix "), f"flag {flag!r} did not print 'kairix '; got {stdout!r}"


@pytest.mark.unit
def test_unknown_command_prints_to_stderr_and_exits_1(patched_dispatch_table, monkeypatch):
    _record, commands = patched_dispatch_table
    _stdout, stderr, code = _drive_main(["definitely-unknown"], monkeypatch, commands=commands)
    assert code == 1
    assert "Unknown command: definitely-unknown" in stderr
    # The full docstring is appended for actionable help context.
    assert "Subcommands:" in stderr


@pytest.mark.unit
def test_help_long_flag_prints_doc_and_exits_0(monkeypatch):
    stdout, _stderr, code = _drive_main(["--help"], monkeypatch)
    assert code == 0
    assert "Subcommands:" in stdout


@pytest.mark.unit
def test_help_short_flag_prints_doc_and_exits_0(monkeypatch):
    stdout, _stderr, code = _drive_main(["-h"], monkeypatch)
    assert code == 0
    assert "Subcommands:" in stdout


@pytest.mark.unit
def test_main_module_guard_runs_main_when_executed_as_script(monkeypatch):
    """Drive the ``if __name__ == "__main__": main()`` guard at line 95.

    Importing under runpy with ``run_name="__main__"`` is the documented
    way to execute a module's __main__ block in-process without spawning
    a subprocess (uses the real import machinery).
    """
    import runpy

    monkeypatch.setattr(sys, "argv", ["kairix"])  # no args → exit 1 path
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc_info, redirect_stdout(out):
        runpy.run_module("kairix.cli", run_name="__main__")
    assert int(exc_info.value.code or 0) == 1
    assert "Subcommands:" in out.getvalue()
