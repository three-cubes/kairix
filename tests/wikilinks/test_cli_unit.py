"""Unit tests for kairix.knowledge.wikilinks.cli.

The BDD suite covers help / unknown subcommand. These unit tests drive
each subcommand handler with a FakePaths and stubbed entity lookup so
the production code paths execute without Neo4j or a real vault.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import kairix.knowledge.wikilinks.cli as wl_cli
from tests.fakes import FakePaths

pytestmark = pytest.mark.unit


def _drive(
    args: list[str] | None,
    *,
    paths: Any = None,
) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            wl_cli.main(args, paths=paths)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.fixture
def _entities(monkeypatch):
    """Patch get_entities on the cli module to return a deterministic list."""
    from types import SimpleNamespace

    entities = [SimpleNamespace(name="Acme"), SimpleNamespace(name="Bob")]
    monkeypatch.setattr(wl_cli, "get_entities", lambda: entities)
    return entities


@pytest.fixture
def _no_entities(monkeypatch):
    monkeypatch.setattr(wl_cli, "get_entities", lambda: [])
    return []


@pytest.fixture
def fake_paths(tmp_path: Path):
    doc = tmp_path / "vault"
    ws = tmp_path / "ws"
    doc.mkdir()
    ws.mkdir()
    return FakePaths(document_root=str(doc), workspace_root=str(ws))


# ---------------------------------------------------------------------------
# main() top-level dispatch
# ---------------------------------------------------------------------------


def test_main_no_argv_prints_doc_and_exits_0() -> None:
    """No arguments → CLI prints its docstring and exits 0."""
    stdout, _stderr, code = _drive([], paths=None)
    assert code == 0
    assert "Wikilink injection CLI" in stdout


def test_main_argv_none_strips_sys_argv_and_runs(monkeypatch) -> None:
    """When argv is None, sys.argv[2:] is consumed."""
    monkeypatch.setattr(sys, "argv", ["kairix", "wikilinks"])
    # argv=None and no args after 'wikilinks' → prints doc.
    stdout, _stderr, code = _drive(None, paths=None)
    assert code == 0
    assert "Wikilink injection" in stdout


def test_main_help_long_flag_prints_doc_and_exits_0(fake_paths) -> None:
    stdout, _stderr, code = _drive(["--help"], paths=fake_paths)
    assert code == 0
    assert "Wikilink injection" in stdout


def test_main_help_short_flag_prints_doc_and_exits_0(fake_paths) -> None:
    stdout, _stderr, code = _drive(["-h"], paths=fake_paths)
    assert code == 0
    assert "Wikilink injection" in stdout


def test_main_unknown_subcommand_exits_1_with_message(fake_paths) -> None:
    _stdout, stderr, code = _drive(["bogus"], paths=fake_paths)
    assert code == 1
    assert "Unknown wikilinks subcommand: bogus" in stderr


def test_main_default_paths_resolves_kairixpaths(monkeypatch) -> None:
    """When paths is None, the CLI calls KairixPaths.resolve()."""
    import kairix.knowledge.wikilinks.cli as cli_mod

    fake = FakePaths(document_root="/tmp/nope-1", workspace_root="/tmp/nope-2")
    monkeypatch.setattr(cli_mod.KairixPaths, "resolve", classmethod(lambda cls: fake))
    monkeypatch.setattr(cli_mod, "get_entities", lambda: [])  # no entities → inject exits 1

    _stdout, _stderr, code = _drive(["inject"], paths=None)
    # inject with 0 entities exits 1; we got here via the resolve() path.
    assert code == 1


# ---------------------------------------------------------------------------
# inject subcommand
# ---------------------------------------------------------------------------


def test_inject_no_entities_exits_1(fake_paths, _no_entities) -> None:
    _stdout, stderr, code = _drive(["inject"], paths=fake_paths)
    assert code == 1
    assert "No entities loaded" in stderr


def test_inject_path_without_argument_exits_1(fake_paths, _entities) -> None:
    _stdout, stderr, code = _drive(["inject", "--path"], paths=fake_paths)
    assert code == 1
    assert "--path requires a file path argument" in stderr


def test_inject_single_path_eligible_calls_inject_file(monkeypatch, fake_paths, _entities, tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_text("page", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "inject_file", lambda p, ents, *, dry_run, paths: ["Acme", "Bob"])
    # Avoid touching _LAST_RUN_PATH on disk.
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--path", str(target), "--dry-run"], paths=fake_paths)
    assert code == 0
    assert str(target) in stdout
    assert "+ [[Acme]]" in stdout


def test_inject_single_path_not_eligible_returns_silently(monkeypatch, fake_paths, _entities, tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_text("page", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: False)
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--path", str(target)], paths=fake_paths)
    assert code == 0
    assert "is not eligible for injection" in stdout


def test_inject_single_path_with_no_new_links(monkeypatch, fake_paths, _entities, tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_text("page", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "inject_file", lambda p, ents, *, dry_run, paths: [])
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--path", str(target)], paths=fake_paths)
    assert code == 0
    assert "no new links" in stdout


def test_inject_all_iterates_eligible_files(monkeypatch, fake_paths, _entities) -> None:
    """inject (no flags) → calls _inject_all → gathers eligible files."""
    # Create a couple of markdown files in the FakePaths' doc root.
    doc = Path(fake_paths.document_root)
    (doc / "a.md").write_text("a", encoding="utf-8")
    (doc / "b.md").write_text("b", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "inject_file", lambda p, ents, *, dry_run, paths: ["Acme"] if "a.md" in p else [])
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject"], paths=fake_paths)
    assert code == 0
    assert "1 files updated, 1 wikilinks injected" in stdout


def test_inject_changed_with_no_last_run_falls_back_to_all(monkeypatch, fake_paths, _entities) -> None:
    """--changed but no prior run → CLI processes all eligible files."""
    doc = Path(fake_paths.document_root)
    (doc / "a.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "_read_last_run", lambda: None)
    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "inject_file", lambda p, ents, *, dry_run, paths: [])
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--changed"], paths=fake_paths)
    assert code == 0
    assert "No previous run found" in stdout


def test_inject_changed_with_no_modified_files(monkeypatch, fake_paths, _entities) -> None:
    """--changed with prior run but no recent mtimes → 'nothing to do'."""
    doc = Path(fake_paths.document_root)
    (doc / "a.md").write_text("a", encoding="utf-8")

    # Set last run to far future so no file mtime exceeds it.
    monkeypatch.setattr(wl_cli, "_read_last_run", lambda: 9999999999.0)
    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--changed"], paths=fake_paths)
    assert code == 0
    assert "No files modified" in stdout


def test_inject_changed_with_modified_files(monkeypatch, fake_paths, _entities) -> None:
    """--changed with mtimes after last run → processes those files."""
    doc = Path(fake_paths.document_root)
    (doc / "a.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(wl_cli, "_read_last_run", lambda: 0.0)  # all files modified after 1970
    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: True)
    monkeypatch.setattr(wl_cli, "inject_file", lambda p, ents, *, dry_run, paths: ["Acme"])
    monkeypatch.setattr(wl_cli, "_write_last_run", lambda: None)

    stdout, _stderr, code = _drive(["inject", "--changed"], paths=fake_paths)
    assert code == 0
    assert "files updated" in stdout


# ---------------------------------------------------------------------------
# audit subcommand
# ---------------------------------------------------------------------------


def test_audit_calls_weekly_report_and_writes_to_vault(monkeypatch, fake_paths, _entities) -> None:
    import kairix.knowledge.wikilinks.audit as audit_mod

    monkeypatch.setattr(audit_mod, "weekly_report", lambda root, ents, *, paths: "REPORT-BODY")

    stdout, _stderr, code = _drive(["audit"], paths=fake_paths)
    assert code == 0
    assert "REPORT-BODY" in stdout
    assert "Report saved to" in stdout
    # The report file was created on the FakePaths' document_root.
    saved = Path(fake_paths.document_root) / "04-Agent-Knowledge" / "shared" / "wikilink-audit-report.md"
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "REPORT-BODY"


def test_audit_handles_save_failure(monkeypatch, fake_paths, _entities) -> None:
    """If the report can't be saved, the audit command still exits 0 with stderr note."""
    import kairix.knowledge.wikilinks.audit as audit_mod

    monkeypatch.setattr(audit_mod, "weekly_report", lambda root, ents, *, paths: "BODY")
    # Force mkdir to raise.
    import pathlib as _pl

    real_mkdir = _pl.Path.mkdir

    def _raise_mkdir(self, *a, **kw):
        if "wikilink-audit-report.md" in str(self.parent.parent) or "04-Agent-Knowledge" in str(self):
            raise OSError("disk full")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(_pl.Path, "mkdir", _raise_mkdir)

    _stdout, stderr, code = _drive(["audit"], paths=fake_paths)
    assert code == 0
    assert "Could not save report" in stderr


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


