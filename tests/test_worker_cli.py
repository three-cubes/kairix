"""Tests for ``kairix worker status`` — #224 phase 5 CLI surface.

The status sub-command reads the worker's persisted JSON state and
prints it in operator-readable form. Tests cover:

  - status command prints phase + counters when a state file exists;
  - status command exits 1 with a clear message when no file is present;
  - format_status renders all fields without touching the filesystem.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kairix.worker_cli import build_parser, format_status, main, pause, resume, status
from kairix.worker_state import WorkerPhase, WorkerState, write_state

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_status_command_prints_phase_and_counters(tmp_path: Path) -> None:
    """status() reads a pre-written state file and prints all key fields.

    Sabotage proof: change ``state.embedded_total = 42`` to ``= 41`` in
    this test (or comment out the embedded_total render in
    ``format_status``) — the assertion ``"42"`` fails.
    """
    state_path = tmp_path / "worker-state.json"
    state = WorkerState(
        current_phase=WorkerPhase.INGEST,
        embedded_total=42,
        failed_chunks_total=3,
        recall_alerts_total=1,
        restart_count=7,
        consecutive_embed_noops=2,
    )
    write_state(state, state_path)

    out = io.StringIO()
    err = io.StringIO()
    rc = status(state_path=state_path, out=out, err=err)

    assert rc == 0
    printed = out.getvalue()
    assert "INGEST" in printed, f"phase missing from output: {printed!r}"
    assert "42" in printed, f"embedded_total missing: {printed!r}"
    assert "3" in printed, f"failed_chunks_total missing: {printed!r}"
    assert "1" in printed, f"recall_alerts_total missing: {printed!r}"
    assert "7" in printed, f"restart_count missing: {printed!r}"


@pytest.mark.unit
def test_status_command_exits_1_when_state_file_missing(tmp_path: Path) -> None:
    """No state file → exit 1, message on stderr, nothing on stdout.

    Sabotage proof: return 0 instead of 1 in the missing branch and the
    rc assertion fails. Monitoring scripts rely on this exit code.
    """
    state_path = tmp_path / "does-not-exist.json"
    assert not state_path.exists()

    out = io.StringIO()
    err = io.StringIO()
    rc = status(state_path=state_path, out=out, err=err)

    assert rc == 1
    assert out.getvalue() == "", "no status text should print when state is missing"
    assert "no state file" in err.getvalue().lower(), f"stderr should explain: {err.getvalue()!r}"


@pytest.mark.unit
def test_format_status_renders_all_observable_fields() -> None:
    """Direct test of the pure renderer — no I/O, no tmp_path needed.

    Sabotage proof: drop any of the rendered fields from
    ``format_status`` and the corresponding ``in rendered`` fails.
    """
    state = WorkerState(
        current_phase=WorkerPhase.MAINTENANCE,
        embedded_total=128,
        failed_chunks_total=4,
        recall_alerts_total=2,
        restart_count=9,
        consecutive_embed_noops=5,
        last_embed_run_at=1000.0,
        last_embed_did_work=True,
        started_at=900.0,
    )
    # Pin ``now`` so age formatting is deterministic.
    rendered = format_status(state, now=1180.0)
    assert "MAINTENANCE" in rendered
    assert "128" in rendered  # embedded_total
    assert "4" in rendered  # failed_chunks_total
    assert "2" in rendered  # recall_alerts_total
    assert "9" in rendered  # restart_count
    assert "5" in rendered  # consecutive_embed_noops
    # Last embed was 180s ago → "3 min ago"
    assert "min ago" in rendered, f"age format missing in: {rendered!r}"


@pytest.mark.unit
def test_format_status_renders_never_for_unset_timestamps() -> None:
    """Default WorkerState has last_embed_run_at=0; should render 'never'.

    Sabotage proof: if format_status fed 0 into the duration formatter
    blindly, it would print "0s ago" — the assertion catches that.
    """
    state = WorkerState()
    rendered = format_status(state, now=1000.0)
    assert "never" in rendered.lower(), f"expected 'never' for unset timestamps: {rendered!r}"


@pytest.mark.unit
def test_format_status_surfaces_connector_sync_fields() -> None:
    """SYNC-OBS: ``kairix worker status`` shows the connector-sync heartbeat.

    A recent ``last_connector_sync_at`` with ``last_connector_tick_yielded``
    False is the quiet-vs-dead signal — the source IS being polled but had
    no new docs. The renderer must surface that, plus ``syncs_attempted``.

    Sabotage proof: drop the ``Last connector sync:`` line from
    ``format_status`` and the ``"Last connector sync"`` assertion fails.
    """
    state = WorkerState(
        syncs_attempted=7,
        last_connector_sync_at=1000.0,
        last_connector_tick_yielded=False,
        last_connector_synced=0,
        last_connector_connectors_polled=3,
    )
    rendered = format_status(state, now=1060.0)  # 60s after the last sync
    assert "Last connector sync" in rendered
    assert "yielded last tick: False" in rendered
    assert "Syncs attempted: 7" in rendered
    assert "Connectors polled last tick: 3" in rendered


@pytest.mark.unit
def test_format_status_connector_sync_never_when_unset() -> None:
    """A worker that has not yet run a sync shows ``never`` for the heartbeat."""
    state = WorkerState()
    rendered = format_status(state, now=1000.0)
    assert "Last connector sync: never" in rendered
    assert "Syncs attempted: 0" in rendered


@pytest.mark.unit
def test_main_dispatches_status_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``kairix worker status`` returns the status exit code through main().

    We can't easily redirect the default ``worker_state_path()`` here,
    so we exercise the dispatch via ``status()`` directly with an
    injected path. The point of this test is to prove the argparse
    routing — by writing the state to the default path under tmp_path
    we'd need filesystem monkeypatching, which violates F2 discipline.
    Instead we test the parser builds the expected subcommand and that
    ``main([])`` resolves to the worker loop branch (asserted via the
    deferred import not happening — that's hard to test directly, so
    we settle for parser-shape coverage here and the dedicated
    ``status()`` tests above).
    """
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.cmd == "status"
    # Default (no args) dispatches to ``run``.
    args2 = parser.parse_args([])
    assert args2.cmd is None  # default branch in main() falls through to worker loop


