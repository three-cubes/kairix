"""Unit tests for kairix.knowledge.summaries.cli.

The BDD suite covers ``--status`` + the empty-vault rejection paths. This
module drives the remaining branches:

  - ``_get_cred`` (delegates to secrets.get_secret).
  - ``_run_generate`` happy path (generate_summaries + write_summary).
  - ``main()`` --all / --stale / --path with credential resolution.
  - ``main()`` credential-failure branch.
  - ``main()`` default ``document_root`` / ``db_path`` resolution (when
    the test omits them).
  - ``__main__`` guard.
"""

from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import kairix.knowledge.summaries.cli as sum_cli
from kairix.knowledge.summaries.cli import SummariesCliDeps

pytestmark = pytest.mark.unit


def _drive(args: list[str], **kw: Any) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            sum_cli.main(args, **kw)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def _fake_creds(api_key: str = "k", endpoint: str = "https://x") -> Any:
    from types import SimpleNamespace

    def _get(_kind: str) -> Any:
        return SimpleNamespace(api_key=api_key, endpoint=endpoint)

    return _get


def _init_db(path: Path) -> None:
    from kairix.knowledge.summaries.staleness import init_summaries_db

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    init_summaries_db(conn)
    conn.close()


# ---------------------------------------------------------------------------
# run_generate path — drives the same behaviour through the public ``main()``
# surface (``--all`` → discovers vault → fetches creds → runs generate →
# persists each result). The previously-private ``_run_generate`` helper is
# exercised indirectly: per-result persistence + the "Done: N / N succeeded"
# summary land in stdout. F5-clean — no module-level private access.
# ---------------------------------------------------------------------------


