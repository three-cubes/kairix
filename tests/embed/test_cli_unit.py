"""Unit tests for kairix/core/embed/cli.py.

The BDD layer covers ``--help`` / argparse rejection. These unit tests
drive each ``cmd_*`` exit-code mapping and the dispatcher in main(),
using the ``EmbedCliDeps`` injection seam where the production code
constructs heavy collaborators.

No real DB, no Azure, no lockfile, no FTS rebuild.

F1 paydown note: every production collaborator (recall gate, DB
helpers, FTS helpers, summarise sub-helpers) is reached through
``EmbedCliDeps`` rather than ``monkeypatch.setattr`` on
``kairix.core.embed.cli`` module attributes — see the dataclass for
the full surface.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kairix.core.embed.cli import (
    EmbedCliDeps,
    acquire_lock,
    cmd_embed,
    cmd_rebuild_fts,
    cmd_recall,
    cmd_status,
    release_lock,
    run_post_embed_summarise,
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
    defaults: dict[str, Any] = {
        "embedded": 0,
        "failed": 0,
        "skipped": 0,
        "duration_s": 0.0,
        "cost_usd": 0.0,
        "db_path": "/tmp/k.db",
        "timestamp": 0,
        "recall_score": None,
        "recall_passed": None,
        "recall_alert": None,
        "scan_new": 0,
        "scan_updated": 0,
        "scan_errors": 0,
        "diagnostics": [],
    }
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
# Fakes for DB / FTS / summarise sub-helpers
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


def _status_deps(
    *,
    db_path: Path,
    fake_db: _FakeDb,
    pending: list[Any] | None = None,
) -> EmbedCliDeps:
    """Compose an EmbedCliDeps with status-mode DB + pending-chunk stubs."""
    return EmbedCliDeps(
        get_db_path_fn=lambda: db_path,
        open_db_fn=lambda _p: fake_db,
        get_pending_chunks_fn=lambda _db: pending or [],
    )


def _rebuild_fts_deps(
    *,
    db_path: Path,
    fake_db: _FakeDb,
    state_before: SimpleNamespace,
    state_after: SimpleNamespace | None = None,
    rebuilt: int = 0,
) -> EmbedCliDeps:
    after = state_after if state_after is not None else state_before
    states: list[SimpleNamespace] = [state_before, after]

    def _check(_db: Any) -> SimpleNamespace:
        return states.pop(0) if states else after

    return EmbedCliDeps(
        get_db_path_fn=lambda: db_path,
        open_db_fn=lambda _p: fake_db,
        check_fts_available_fn=_check,
        rebuild_fts_fn=lambda _db: rebuilt,
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
def test_cmd_recall_returns_0_when_gate_passes(capsys) -> None:
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

    deps = EmbedCliDeps(run_recall_gate_fn=_fake_run_recall_gate)
    rc = cmd_recall(argparse.Namespace(), deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Recall: 4/5 (80%)" in out
    # Detail lines emitted.
    assert "✓" in out
    assert "✗" in out


@pytest.mark.unit
def test_cmd_recall_returns_1_when_gate_fails() -> None:
    deps = EmbedCliDeps(
        run_recall_gate_fn=lambda: (False, {"passed": 1, "total": 5, "score": 0.2, "detail": []}),
    )
    assert cmd_recall(argparse.Namespace(), deps=deps) == 1


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_status_prints_counters_and_returns_0(capsys, tmp_path: Path) -> None:
    fake_db = _FakeDb(total_vecs=42, total_docs=10)
    deps = _status_deps(db_path=tmp_path / "kairix.db", fake_db=fake_db, pending=[])

    rc = cmd_status(argparse.Namespace(), deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Vectors:   42" in out
    assert "Documents: 10" in out
    assert "Pending:   0 documents need embedding" in out
    assert fake_db.closed is True


@pytest.mark.unit
def test_cmd_status_handles_last_run_log(monkeypatch, capsys, tmp_path: Path) -> None:
    """Status reads ~/.cache/kairix/azure-embed-runs.json if it exists.

    ``Path.home`` is stdlib — F1 exempts stdlib roots — so we can
    monkeypatch it directly to point at a tmp home; the kairix-internal
    collaborators flow through ``EmbedCliDeps``.
    """
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
    deps = _status_deps(
        db_path=tmp_path / "k.db",
        fake_db=_FakeDb(total_vecs=1, total_docs=1),
        pending=[],
    )

    rc = cmd_status(argparse.Namespace(), deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Last run:" in out
    assert "embedded=7" in out
    assert "$0.1235" in out  # rounded to 4 d.p.


@pytest.mark.unit
def test_cmd_status_handles_broken_run_log(monkeypatch, capsys, tmp_path: Path) -> None:
    """When the run-log JSON is corrupted, status still returns 0."""
    home = tmp_path / "fakehome"
    home.mkdir()
    log_dir = home / ".cache" / "kairix"
    log_dir.mkdir(parents=True)
    (log_dir / "azure-embed-runs.json").write_text("not json{", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    deps = _status_deps(
        db_path=tmp_path / "k.db",
        fake_db=_FakeDb(total_vecs=0, total_docs=0),
        pending=[],
    )

    rc = cmd_status(argparse.Namespace(), deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    # Even with broken log, the headline lines printed.
    assert "Documents:" in out


# ---------------------------------------------------------------------------
# cmd_rebuild_fts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_rebuild_fts_prints_before_after_state(capsys, tmp_path: Path) -> None:
    fake_db = _FakeDb(total_vecs=0, total_docs=0)
    state = SimpleNamespace(available=True, reason="ok", row_count=5)
    deps = _rebuild_fts_deps(
        db_path=tmp_path / "k.db",
        fake_db=fake_db,
        state_before=state,
        rebuilt=5,
    )

    rc = cmd_rebuild_fts(argparse.Namespace(), deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "FTS state before rebuild" in out
    assert "FTS state after rebuild" in out
    assert "Rebuilt: 5 documents indexed" in out


# ---------------------------------------------------------------------------
# acquire_lock / release_lock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_acquire_and_release_lock_roundtrip(tmp_path: Path) -> None:
    lock_path = tmp_path / "embed.lock"
    fh = acquire_lock(lockfile=lock_path)
    assert lock_path.exists()
    assert "pid" not in str(fh)  # sanity — fh is a file handle, not a stringified pid
    release_lock(fh, lockfile=lock_path)
    # After release, file is removed.
    assert not lock_path.exists()


@pytest.mark.unit
def test_acquire_lock_exits_3_when_holder_never_releases(monkeypatch, tmp_path: Path, caplog) -> None:
    """If LOCK_EX blocks for the entire wait window, acquire_lock exits 3
    and the error carries the F21 affordance (#475) — fix:/next:/run:
    telling the operator the worker holds the lock and how to pause it.

    ``fcntl`` is stdlib — F1 exempts it — so we keep the flock monkeypatch
    to force the BlockingIOError path. Time + lockfile flow through the
    new public seams (no module-attribute reassignment).

    Sabotage proof: reverting the lock-timeout message to the bare
    "another embed is still running" line fails the fix:/run: assertions.
    """
    lock_path = tmp_path / "embed.lock"

    # Make flock always raise BlockingIOError so we hit the timeout branch.
    import fcntl as fcntl_mod

    def _always_block(_fh: Any, _flags: int) -> None:
        raise BlockingIOError("would block")

    monkeypatch.setattr(fcntl_mod, "flock", _always_block)
    # ``time.sleep`` is stdlib — F1 allows patching the stdlib module
    # to keep the test fast.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    import logging as _logging

    with caplog.at_level(_logging.ERROR):
        with pytest.raises(SystemExit) as info:
            acquire_lock(lockfile=lock_path, wait_secs=0.01)
    assert info.value.code == 3
    assert "fix: another embed (usually the background worker) holds the lock" in caplog.text
    assert "next: kairix worker status" in caplog.text
    assert "run: kairix worker pause && kairix embed && kairix worker resume" in caplog.text


@pytest.mark.unit
def test_release_lock_swallows_oserror(tmp_path: Path) -> None:
    """release_lock must swallow OSError/ValueError without crashing.

    We hand it a closed file handle so ``fcntl.flock`` raises ValueError on
    a -1 fd — the production except clause covers both OSError and ValueError.
    """
    fh = open(tmp_path / "lock", "w")
    fh.close()  # close so any flock/close raises ValueError or OSError.
    release_lock(fh, lockfile=tmp_path / "lock")  # no exception


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setup_logging_creates_log_dir(tmp_path: Path) -> None:
    log_file = tmp_path / "deep" / "log" / "embed.log"
    # Use force=True semantics via basicConfig; we don't care about handler state.
    setup_logging(verbose=True, log_file=log_file)
    assert log_file.parent.exists()
    setup_logging(verbose=False, log_file=log_file)  # second pass — exercises non-verbose branch


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
def test_main_dispatches_recall_check() -> None:
    deps = EmbedCliDeps(
        run_recall_gate_fn=lambda: (True, {"passed": 1, "total": 1, "score": 1.0, "detail": []}),
    )
    assert _drive_main(["recall-check"], deps=deps) == 0


@pytest.mark.unit
def test_main_dispatches_status(tmp_path: Path) -> None:
    deps = _status_deps(
        db_path=tmp_path / "k.db",
        fake_db=_FakeDb(total_vecs=0, total_docs=0),
        pending=[],
    )
    assert _drive_main(["status"], deps=deps) == 0


@pytest.mark.unit
def test_main_dispatches_rebuild_fts(tmp_path: Path) -> None:
    deps = _rebuild_fts_deps(
        db_path=tmp_path / "k.db",
        fake_db=_FakeDb(total_vecs=0, total_docs=0),
        state_before=SimpleNamespace(available=True, reason="ok", row_count=0),
        rebuilt=0,
    )
    assert _drive_main(["rebuild-fts"], deps=deps) == 0


# ---------------------------------------------------------------------------
# run_post_embed_summarise (wired into EmbedCliDeps.post_embed_summarise)
# ---------------------------------------------------------------------------


def _summarise_deps(
    *,
    doc_root: Path,
    summaries_db: Path = Path(":memory:"),
    stale: list[str] | None = None,
    generated: list[Any] | None = None,
    written: list[Any] | None = None,
) -> EmbedCliDeps:
    """Compose an EmbedCliDeps wired for ``run_post_embed_summarise`` testing.

    Every sub-helper has a controllable lambda; ``written`` is the
    optional list the test inspects to confirm ``write_summary_fn``
    fired once per generated summary.
    """
    write_log: list[Any] = written if written is not None else []
    gen_results: list[Any] = generated if generated is not None else []

    return EmbedCliDeps(
        document_root_fn=lambda: doc_root,
        summaries_db_path_fn=lambda: summaries_db,
        init_summaries_db_fn=lambda _db: None,
        get_stale_paths_fn=lambda _docs, _db: list(stale or []),
        generate_summaries_fn=lambda **_kw: gen_results,
        write_summary_fn=lambda r, _db: write_log.append(r),
    )


@pytest.mark.unit
def test_run_post_embed_summarise_no_docs_returns_early(tmp_path: Path) -> None:
    """No .md files in document_root → function returns immediately."""
    deps = _summarise_deps(doc_root=tmp_path)
    # No files in tmp_path → all_docs is empty → early return; no exception.
    run_post_embed_summarise(deps=deps)


@pytest.mark.unit
def test_run_post_embed_summarise_no_stale_docs_returns_after_init(tmp_path: Path, caplog) -> None:
    """all_docs non-empty but no stale → log message + early return."""
    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")

    deps = _summarise_deps(doc_root=doc_root, stale=[])

    # sqlite3.connect(":memory:") should work fine.
    with caplog.at_level("INFO"):
        run_post_embed_summarise(deps=deps)
    assert any("all 1 docs have current summaries" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_post_embed_summarise_generates_summaries_for_stale_docs(tmp_path: Path, caplog) -> None:
    """Stale docs found → generate_summaries is called and write_summary is invoked per result."""
    doc_root = tmp_path / "docs"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    (doc_root / "b.md").write_text("# b", encoding="utf-8")

    written: list[Any] = []
    deps = _summarise_deps(
        doc_root=doc_root,
        stale=[str(doc_root / "a.md"), str(doc_root / "b.md")],
        generated=["summary-of-a", "summary-of-b"],
        written=written,
    )

    with caplog.at_level("INFO"):
        run_post_embed_summarise(deps=deps)
    assert len(written) == 2
    assert any("L0 summaries generated" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_post_embed_summarise_swallows_exception(caplog) -> None:
    """If any sub-step raises, the failure is logged but doesn't propagate."""

    def _raises() -> Any:
        raise RuntimeError("paths blown up")

    deps = EmbedCliDeps(document_root_fn=_raises)
    with caplog.at_level("WARNING"):
        run_post_embed_summarise(deps=deps)
    assert any("Post-embed summarise failed" in r.message for r in caplog.records)


@pytest.mark.unit
def test_module_main_guard_runs_main() -> None:
    """Drive ``if __name__ == "__main__": main()`` (the bottom guard) in a
    subprocess — a fresh interpreter, so no kairix module body re-executes in
    the test process (F1)."""
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.core.embed.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # argparse --help exits 0.
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout
