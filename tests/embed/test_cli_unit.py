"""Unit tests for kairix/core/embed/cli.py.

The BDD layer covers ``--help`` / argparse rejection. These unit tests
drive each ``cmd_*`` exit-code mapping and the dispatcher in main(),
using the ``EmbedCliDeps`` injection seam where the production code
constructs heavy collaborators.

No real DB, no Azure, no lockfile, no FTS rebuild.
"""

from __future__ import annotations

import argparse
import io
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import kairix.core.embed.cli as embed_cli
from kairix.core.embed.cli import (
    EmbedCliDeps,
    acquire_lock,
    cmd_embed,
    cmd_rebuild_fts,
    cmd_recall,
    cmd_status,
    release_lock,
    setup_logging,
)
from kairix.core.embed.cli import (
    main as embed_main,
)
from kairix.core.embed.use_cases import EmbedPipelineResult


def _make_args(
    *,
    force: bool = False,
    limit: int | None = None,
    batch_size: int = 1,
    skip_recall_check: bool = False,
    skip_summarise: bool = True,
    rebuild_canaries: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        force=force,
        limit=limit,
        batch_size=batch_size,
        skip_recall_check=skip_recall_check,
        skip_summarise=skip_summarise,
        rebuild_canaries=rebuild_canaries,
    )


def _result(**kw: Any) -> EmbedPipelineResult:
    defaults = dict(
        embedded=0,
        failed=0,
        skipped=0,
        duration_s=0.0,
        cost_usd=0.0,
        db_path="/tmp/k.db",
        timestamp=0,
        recall_score=None,
        recall_passed=None,
        recall_alert=None,
        scan_new=0,
        scan_updated=0,
        scan_errors=0,
        diagnostics=[],
    )
    defaults.update(kw)
    return EmbedPipelineResult(**defaults)


def _deps_returning(result: EmbedPipelineResult, *, post_calls: list[bool] | None = None) -> EmbedCliDeps:
    captured: list[bool] = post_calls if post_calls is not None else []

    def runner(**_kwargs: Any) -> EmbedPipelineResult:
        return result

    return EmbedCliDeps(
        pipeline_runner_factory=lambda: runner,
        post_embed_summarise=lambda: captured.append(True),
    )


def _deps_raising(exc: Exception, *, post_calls: list[bool] | None = None) -> EmbedCliDeps:
    captured: list[bool] = post_calls if post_calls is not None else []

    def runner(**_kwargs: Any) -> EmbedPipelineResult:
        raise exc

    return EmbedCliDeps(
        pipeline_runner_factory=lambda: runner,
        post_embed_summarise=lambda: captured.append(True),
    )


# ---------------------------------------------------------------------------
# cmd_embed exit-code mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_embed_returns_0_on_clean_success() -> None:
    deps = _deps_returning(_result(embedded=10, recall_passed=True, recall_score=0.95))
    assert cmd_embed(_make_args(), deps=deps) == 0


@pytest.mark.unit
def test_cmd_embed_returns_2_when_pipeline_raises() -> None:
    deps = _deps_raising(RuntimeError("db gone"))
    assert cmd_embed(_make_args(), deps=deps) == 2


@pytest.mark.unit
def test_cmd_embed_returns_1_when_failed_chunks_present() -> None:
    deps = _deps_returning(_result(embedded=3, failed=2, recall_passed=True, recall_score=0.9))
    # failed > 0 → success=False → return 1
    assert cmd_embed(_make_args(), deps=deps) == 1


@pytest.mark.unit
def test_cmd_embed_returns_1_when_recall_gate_failed() -> None:
    deps = _deps_returning(_result(embedded=5, recall_passed=False, recall_score=0.4))
    assert cmd_embed(_make_args(), deps=deps) == 1


@pytest.mark.unit
def test_cmd_embed_logs_skip_recall_message_when_flag_set(caplog) -> None:
    import logging as _log

    deps = _deps_returning(_result(embedded=5))
    with caplog.at_level(_log.INFO):
        cmd_embed(_make_args(skip_recall_check=True), deps=deps)
    assert any("Skipping recall check" in r.message for r in caplog.records)


@pytest.mark.unit
def test_cmd_embed_calls_post_embed_summarise_when_not_skipped() -> None:
    post_calls: list[bool] = []
    deps = _deps_returning(_result(embedded=1), post_calls=post_calls)
    cmd_embed(_make_args(skip_summarise=False), deps=deps)
    assert post_calls == [True]


@pytest.mark.unit
def test_cmd_embed_skips_post_summarise_when_skip_flag_set() -> None:
    post_calls: list[bool] = []
    deps = _deps_returning(_result(embedded=1), post_calls=post_calls)
    cmd_embed(_make_args(skip_summarise=True), deps=deps)
    assert post_calls == []


# ---------------------------------------------------------------------------
# cmd_recall
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_recall_returns_0_when_gate_passes(monkeypatch, capsys) -> None:
    import kairix.core.embed.cli as cli_mod

    def _fake_run_recall_gate() -> tuple[bool, dict[str, Any]]:
        return (
            True,
            {
                "passed": 4,
                "total": 5,
                "score": 0.8,
                "detail": [
                    {"id": "q1", "query": "first query", "hit": True},
                    {"id": "q2", "query": "second query", "hit": False},
                ],
            },
        )

    monkeypatch.setattr(cli_mod, "run_recall_gate", _fake_run_recall_gate)
    rc = cmd_recall(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Recall: 4/5 (80%)" in out
    # Detail lines emitted.
    assert "✓" in out
    assert "✗" in out


@pytest.mark.unit
def test_cmd_recall_returns_1_when_gate_fails(monkeypatch) -> None:
    import kairix.core.embed.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "run_recall_gate",
        lambda: (False, {"passed": 1, "total": 5, "score": 0.2, "detail": []}),
    )
    assert cmd_recall(argparse.Namespace()) == 1


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple:
        return self._rows.pop(0)


class _FakeDb:
    def __init__(self, *, total_vecs: int, total_docs: int) -> None:
        self.total_vecs = total_vecs
        self.total_docs = total_docs
        self.closed = False

    def execute(self, sql: str) -> _FakeCursor:
        if "content_vectors" in sql:
            return _FakeCursor([(self.total_vecs,)])
        if "documents WHERE active=1" in sql:
            return _FakeCursor([(self.total_docs,)])
        raise AssertionError(f"unexpected sql: {sql}")

    def close(self) -> None:
        self.closed = True