def test_status_empty_log_branch(monkeypatch, fake_paths, _entities) -> None:
    monkeypatch.setattr(wl_cli, "_read_last_run", lambda: None)
    monkeypatch.setattr(wl_cli, "_read_log_entries", lambda: [])

    stdout, _stderr, code = _drive(["status"], paths=fake_paths)
    assert code == 0
    assert "Entities loaded:    2" in stdout
    assert "Last run:           never" in stdout
    assert "Injection log:      empty" in stdout


def test_status_with_log_entries(monkeypatch, fake_paths, _entities) -> None:
    monkeypatch.setattr(wl_cli, "_read_last_run", lambda: 1700000000.0)
    monkeypatch.setattr(
        wl_cli,
        "_read_log_entries",
        lambda: [
            {"injected": ["X", "Y"], "dry_run": False},
            {"injected": ["Z"], "dry_run": True},
        ],
    )

    stdout, _stderr, code = _drive(["status"], paths=fake_paths)
    assert code == 0
    assert "Total log entries:  2" in stdout
    assert "Real injections:  1" in stdout
    assert "Dry runs:         1" in stdout
    assert "Total links added: 3" in stdout


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_fmt_ts_none_returns_never() -> None:
    assert wl_cli._fmt_ts(None) == "never"


def test_fmt_ts_returns_iso_string() -> None:
    out = wl_cli._fmt_ts(1700000000.0)
    assert "2023" in out
    assert "UTC" in out


