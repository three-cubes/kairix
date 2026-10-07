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
def test_health_resolves_default_client_via_client_factory_kwarg() -> None:
    """When neo4j_client is None, the CLI must invoke ``client_factory``.

    Drives the public ``client_factory`` kwarg seam: the test passes a
    counting factory and asserts it was called exactly once. Exercises
    the "neo4j_client is None → factory()" branch without monkey-patching
    the lazy ``get_client`` import inside _health_cmd, and the
    call-count assertion catches any sabotage that bypasses the kwarg.
    """
    fake = FakeNeo4jClient(entities=[])
    call_count = 0

    def counting_factory() -> Any:
        nonlocal call_count
        call_count += 1
        return fake

    stdout, _stderr, code = _drive(["health", "--format", "json"], client_factory=counting_factory)
    assert code == 0
    assert call_count == 1, f"client_factory must be invoked exactly once; got {call_count}"
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


# ---------------------------------------------------------------------------
# GH #334 — `kairix curator drain` unit tests.
#
# The integration tests exercise the subprocess surface (F30 outcome
# contract). These unit tests cover the in-process CLI branches with the
# F47-sanctioned drain_repo injection seam — drain logic gets coverage
# without spawning a Python subprocess for every assertion.
# ---------------------------------------------------------------------------


def _seed_drain_db(tmp_path: Path, count: int = 2) -> Path:
    """Seed a tmp SQLite DB with N un-pushed person signals."""
    import sqlite3

    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "drain_unit.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        create_schema(conn)
        for i in range(count):
            conn.execute(
                "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
                "sensitivity, pushed_to_neo4j, push_attempt_count) "
                "VALUES ('person', ?, ?, ?, 0.9, 'internal', 0, 0)",
                (f"unit-person-{i}", f"vault://unit-person-{i}.md", f"2026-05-2{i}T10:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.mark.unit
def test_drain_text_format_prints_summary_line_with_pushed_count(tmp_path: Path) -> None:
    """`kairix curator drain` text format prints pushed=N for the operator."""
    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=2)
    fake_repo = FakeDrainGraphRepository(available=True)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "text"],
        drain_repo=fake_repo,
    )

    assert code == 0, f"drain exited {code}; stderr was empty? stdout={stdout!r}"
    assert "neo4j drain complete" in stdout, f"missing summary line in stdout: {stdout!r}"
    assert "pushed                 : 2" in stdout, f"expected pushed=2 line; got: {stdout!r}"
    assert "batches_run            : 1" in stdout


@pytest.mark.unit
def test_drain_json_format_emits_envelope_with_required_keys(tmp_path: Path) -> None:
    """`kairix curator drain --format json` outputs a parseable envelope."""
    import json as _json

    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=1)
    fake_repo = FakeDrainGraphRepository(available=True)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json"],
        drain_repo=fake_repo,
    )

    assert code == 0
    envelope = _json.loads(stdout)
    assert envelope["pushed"] == 1
    assert envelope["failed"] == 0
    assert envelope["neo4j_available"] is True
    assert envelope["batches_run"] == 1
    assert envelope["dry_run"] is False


@pytest.mark.unit
def test_drain_max_batches_three_drains_full_backlog(tmp_path: Path) -> None:
    """``--max-batches`` lets the operator catch up backlog > one batch in a single CLI call."""
    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=7)
    fake_repo = FakeDrainGraphRepository(available=True)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json", "--batch-size", "3", "--max-batches", "5"],
        drain_repo=fake_repo,
    )

    import json as _json

    assert code == 0
    envelope = _json.loads(stdout)
    # batch_size=3 over 7 rows → 3 + 3 + 1 = three ticks; fourth tick returns
    # pushed=0 and triggers early exit. We don't pin batches_run because
    # the early-exit semantics give us either 3 or 4 depending on whether
    # the final empty tick fires. Both are acceptable.
    assert envelope["pushed"] == 7, f"expected all 7 rows pushed; got {envelope['pushed']}"
    assert envelope["batches_run"] in (3, 4), f"expected 3 or 4 ticks; got {envelope['batches_run']}"


@pytest.mark.unit
def test_drain_dry_run_does_not_flip_flags(tmp_path: Path) -> None:
    """``--dry-run`` reports what would push but leaves flags at 0."""
    import sqlite3

    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=2)
    fake_repo = FakeDrainGraphRepository(available=True)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json", "--dry-run"],
        drain_repo=fake_repo,
    )

    import json as _json

    assert code == 0
    envelope = _json.loads(stdout)
    assert envelope["dry_run"] is True
    # Dry-run still reports the would-have-been pushed count.
    assert envelope["pushed"] == 2

    # Source-of-truth: the rows stay un-pushed.
    conn = sqlite3.connect(str(db_path))
    try:
        unpushed = conn.execute("SELECT COUNT(*) FROM entity_signals WHERE pushed_to_neo4j = 0").fetchone()[0]
    finally:
        conn.close()
    assert unpushed == 2, f"dry-run should not flip flags; got {unpushed} un-pushed (expected 2)"