@pytest.mark.unit
def test_cmd_status_prints_counters_and_returns_0(monkeypatch, capsys, tmp_path: Path) -> None:
    import kairix.core.embed.cli as cli_mod

    fake_db = _FakeDb(total_vecs=42, total_docs=10)
    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "kairix.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: fake_db)

    # Pending chunks: an empty list keeps the test self-contained.
    import kairix.core.embed.schema as schema_mod

    monkeypatch.setattr(schema_mod, "get_pending_chunks", lambda _db: [])

    rc = cmd_status(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Vectors:   42" in out
    assert "Documents: 10" in out
    assert "Pending:   0 documents need embedding" in out
    assert fake_db.closed is True


@pytest.mark.unit
def test_cmd_status_handles_last_run_log(monkeypatch, capsys, tmp_path: Path) -> None:
    """Status reads ~/.cache/kairix/azure-embed-runs.json if it exists."""
    import kairix.core.embed.cli as cli_mod
    import kairix.core.embed.schema as schema_mod

    home = tmp_path / "fakehome"
    home.mkdir()
    log_dir = home / ".cache" / "kairix"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "azure-embed-runs.json"
    log_path.write_text(
        '[{"timestamp": 1700000000, "embedded": 7, "estimated_cost_usd": 0.12345}]',
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "k.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: _FakeDb(total_vecs=1, total_docs=1))
    monkeypatch.setattr(schema_mod, "get_pending_chunks", lambda _db: [])

    rc = cmd_status(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Last run:" in out
    assert "embedded=7" in out
    assert "$0.1235" in out  # rounded to 4 d.p.


@pytest.mark.unit
def test_cmd_status_handles_broken_run_log(monkeypatch, capsys, tmp_path: Path) -> None:
    """When the run-log JSON is corrupted, status still returns 0."""
    import kairix.core.embed.cli as cli_mod
    import kairix.core.embed.schema as schema_mod

    home = tmp_path / "fakehome"
    home.mkdir()
    log_dir = home / ".cache" / "kairix"
    log_dir.mkdir(parents=True)
    (log_dir / "azure-embed-runs.json").write_text("not json{", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "k.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: _FakeDb(total_vecs=0, total_docs=0))
    monkeypatch.setattr(schema_mod, "get_pending_chunks", lambda _db: [])

    rc = cmd_status(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    # Even with broken log, the headline lines printed.
    assert "Documents:" in out


# ---------------------------------------------------------------------------
# cmd_rebuild_fts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_rebuild_fts_prints_before_after_state(monkeypatch, capsys, tmp_path: Path) -> None:
    import kairix.core.db.fts as fts_mod
    import kairix.core.embed.cli as cli_mod

    fake_db = _FakeDb(total_vecs=0, total_docs=0)
    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "k.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: fake_db)

    from types import SimpleNamespace

    state = SimpleNamespace(available=True, reason="ok", row_count=5)
    monkeypatch.setattr(fts_mod, "check_fts_available", lambda _db: state)
    monkeypatch.setattr(fts_mod, "rebuild_fts", lambda _db: 5)

    rc = cmd_rebuild_fts(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "FTS state before rebuild" in out
    assert "FTS state after rebuild" in out
    assert "Rebuilt: 5 documents indexed" in out


# ---------------------------------------------------------------------------
# acquire_lock / release_lock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_acquire_and_release_lock_roundtrip(monkeypatch, tmp_path: Path) -> None:
    import kairix.core.embed.cli as cli_mod

    lock_path = tmp_path / "embed.lock"
    monkeypatch.setattr(cli_mod, "LOCKFILE", lock_path)

    fh = acquire_lock()
    assert lock_path.exists()
    assert "pid" not in str(fh)  # sanity — fh is a file handle, not a stringified pid
    release_lock(fh)
    # After release, file is removed.
    assert not lock_path.exists()


@pytest.mark.unit
def test_acquire_lock_exits_3_when_holder_never_releases(monkeypatch, tmp_path: Path) -> None:
    """If LOCK_EX blocks for the entire wait window, acquire_lock exits 3."""
    import kairix.core.embed.cli as cli_mod

    lock_path = tmp_path / "embed.lock"
    monkeypatch.setattr(cli_mod, "LOCKFILE", lock_path)
    monkeypatch.setattr(cli_mod, "LOCK_WAIT_SECS", 0.01)

    # Make flock always raise BlockingIOError so we hit the timeout branch.
    import fcntl as fcntl_mod

    def _always_block(_fh: Any, _flags: int) -> None:
        raise BlockingIOError("would block")

    monkeypatch.setattr(fcntl_mod, "flock", _always_block)
    monkeypatch.setattr(cli_mod.time, "sleep", lambda _s: None)

    with pytest.raises(SystemExit) as info:
        acquire_lock()
    assert info.value.code == 3


@pytest.mark.unit
def test_release_lock_swallows_oserror(monkeypatch, tmp_path: Path) -> None:
    """release_lock must swallow OSError/ValueError without crashing.

    We hand it a closed file handle so ``fcntl.flock`` raises ValueError on
    a -1 fd — the production except clause covers both OSError and ValueError.
    """
    fh = open(tmp_path / "lock", "w")
    fh.close()  # close so any flock/close raises ValueError or OSError.
    release_lock(fh)  # no exception


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setup_logging_creates_log_dir(monkeypatch, tmp_path: Path) -> None:
    import kairix.core.embed.cli as cli_mod

    log_file = tmp_path / "deep" / "log" / "embed.log"
    monkeypatch.setattr(cli_mod, "LOG_FILE", log_file)
    # Use force=True semantics via basicConfig; we don't care about handler state.
    setup_logging(verbose=True)
    assert log_file.parent.exists()
    setup_logging(verbose=False)  # second pass — exercises non-verbose branch


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------


def _drive_main(argv: list[str], deps: EmbedCliDeps | None = None) -> int:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            embed_main(argv, deps=deps)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return code


@pytest.mark.unit
def test_main_default_subcommand_runs_cmd_embed() -> None:
    deps = _deps_returning(_result(embedded=1))
    code = _drive_main([], deps=deps)
    assert code == 0


@pytest.mark.unit
def test_main_dispatches_recall_check(monkeypatch) -> None:
    import kairix.core.embed.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "run_recall_gate",
        lambda: (True, {"passed": 1, "total": 1, "score": 1.0, "detail": []}),
    )
    assert _drive_main(["recall-check"]) == 0


@pytest.mark.unit
def test_main_dispatches_status(monkeypatch, tmp_path: Path) -> None:
    import kairix.core.embed.cli as cli_mod
    import kairix.core.embed.schema as schema_mod

    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "k.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: _FakeDb(total_vecs=0, total_docs=0))
    monkeypatch.setattr(schema_mod, "get_pending_chunks", lambda _db: [])
    assert _drive_main(["status"]) == 0


@pytest.mark.unit
def test_main_dispatches_rebuild_fts(monkeypatch, tmp_path: Path) -> None:
    import kairix.core.db.fts as fts_mod
    import kairix.core.embed.cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_db_path", lambda: tmp_path / "k.db")
    monkeypatch.setattr(cli_mod, "open_db", lambda _p: _FakeDb(total_vecs=0, total_docs=0))

    from types import SimpleNamespace

    monkeypatch.setattr(
        fts_mod,
        "check_fts_available",
        lambda _db: SimpleNamespace(available=True, reason="ok", row_count=0),
    )
    monkeypatch.setattr(fts_mod, "rebuild_fts", lambda _db: 0)

    assert _drive_main(["rebuild-fts"]) == 0


# ---------------------------------------------------------------------------
# _run_post_embed_summarise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_post_embed_summarise_no_docs_returns_early(monkeypatch, tmp_path: Path) -> None:
    """No .md files in document_root → function returns immediately."""
    import kairix.paths as paths

    monkeypatch.setattr(paths, "document_root", lambda: tmp_path)
    # No files in tmp_path → all_docs is empty → early return; no exception.
    embed_cli._run_post_embed_summarise()


@pytest.mark.unit
def test_run_post_embed_summarise_no_stale_docs_returns_after_init(monkeypatch, tmp_path: Path, caplog) -> None:
    """all_docs non-empty but no stale → log message + early return."""

    import kairix.knowledge.summaries.staleness as staleness
    import kairix.paths as paths

    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")

    monkeypatch.setattr(paths, "document_root", lambda: doc_root)
    monkeypatch.setattr(paths, "summaries_db_path", lambda: ":memory:")
    monkeypatch.setattr(staleness, "init_summaries_db", lambda _db: None)
    monkeypatch.setattr(staleness, "get_stale_paths", lambda _docs, _db: [])

    # sqlite3.connect(":memory:") should work fine.
    with caplog.at_level("INFO"):
        embed_cli._run_post_embed_summarise()
    assert any("all 1 docs have current summaries" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_post_embed_summarise_generates_summaries_for_stale_docs(monkeypatch, tmp_path: Path, caplog) -> None:
    """Stale docs found → generate_summaries is called and write_summary is invoked per result."""
    import kairix.knowledge.summaries.generate as gen_mod
    import kairix.knowledge.summaries.staleness as staleness
    import kairix.paths as paths

    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    (doc_root / "b.md").write_text("# b", encoding="utf-8")

    monkeypatch.setattr(paths, "document_root", lambda: doc_root)
    monkeypatch.setattr(paths, "summaries_db_path", lambda: ":memory:")
    monkeypatch.setattr(staleness, "init_summaries_db", lambda _db: None)
    monkeypatch.setattr(staleness, "get_stale_paths", lambda docs, _db: list(docs))

    written: list[object] = []
    monkeypatch.setattr(staleness, "write_summary", lambda r, _db: written.append(r))
    monkeypatch.setattr(
        gen_mod,
        "generate_summaries",
        lambda *, paths, api_key, endpoint, deployment: [f"summary-of-{p}" for p in paths],
    )

    with caplog.at_level("INFO"):
        embed_cli._run_post_embed_summarise()
    assert len(written) == 2
    assert any("L0 summaries generated" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_post_embed_summarise_swallows_exception(monkeypatch, caplog) -> None:
    """If any sub-step raises, the failure is logged but doesn't propagate."""
    import kairix.paths as paths

    def _raises() -> Any:
        raise RuntimeError("paths blown up")

    monkeypatch.setattr(paths, "document_root", _raises)
    with caplog.at_level("WARNING"):
        embed_cli._run_post_embed_summarise()
    assert any("Post-embed summarise failed" in r.message for r in caplog.records)


@pytest.mark.unit
def test_module_main_guard_runs_main() -> None:
    """Drive ``if __name__ == "__main__": main()`` (the bottom guard)."""
    old_argv = sys.argv
    try:
        sys.argv = ["kairix-embed", "--help"]
        with pytest.raises(SystemExit) as info:
            runpy.run_module("kairix.core.embed.cli", run_name="__main__")
        # argparse --help exits 0.
        assert int(info.value.code or 0) == 0
    finally:
        sys.argv = old_argv