def test_main_all_persists_each_result_and_prints_done_summary(tmp_path: Path) -> None:
    """Drive the public surface: ``main(['--all'], ...)`` discovers two
    vault docs, fetches credentials, generates summaries, and persists
    each result.

    Sabotage: drop the ``for result in results: deps.write_summary_fn(result, db)``
    loop in ``_run_generate`` → ``written`` stays empty + this assertion
    fires.
    """
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    (doc_root / "b.md").write_text("# b", encoding="utf-8")
    db_path = tmp_path / "summaries.db"
    _init_db(db_path)

    written: list[Any] = []

    def _write(r: Any, _db: sqlite3.Connection) -> None:
        written.append(r)

    def _gen(*, paths: list[str], **_kw: Any) -> list[Any]:
        return [{"path": p} for p in paths]

    stdout, _stderr, code = _drive(
        ["--all", "--include-l1"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(
            get_credentials_fn=_fake_creds(),
            write_summary_fn=_write,
            generate_summaries_fn=_gen,
        ),
    )
    assert code == 0
    assert len(written) == 2
    assert "Done: 2 / 2 succeeded" in stdout


# ---------------------------------------------------------------------------
# main() branches
# ---------------------------------------------------------------------------


def test_main_status_branch_prints_counters(tmp_path: Path) -> None:
    db_path = tmp_path / "summaries.db"
    _init_db(db_path)
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")

    stdout, _stderr, code = _drive(
        ["--status"],
        document_root=doc_root,
        db_path=db_path,
    )
    assert code == 0
    assert "Vault docs:" in stdout
    assert "With L0:" in stdout


def test_main_all_branch_generates_summaries(tmp_path: Path) -> None:
    """--all enumerates vault, fetches creds, calls _run_generate."""
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    db_path = tmp_path / "summaries.db"
    _init_db(db_path)

    written: list[Any] = []

    def _write(r: Any, _db: sqlite3.Connection) -> None:
        written.append(r)

    def _gen(*, paths: list[str], **_kw: Any) -> list[Any]:
        return [{"path": p} for p in paths]

    stdout, _stderr, code = _drive(
        ["--all"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(
            get_credentials_fn=_fake_creds(endpoint="e"),
            write_summary_fn=_write,
            generate_summaries_fn=_gen,
        ),
    )
    assert code == 0
    assert len(written) == 1
    assert "Done: 1 / 1" in stdout


def test_main_all_exits_1_when_vault_empty(tmp_path: Path) -> None:
    doc_root = tmp_path / "empty"
    doc_root.mkdir()
    db_path = tmp_path / "s.db"
    _init_db(db_path)
    _stdout, stderr, code = _drive(
        ["--all"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(get_credentials_fn=_fake_creds(endpoint="e")),
    )
    assert code == 1
    assert "No vault docs found" in stderr


def test_main_stale_branch_when_nothing_stale_returns_clean(tmp_path: Path) -> None:
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    db_path = tmp_path / "s.db"
    _init_db(db_path)

    def _stale(_all_paths: list[str], _db: sqlite3.Connection) -> list[str]:
        return []

    stdout, _stderr, code = _drive(
        ["--stale"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(
            get_credentials_fn=_fake_creds(endpoint="e"),
            get_stale_paths_fn=_stale,
        ),
    )
    assert code == 0
    assert "Stale/missing: 0 of 1" in stdout
    assert "Nothing to do" in stdout


def test_main_stale_branch_generates_when_stale_paths_exist(tmp_path: Path) -> None:
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    (doc_root / "b.md").write_text("# b", encoding="utf-8")
    db_path = tmp_path / "s.db"
    _init_db(db_path)

    written: list[Any] = []

    def _stale(all_paths: list[str], _db: sqlite3.Connection) -> list[str]:
        return list(all_paths)

    def _write(r: Any, _db: sqlite3.Connection) -> None:
        written.append(r)

    def _gen(*, paths: list[str], **_kw: Any) -> list[Any]:
        return [{"path": p} for p in paths]

    stdout, _stderr, code = _drive(
        ["--stale"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(
            get_credentials_fn=_fake_creds(endpoint="e"),
            get_stale_paths_fn=_stale,
            write_summary_fn=_write,
            generate_summaries_fn=_gen,
        ),
    )
    assert code == 0
    assert len(written) == 2
    assert "Stale/missing: 2 of 2" in stdout


def test_main_path_branch_for_existing_file(tmp_path: Path) -> None:
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    target = doc_root / "one.md"
    target.write_text("# one", encoding="utf-8")

    db_path = tmp_path / "s.db"
    _init_db(db_path)

    def _write(_r: Any, _db: sqlite3.Connection) -> None:
        return None

    def _gen(*, paths: list[str], **_kw: Any) -> list[Any]:
        return [{"p": p} for p in paths]

    _stdout, _stderr, code = _drive(
        ["--path", str(target)],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(
            get_credentials_fn=_fake_creds(endpoint="e"),
            write_summary_fn=_write,
            generate_summaries_fn=_gen,
        ),
    )
    assert code == 0


def test_main_path_exits_1_when_file_missing(tmp_path: Path) -> None:
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    db_path = tmp_path / "s.db"
    _init_db(db_path)

    _stdout, stderr, code = _drive(
        ["--path", str(tmp_path / "absent.md")],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(get_credentials_fn=_fake_creds(endpoint="e")),
    )
    assert code == 1
    assert "File not found" in stderr


def test_summaries_cache_inside_allowed_roots_is_accepted(tmp_path: Path) -> None:
    """``--summaries-cache`` under an allowed root (tmp) opens the DB and runs.

    Legitimate-value half of the S8706 confinement contract: a cache path under
    pytest's temp dir is accepted and ``--status`` reaches its coverage output.
    """
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    cache = tmp_path / "nested" / "summaries.db"

    stdout, _stderr, code = _drive(
        ["--status", "--summaries-cache", str(cache)],
        document_root=doc_root,
    )
    assert code == 0
    assert "Vault docs:" in stdout
    assert cache.exists()


def test_summaries_cache_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    """A ``--summaries-cache`` path that escapes every root raises before connect.

    Sabotage: drop the ``confine_to_roots`` guard in ``_open_db`` and this
    traversal reaches ``sqlite3.connect`` (pythonsecurity:S8706). ``/etc/...``
    is not under cwd, home, the temp dir, or the configured summaries dir, so
    confinement rejects it before any DB file is created.
    """
    from kairix.paths import PathTraversalError

    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    escape = Path("/etc") / "kairix-summaries-escape" / "s.db"

    with pytest.raises(PathTraversalError):
        sum_cli.main(["--status", "--summaries-cache", str(escape)], document_root=doc_root)

    assert not escape.exists()


def test_main_credential_failure_exits_1(tmp_path: Path) -> None:
    """When get_credentials raises, the CLI prints the error and exits 1."""

    def _raises(_kind: str) -> Any:
        raise RuntimeError("no Azure key")

    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    db_path = tmp_path / "s.db"
    _init_db(db_path)

    _stdout, stderr, code = _drive(
        ["--all"],
        document_root=doc_root,
        db_path=db_path,
        deps=SummariesCliDeps(get_credentials_fn=_raises),
    )
    assert code == 1
    assert "Error: no Azure key" in stderr


def test_main_resolves_defaults_when_neither_kwarg_provided(tmp_path: Path) -> None:
    """When document_root and db_path are None, deps.document_root_fn /
    deps.summaries_db_path_fn are consulted (production defaults wire to
    kairix.paths).

    Sabotage: drop the ``deps.document_root_fn()`` / ``deps.summaries_db_path_fn()``
    fallbacks in ``_resolve_paths`` and the CLI either crashes (None
    paths) or never reaches the status branch.
    """
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    (doc_root / "a.md").write_text("# a", encoding="utf-8")
    db_path = tmp_path / "s.db"
    _init_db(db_path)

    _stdout, _stderr, code = _drive(
        ["--status"],
        deps=SummariesCliDeps(
            document_root_fn=lambda: doc_root,
            summaries_db_path_fn=lambda: db_path,
        ),
    )
    assert code == 0


# ---------------------------------------------------------------------------
# Production-default helpers — exercise the ``_default_*`` shims in the
# ``SummariesCliDeps`` so the per-file coverage floor (F7 ≥ 90%) stays
# satisfied. Each shim is a 2-line lazy-import wrapper; calling it once
# proves both the import and the delegation. F1-clean — no monkeypatch
# of kairix internals.
# ---------------------------------------------------------------------------


def test_default_document_root_path_delegates_to_kairix_paths(tmp_path: Path) -> None:
    """``default_document_root_path`` returns whatever ``kairix.paths.document_root``
    resolves. Drives the real ``KAIRIX_DOCUMENT_ROOT`` resolution through the
    ``env=`` mapping seam (F2-clean — no process-env mutation, no LRU-cache
    invalidation), exercising both the wrapper and the real resolution path.
    """
    from kairix.knowledge.summaries.cli import default_document_root_path

    assert default_document_root_path(env={"KAIRIX_DOCUMENT_ROOT": str(tmp_path)}) == tmp_path


def test_default_summaries_db_path_fn_delegates_to_kairix_paths() -> None:
    """``default_summaries_db_path_fn`` returns a Path resolved by kairix.paths."""
    from kairix.knowledge.summaries.cli import default_summaries_db_path_fn

    result = default_summaries_db_path_fn()
    assert isinstance(result, Path)


def test_default_get_stale_paths_delegates_through_real_staleness(tmp_path: Path) -> None:
    """``default_get_stale_paths`` hits the real staleness module. With
    an empty paths list it returns an empty list."""
    from kairix.knowledge.summaries.cli import default_get_stale_paths

    db_path = tmp_path / "stale.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        assert default_get_stale_paths([], conn) == []
    finally:
        conn.close()


def test_default_write_summary_delegates_through_real_staleness(tmp_path: Path) -> None:
    """``default_write_summary`` calls ``staleness.write_summary``; a
    non-dict input is rejected by the real impl with an AttributeError or
    similar — either way the wrapper's two lines execute."""
    from kairix.knowledge.summaries.cli import default_write_summary

    db_path = tmp_path / "write.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises((AttributeError, TypeError, KeyError, sqlite3.Error)):
            default_write_summary(object(), conn)
    finally:
        conn.close()


def test_default_get_credentials_delegates_through_real_credentials() -> None:
    """``default_get_credentials`` calls into real ``credentials.get_credentials``.
    With an unknown kind the real impl raises — proves the wrapper
    delegated, not silently swallowed."""
    from kairix.knowledge.summaries.cli import default_get_credentials

    with pytest.raises((ValueError, KeyError, RuntimeError, FileNotFoundError)):
        default_get_credentials("nonexistent_kind_xyz")


def test_default_generate_summaries_delegates_through_real_generator() -> None:
    """``default_generate_summaries`` calls ``generate.generate_summaries``.
    Missing required kwargs surface as TypeError from the real impl —
    confirms the wrapper delegated, not stubbed silently."""
    from kairix.knowledge.summaries.cli import default_generate_summaries

    with pytest.raises(TypeError):
        default_generate_summaries()


def test_module_main_guard() -> None:
    """Drive the ``__main__`` guard at the bottom of the file in a subprocess
    — a fresh interpreter, so no kairix module body re-executes in the test
    process (F1). With no mode flag main()'s argparse exits 2 with its usage
    line — proof the guard called main()."""
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.knowledge.summaries.cli"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert "usage: kairix summarise" in proc.stderr