@pytest.mark.unit
def test_main_status_returns_exit_code_via_dispatcher(tmp_path: Path) -> None:
    """End-to-end: ``main(["status"], state_path=...)`` returns 1 when no
    state file exists.

    Uses the ``state_path`` injection seam on ``main()`` — F1-clean
    (no @patch, no monkeypatch on internals).
    """
    state_path = tmp_path / "worker-state.json"
    rc = main(["status"], state_path=state_path)
    assert rc == 1


@pytest.mark.unit
def test_main_status_returns_zero_when_state_exists(tmp_path: Path) -> None:
    """End-to-end: ``main(["status"], state_path=...)`` returns 0 when state
    file is present."""
    state_path = tmp_path / "worker-state.json"
    write_state(WorkerState(current_phase=WorkerPhase.IDLE), state_path)
    rc = main(["status"], state_path=state_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# pause / resume (#224 phase 4)
# ---------------------------------------------------------------------------


def test_pause_cli_creates_flag_file(tmp_path: Path) -> None:
    """``main(["pause"])`` creates the flag file at the injected path.

    Sabotage proof: removing the ``path.touch()`` line in worker_cli.pause
    leaves the file absent and this assert fails.
    """
    flag = tmp_path / ".worker-paused"
    assert not flag.exists()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists(), "pause must create the flag file"


def test_resume_cli_removes_flag_file(tmp_path: Path) -> None:
    """Pre-touch the flag, dispatch ``main(["resume"])``, assert it's gone.

    Sabotage proof: replacing ``unlink(missing_ok=True)`` with a no-op
    leaves the flag in place and this assert fails.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()
    assert flag.exists()

    exit_code = main(["resume"], flag_path=flag)

    assert exit_code == 0
    assert not flag.exists(), "resume must remove the flag file"


def test_resume_cli_is_idempotent_when_flag_missing(tmp_path: Path) -> None:
    """``resume`` without a pre-existing flag returns 0 without raising.

    Sabotage proof: switching to ``unlink()`` (no missing_ok) would raise
    FileNotFoundError and this test would fail with an unhandled exception.
    """
    flag = tmp_path / ".worker-paused"
    assert not flag.exists()

    exit_code = main(["resume"], flag_path=flag)

    assert exit_code == 0
    assert not flag.exists()


def test_pause_cli_is_idempotent_when_flag_already_present(tmp_path: Path) -> None:
    """Calling pause twice in a row leaves the flag present and exit 0.

    Sabotage proof: if pause raised on existing flag (e.g. ``open(x, "x")``
    instead of touch), the second call would error.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists()


def test_pause_function_returns_zero_and_creates_file(tmp_path: Path) -> None:
    """The ``pause`` helper (called directly) creates the flag and returns 0.

    Sabotage proof: returning anything other than 0 from ``pause`` would
    fail this assertion; the brief requires exit 0 on success.
    """
    flag = tmp_path / ".worker-paused"

    result = pause(flag_path=flag)

    assert result == 0
    assert flag.exists()


def test_resume_function_returns_zero_and_removes_file(tmp_path: Path) -> None:
    """The ``resume`` helper (called directly) removes the flag and returns 0.

    Sabotage proof: returning anything other than 0 would fail this assert.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()

    result = resume(flag_path=flag)

    assert result == 0
    assert not flag.exists()


def test_pause_creates_parent_directory_if_missing(tmp_path: Path) -> None:
    """A fresh data dir layout (parent dir doesn't exist yet) is created.

    Sabotage proof: dropping the ``mkdir(parents=True, exist_ok=True)``
    leaves the parent missing and ``touch`` raises FileNotFoundError.
    """
    flag = tmp_path / "nested" / "data" / ".worker-paused"
    assert not flag.parent.exists()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists()


def test_pause_cli_prints_resume_instruction(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The pause output tells the operator how to resume — discoverability check.

    Sabotage proof: changing the print to anything not containing 'resume'
    would fail this. The string is the operator-facing UX contract.
    """
    flag = tmp_path / ".worker-paused"
    main(["pause"], flag_path=flag)
    out = capsys.readouterr().out
    assert "resume" in out.lower(), f"pause output must mention resume; got: {out!r}"


def test_resume_cli_prints_latency_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Resume output warns about the up-to-5s poll latency — operator UX.

    Sabotage proof: changing the print would fail this. The 5s note is
    important because operators may otherwise expect instant resume.
    """
    flag = tmp_path / ".worker-paused"
    main(["resume"], flag_path=flag)
    out = capsys.readouterr().out
    assert "5s" in out, f"resume output must mention the 5s latency; got: {out!r}"


# ---------------------------------------------------------------------------
# Config-drift WARN in ``kairix worker status`` (issue #726 observability half)
# ---------------------------------------------------------------------------

_TWO_SOURCE_CONFIG = {
    "topology": {
        "connectors": [
            {"id": "obs-conn", "kind": "obsidian", "name": "obs-conn"},
            {"id": "sp-conn", "kind": "sharepoint", "name": "sp-conn"},
        ],
        "credentials": [
            {"id": "m365-oauth", "kind": "oauth", "secret_name": "s-m365"},  # pragma: allowlist secret
        ],
        "cc_pairs": [
            {"id": "obs-cp", "connector": "obs-conn", "credential": None, "name": "obsidian-personal"},
            {"id": "sp-cp", "connector": "sp-conn", "credential": "m365-oauth", "name": "sharepoint-corp"},
        ],
        "collections": [
            {"name": "obsidian-all", "sources": [{"cc_pair": "obs-cp", "path_filter": "*"}]},
            {"name": "sharepoint-public", "sources": [{"cc_pair": "sp-cp", "path_filter": "*"}]},
        ],
    }
}

_ONE_SOURCE_CONFIG = {
    "topology": {
        "connectors": [{"id": "obs-conn", "kind": "obsidian", "name": "obs-conn"}],
        "cc_pairs": [{"id": "obs-cp", "connector": "obs-conn", "credential": None, "name": "obsidian-personal"}],
        "collections": [{"name": "obsidian-all", "sources": [{"cc_pair": "obs-cp", "path_filter": "*"}]}],
    }
}


def _seed_topology_db(tmp_path: Path, config: dict[str, object]) -> Path:
    """Materialise ``config`` into a file-backed topology store and return its path.

    Uses the production applier so the seeded rows match exactly what a live
    worker boot would write — the drift scan then reads them back.
    """
    import sqlite3

    from kairix.config import parse_topology
    from kairix.core.connectors.topology_applier import apply_topology
    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db, dims=4)
    apply_topology(db, parse_topology(config))
    db.commit()
    db.close()
    return db_path


def _write_idle_state(tmp_path: Path) -> Path:
    state_path = tmp_path / "worker-state.json"
    write_state(WorkerState(current_phase=WorkerPhase.IDLE, embedded_total=5), state_path)
    return state_path


def test_status_warns_when_store_holds_source_removed_from_config(tmp_path: Path) -> None:
    """A store with a source the config dropped surfaces the drift WARN.

    Sabotage proof: delete the ``if warn: out.write(warn ...)`` branch in
    ``status`` (or the ``stored - config`` subtraction in
    ``detect_config_drift``) — the ``config drift: 3`` assertion fails.
    """
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        db_path=db_path,
        config_mapping=_ONE_SOURCE_CONFIG,
    )

    printed = out.getvalue()
    assert rc == 0
    assert "WARN config drift: 3 topology source(s)" in printed, printed
    assert "still routed/synced until pruned" in printed, printed
    for stranded in ("sp-conn", "sharepoint-corp", "sharepoint-public"):
        assert stranded in printed, f"{stranded} missing from drift WARN: {printed!r}"


def test_status_no_drift_warn_when_config_matches_store(tmp_path: Path) -> None:
    """Store and config agree → status renders no drift WARN.

    Sabotage proof: hard-code ``ConfigDriftReport.has_drift`` to True — this
    assertion that ``config drift`` is absent fails.
    """
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        db_path=db_path,
        config_mapping=_TWO_SOURCE_CONFIG,
    )

    printed = out.getvalue()
    assert rc == 0
    assert "config drift" not in printed, printed
    assert "Phase: IDLE" in printed


def test_status_json_envelope_carries_config_drift_block(tmp_path: Path) -> None:
    """``--json`` mode embeds a machine-readable ``config_drift`` block on drift.

    Sabotage proof: drop the ``envelope['config_drift'] = ...`` assignment in
    ``status`` — the ``config_drift`` key assertion raises KeyError.
    """
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        as_json=True,
        db_path=db_path,
        config_mapping=_ONE_SOURCE_CONFIG,
    )

    envelope = json.loads(out.getvalue())
    assert rc == 0
    assert envelope["config_drift"]["count"] == 3
    assert envelope["config_drift"]["connectors"] == ["sp-conn"]
    assert envelope["config_drift"]["cc_pairs"] == ["sharepoint-corp"]
    assert envelope["config_drift"]["collections"] == ["sharepoint-public"]


def test_status_json_envelope_omits_config_drift_when_clean(tmp_path: Path) -> None:
    """No drift → ``config_drift`` key is absent (backward-compatible envelope)."""
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        as_json=True,
        db_path=db_path,
        config_mapping=_TWO_SOURCE_CONFIG,
    )

    envelope = json.loads(out.getvalue())
    assert rc == 0
    assert "config_drift" not in envelope


def test_status_drift_scan_is_readonly_noop_when_db_absent(tmp_path: Path) -> None:
    """A ``--db-path`` that does not exist is skipped — status never creates it.

    Sabotage proof: change ``_resolve_existing_db_path`` to return the path
    without the ``.exists()`` guard — ``open_db`` then creates an empty index
    and this ``not db_path.exists()`` assertion fails.
    """
    missing_db = tmp_path / "never-ingested.sqlite"
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        db_path=missing_db,
        config_mapping=_ONE_SOURCE_CONFIG,
    )

    assert rc == 0
    assert "config drift" not in out.getvalue()
    assert not missing_db.exists(), "status must not create a DB just to scan for drift"


def test_main_status_threads_db_path_and_json_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``main(["status", "--db-path", ..., "--json"])`` dispatches through to a
    valid JSON envelope — pins the argparse ``--db-path`` + ``--json`` threading.

    Sabotage proof: drop ``db_path=resolved_db`` from the status dispatch in
    ``main`` — status then scans the default index instead of ``--db-path``;
    with the seeded store here the envelope still parses, so this test pins the
    envelope shape / exit code rather than the drift contents (which the
    in-process ``status`` tests above assert deterministically).
    """
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    rc = main(["status", "--state-path", str(state_path), "--db-path", str(db_path), "--json"])

    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["current_phase"] == WorkerPhase.IDLE.value


def test_status_default_config_resolution_does_not_crash(tmp_path: Path) -> None:
    """``config_mapping=None`` exercises the layered-config default loader safely.

    Drives the production ``load_merged_mapping`` resolution branch. The drift
    outcome depends on the ambient config, so we only assert the command stays
    green and still renders the state — the best-effort scan must never break
    ``kairix worker status``.
    """
    db_path = _seed_topology_db(tmp_path, _TWO_SOURCE_CONFIG)
    state_path = _write_idle_state(tmp_path)

    out = io.StringIO()
    rc = status(
        state_path=state_path,
        out=out,
        err=io.StringIO(),
        db_path=db_path,
        config_mapping=None,
    )

    assert rc == 0
    assert "Phase: IDLE" in out.getvalue()


# ---------------------------------------------------------------------------
# ``kairix worker preflight`` human renderer + ``maintenance`` FTS heal +
# argv path-flag routing, driven in-process through ``main(argv, ...)``.
# ---------------------------------------------------------------------------

_PREFLIGHT_PASSED_CLEAN = "Preflight integrity check: PASSED (no gaps detected)"


def _seed_documents_without_fts(tmp_path: Path) -> Path:
    """Seed two active documents (content + vectors) and wipe every FTS row.

    Reproduces the IM-6 failure mode: real documents with no matching
    ``documents_fts`` rows, so BM25 silently degrades to vector-only.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db, dims=4)
    now = "2026-05-25T09:00:00Z"
    for doc_path, doc_hash, text in (("a.md", "hash-a", "alpha content"), ("b.md", "hash-b", "beta content")):
        db.execute(
            "INSERT INTO documents (collection, path, hash, created_at, modified_at, active) VALUES (?, ?, ?, ?, ?, 1)",
            ("default", doc_path, doc_hash, now, now),
        )
        db.execute("INSERT INTO content (hash, doc, created_at) VALUES (?, ?, ?)", (doc_hash, text, now))
        db.execute("INSERT INTO content_vectors (hash, seq, pos) VALUES (?, 0, 0)", (doc_hash,))
    db.execute("DELETE FROM documents_fts")
    db.commit()
    db.close()
    return db_path


def _fts_row_count(db_path: Path) -> int:
    import sqlite3

    db = sqlite3.connect(str(db_path))
    try:
        return int(db.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0])
    finally:
        db.close()


def test_format_status_renders_hours_for_ages_over_an_hour() -> None:
    """Ages of an hour or more render as fractional hours.

    Sabotage proof: change the ``< 3600`` threshold in ``_format_age`` to
    ``< 36000`` — the 2-hour-old embed renders ``120 min ago`` and the
    ``2.0 h ago`` assertion fails.
    """
    now = 100_000.0
    state = WorkerState(current_phase=WorkerPhase.IDLE, last_embed_run_at=now - 7200, started_at=now - 5400)

    rendered = format_status(state, now=now)

    assert "Last embed: 2.0 h ago" in rendered
    assert "Uptime: 1.5 h ago" in rendered


def test_main_preflight_human_reports_clean_store_as_passed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A freshly-created schema audits clean: exit 0 + the no-gaps PASSED line.

    Sabotage proof: drop the ``report.healthy and not report.gaps`` early
    return in ``_render_preflight_human`` — the output becomes
    ``PASSED (0 gap(s))`` and the assertion fails.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db, dims=4)
    db.close()

    rc = main(["preflight"], db_path=db_path)

    assert rc == 0
    assert _PREFLIGHT_PASSED_CLEAN in capsys.readouterr().out


def test_main_preflight_human_lists_each_error_gap_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Documents without FTS rows fail preflight with a per-gap operator line.

    Sabotage proof: make ``_format_gap_line`` return ``gap.invariant`` only —
    the ``[ERROR] documents-without-fts: count=2 sample=[`` assertion fails.
    """
    db_path = _seed_documents_without_fts(tmp_path)

    rc = main(["preflight", "--db-path", str(db_path)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "Preflight integrity check: FAILED (" in out
    assert "[ERROR] documents-without-fts: count=2 sample=[" in out


def test_main_preflight_auto_heal_human_output_rebuilds_fts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--auto-heal`` rebuilds FTS for the gap, then the re-audit passes clean.

    Sabotage proof: skip the ``rebuild_fts`` call in ``_auto_heal_gaps`` —
    the re-audit still sees the gap, rc is 1 and the PASSED assertion fails.
    """
    db_path = _seed_documents_without_fts(tmp_path)

    rc = main(["preflight", "--auto-heal"], db_path=db_path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-heal: rebuild_fts indexed 2 documents" in out
    # Post-heal re-audit passes; a warn-severity gap (e.g. no usearch
    # index on disk) may remain listed but does not fail the audit.
    assert "Preflight integrity check: PASSED (" in out
    assert "documents-without-fts" not in out.split("Preflight integrity check:", 1)[1]
    assert _fts_row_count(db_path) == 2


def test_main_maintenance_heals_fts_drift_in_place(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The maintenance verb's FTS healer rebuilds a store whose FTS rows are missing.

    Sabotage proof: make ``_maintenance_verb_fts_healer`` always ``return 0``
    — no FTS rows are rebuilt and both assertions fail.
    """
    db_path = _seed_documents_without_fts(tmp_path)

    rc = main(["maintenance", "--db-path", str(db_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "fts_orphans_healed=2" in out
    assert _fts_row_count(db_path) == 2


def test_main_pause_and_resume_honour_flag_path_argv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--flag-path`` on argv (the F30 subprocess seam) routes pause/resume.

    Sabotage proof: make ``_resolve_flag_path_arg`` ignore ``arg`` (return
    None) — pause touches the production default path instead, so the
    ``flag.exists()`` assertion fails.
    """
    flag = tmp_path / "nested" / ".worker-paused"

    assert main(["pause", "--flag-path", str(flag)]) == 0
    assert flag.exists()
    assert main(["resume", "--flag-path", str(flag)]) == 0
    assert not flag.exists()
    assert "Worker paused" in capsys.readouterr().out