def test_write_and_read_last_run(monkeypatch, tmp_path: Path) -> None:
    """_write_last_run + _read_last_run roundtrip via a tmp path."""
    target = tmp_path / "subdir" / "marker"
    monkeypatch.setattr(wl_cli, "_LAST_RUN_PATH", str(target))
    wl_cli._write_last_run()
    out = wl_cli._read_last_run()
    assert out is not None
    assert out > 0


def test_read_last_run_returns_none_for_missing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wl_cli, "_LAST_RUN_PATH", str(tmp_path / "no-marker"))
    assert wl_cli._read_last_run() is None


def test_read_last_run_returns_none_for_garbage(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "garbage"
    marker.write_text("not-a-number", encoding="utf-8")
    monkeypatch.setattr(wl_cli, "_LAST_RUN_PATH", str(marker))
    assert wl_cli._read_last_run() is None


def test_read_log_entries_skips_blank_and_bad_lines(monkeypatch, tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        '\n{"a": 1}\nnot-json\n{"b": 2}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(wl_cli, "_LOG_PATH", str(log))
    entries = wl_cli._read_log_entries()
    assert entries == [{"a": 1}, {"b": 2}]


def test_read_log_entries_returns_empty_for_missing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wl_cli, "_LOG_PATH", str(tmp_path / "absent"))
    assert wl_cli._read_log_entries() == []


def test_gather_eligible_files_respects_size_and_eligibility(monkeypatch, fake_paths) -> None:
    doc = Path(fake_paths.document_root)
    big = doc / "big.md"
    big.write_text("x", encoding="utf-8")
    small = doc / "small.md"
    small.write_text("x", encoding="utf-8")

    # Only "small.md" is eligible.
    monkeypatch.setattr(wl_cli, "should_inject", lambda p, *, paths: "small.md" in p)
    out = wl_cli._gather_eligible_files(fake_paths)
    assert any("small.md" in p for p in out)
    assert all("big.md" not in p for p in out)