@pytest.mark.unit
def test_drain_resolves_repo_via_repo_factory_when_drain_repo_omitted(tmp_path: Path) -> None:
    """The ``client_factory`` + ``repo_factory`` seams cover the production resolution path."""
    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=1)
    fake_repo = FakeDrainGraphRepository(available=True)
    sentinel_client: dict[str, int] = {"called": 0}

    def client_factory() -> Any:
        sentinel_client["called"] += 1
        return object()

    def repo_factory(_client: Any) -> Any:
        return fake_repo

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json"],
        client_factory=client_factory,
        repo_factory=repo_factory,
    )

    import json as _json

    assert code == 0
    envelope = _json.loads(stdout)
    assert envelope["pushed"] == 1
    assert sentinel_client["called"] == 1, "client_factory must be invoked exactly once when drain_repo omitted"


@pytest.mark.unit
def test_drain_neo4j_unavailable_exits_zero_with_envelope(tmp_path: Path) -> None:
    """When the repo reports unavailable, drain CLI exits 0 with neo4j_available=false."""
    from tests.fakes import FakeDrainGraphRepository

    db_path = _seed_drain_db(tmp_path, count=2)
    unavailable_repo = FakeDrainGraphRepository(available=False)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json"],
        drain_repo=unavailable_repo,
    )

    import json as _json

    assert code == 0
    envelope = _json.loads(stdout)
    assert envelope["neo4j_available"] is False
    assert envelope["pushed"] == 0


@pytest.mark.unit
def test_drain_protocols_module_imports_cleanly() -> None:
    """GH #334 — protocols module is import-only; this test exercises the import path.

    Without this test, ``kairix/core/curator/protocols.py`` shows 0% file
    coverage (F7). The module contains a Protocol declaration only; the
    smoke import is the canonical proof the module loads and the Protocol
    shape is well-formed.
    """
    from kairix.core.curator.protocols import DrainGraphRepository

    # Confirm the runtime-checkable Protocol has the expected attrs/methods.
    assert hasattr(DrainGraphRepository, "available")
    assert hasattr(DrainGraphRepository, "cypher")


@pytest.mark.unit
def test_drain_cli_runs_against_real_db_path_without_drain_repo_kwarg(tmp_path: Path) -> None:
    """The CLI drives the production default factory chain when ``drain_repo`` is omitted.

    Covers the ``_default_drain_db_factory`` + ``_default_drain_repo_factory``
    + ``_default_neo4j_client_factory`` production seams through the
    public CLI surface. Neo4j is unreachable in the test sandbox, so the
    drain reports ``neo4j_available=false`` and exits 0 — that's the
    contract the operator expects when the driver isn't installed.
    """
    import json as _json

    db_path = _seed_drain_db(tmp_path, count=1)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json"],
        # No drain_repo kwarg — exercises the default factory chain.
    )

    assert code == 0, f"drain CLI must exit 0 even with Neo4j unavailable; got {code}"
    envelope = _json.loads(stdout)
    # Neo4j driver not installed in the test sandbox → unavailable.
    assert envelope["neo4j_available"] is False
    assert envelope["pushed"] == 0
    # Staged row stays un-pushed.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path))
    try:
        unpushed = conn.execute("SELECT COUNT(*) FROM entity_signals WHERE pushed_to_neo4j = 0").fetchone()[0]
    finally:
        conn.close()
    assert unpushed == 1, f"row should stay un-pushed when Neo4j unavailable; got {unpushed} un-pushed"


def _rejecting_neo4j_client() -> Any:
    """A real Neo4jClient whose (fake) driver rejects every query."""
    from kairix.knowledge.graph.client import Neo4jClient
    from tests.fakes import FakeNeo4jDriverClass

    return Neo4jClient(
        uri="bolt://graph.invalid:7687",
        user="neo4j",
        password="unit-fixture",  # pragma: allowlist secret — test fixture
        driver_cls=FakeNeo4jDriverClass(run_raises=RuntimeError("constraint violation")),
    )


@pytest.mark.unit
def test_drain_default_repo_marks_rejected_merge_failed_not_pushed(tmp_path: Path) -> None:
    """With no injected repo, the CLI wraps the client in the production
    drain repository — a MERGE Neo4j rejects is reported failed and the
    signal stays un-acked (PLA-472 data-loss regression).

    Sabotage proof (executed): return ``Neo4jGraphRepository(client)``
    (read path, swallows errors) from ``_default_drain_repo_factory`` →
    ``pushed == 1`` / ``failed == 0`` and this test fails. Restored.
    """
    import json as _json
    import sqlite3

    db_path = _seed_drain_db(tmp_path, count=1)

    stdout, _stderr, code = _drive(
        ["drain", "--db-path", str(db_path), "--format", "json"],
        neo4j_client=_rejecting_neo4j_client(),
    )

    assert code == 0
    envelope = _json.loads(stdout)
    assert (envelope["pushed"], envelope["failed"]) == (0, 1)
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT pushed_to_neo4j FROM entity_signals").fetchone()[0] == -1
    finally:
        conn.close()
