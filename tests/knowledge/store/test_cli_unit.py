"""Unit tests for the kairix.knowledge.store CLI.

The BDD layer covers dry-run / --json / no-subcommand paths. These unit
tests fill the remaining branches:

  - ``_resolve_document_root`` with no arg + no env var → exit 1.
  - ``_resolve_document_root`` reading from KAIRIX_DOCUMENT_ROOT env var.
  - ``_cmd_crawl`` verbose-logging branch.
  - ``_cmd_crawl`` no-injection branch (calls get_client()).
  - ``_cmd_crawl`` Neo4j-unavailable warning + auto dry-run.
  - ``_cmd_crawl`` errors-list exit-1 branch.
  - ``_cmd_health`` no-injection branch.
  - ``_cmd_health`` text-format output branch.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import kairix.knowledge.store.cli as store_cli
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.unit


def _drive(args: list[str], **kw: Any) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            store_cli.main(args, **kw)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.fixture
def _no_docroot_env():
    """Remove KAIRIX_DOCUMENT_ROOT from env for the duration of the test."""
    prev = os.environ.pop("KAIRIX_DOCUMENT_ROOT", None)
    try:
        yield
    finally:
        if prev is not None:
            os.environ["KAIRIX_DOCUMENT_ROOT"] = prev


def test_resolve_document_root_exits_1_when_missing(_no_docroot_env) -> None:
    """_resolve_document_root with no arg + no env → exit 1."""
    err = io.StringIO()
    with pytest.raises(SystemExit) as info, redirect_stderr(err):
        store_cli._resolve_document_root(None)
    assert info.value.code == 1
    assert "KAIRIX_DOCUMENT_ROOT" in err.getvalue()


def test_resolve_document_root_returns_env_value(_no_docroot_env, tmp_path: Path) -> None:
    """When --document-root is missing, the env var is consulted."""
    os.environ["KAIRIX_DOCUMENT_ROOT"] = str(tmp_path)
    try:
        assert store_cli._resolve_document_root(None) == str(tmp_path)
    finally:
        del os.environ["KAIRIX_DOCUMENT_ROOT"]


def test_resolve_document_root_arg_wins_over_env(_no_docroot_env) -> None:
    os.environ["KAIRIX_DOCUMENT_ROOT"] = "/env-path"
    try:
        # Arg takes precedence over env var.
        assert store_cli._resolve_document_root("/arg-path") == "/arg-path"
    finally:
        del os.environ["KAIRIX_DOCUMENT_ROOT"]


# ---------------------------------------------------------------------------
# _cmd_crawl branches
# ---------------------------------------------------------------------------


def test_crawl_verbose_path_runs_to_exit_0(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    import kairix.knowledge.store.crawler as crawler

    monkeypatch.setattr(
        crawler,
        "crawl",
        lambda **kw: SimpleNamespace(
            dry_run=True,
            organisations_found=0,
            organisations_upserted=0,
            persons_found=0,
            persons_upserted=0,
            outcomes_found=0,
            outcomes_upserted=0,
            edges_found=0,
            edges_upserted=0,
            errors=[],
        ),
    )

    _stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--dry-run", "--verbose"],
        neo4j_client=FakeNeo4jClient(entities=[]),
    )
    assert code == 0


def test_crawl_default_neo4j_client_calls_get_client(monkeypatch, tmp_path: Path) -> None:
    """When no neo4j_client passed, _cmd_crawl resolves via graph_client.get_client."""
    from types import SimpleNamespace

    import kairix.knowledge.graph.client as graph_client
    import kairix.knowledge.store.crawler as crawler

    fake = FakeNeo4jClient(entities=[])
    monkeypatch.setattr(graph_client, "get_client", lambda: fake)
    seen_clients: list[Any] = []

    def _crawl(**kw: Any) -> Any:
        seen_clients.append(kw["neo4j_client"])
        return SimpleNamespace(
            dry_run=False,
            organisations_found=2,
            organisations_upserted=2,
            persons_found=0,
            persons_upserted=0,
            outcomes_found=0,
            outcomes_upserted=0,
            edges_found=1,
            edges_upserted=1,
            errors=[],
        )

    monkeypatch.setattr(crawler, "crawl", _crawl)

    _stdout, _stderr, code = _drive(["crawl", "--document-root", str(tmp_path)])
    assert code == 0
    assert seen_clients == [fake]


def test_crawl_neo4j_unavailable_forces_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    """When neo4j_client.available is False and dry_run is False, CLI auto-flips to dry-run."""
    from types import SimpleNamespace

    import kairix.knowledge.store.crawler as crawler

    seen: list[bool] = []

    def _crawl(**kw: Any) -> Any:
        seen.append(kw["dry_run"])
        return SimpleNamespace(
            dry_run=True,
            organisations_found=0,
            organisations_upserted=0,
            persons_found=0,
            persons_upserted=0,
            outcomes_found=0,
            outcomes_upserted=0,
            edges_found=0,
            edges_upserted=0,
            errors=[],
        )

    monkeypatch.setattr(crawler, "crawl", _crawl)

    class _Unavailable(FakeNeo4jClient):
        available = False

    _stdout, stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=_Unavailable(entities=[]),
    )
    assert code == 0
    assert seen == [True], "expected dry_run to flip True when Neo4j unavailable"
    assert "Neo4j unavailable" in stderr


def test_crawl_with_errors_exits_1(monkeypatch, tmp_path: Path) -> None:
    """When the crawl report has errors, CLI prints them and exits 1."""
    from types import SimpleNamespace

    import kairix.knowledge.store.crawler as crawler

    monkeypatch.setattr(
        crawler,
        "crawl",
        lambda **kw: SimpleNamespace(
            dry_run=False,
            organisations_found=1,
            organisations_upserted=1,
            persons_found=0,
            persons_upserted=0,
            outcomes_found=0,
            outcomes_upserted=0,
            edges_found=0,
            edges_upserted=0,
            errors=["err1: invalid frontmatter", "err2: missing field"],
        ),
    )

    stdout, stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=FakeNeo4jClient(entities=[]),
    )
    assert code == 1
    assert "Errors (2)" in stdout
    assert "err1" in stderr
    assert "err2" in stderr


def test_crawl_non_dry_run_prints_upsert_counts(monkeypatch, tmp_path: Path) -> None:
    """Non-dry-run report prints the 'upserted' continuation on each counter line."""
    from types import SimpleNamespace

    import kairix.knowledge.store.crawler as crawler

    monkeypatch.setattr(
        crawler,
        "crawl",
        lambda **kw: SimpleNamespace(
            dry_run=False,
            organisations_found=2,
            organisations_upserted=2,
            persons_found=1,
            persons_upserted=1,
            outcomes_found=3,
            outcomes_upserted=3,
            edges_found=4,
            edges_upserted=4,
            errors=[],
        ),
    )

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=FakeNeo4jClient(entities=[]),
    )
    assert code == 0
    assert "Organisations: 2 found, 2 upserted" in stdout
    assert "Persons:       1 found, 1 upserted" in stdout
    assert "Outcomes:      3 found, 3 upserted" in stdout
    assert "Edges:         4 found, 4 upserted" in stdout


# ---------------------------------------------------------------------------
# _cmd_health branches
# ---------------------------------------------------------------------------


def test_health_text_format_prints_human_summary(monkeypatch) -> None:
    """Without --json, _cmd_health prints the human-readable summary via format_health_text."""
    from types import SimpleNamespace

    import kairix.knowledge.store.health as health_mod

    fake_report = SimpleNamespace(
        ok=True,
        neo4j_available=True,
        total_entities=5,
        organisations=2,
        persons=2,
        outcomes=1,
        edges=3,
        document_root="/d",
        issues=[],
    )
    fake_report.ok = True  # property-like
    fake_report.total_entities = 5

    monkeypatch.setattr(health_mod, "run_store_health", lambda **kw: fake_report)
    monkeypatch.setattr(health_mod, "format_health_text", lambda r: "HEALTH-TEXT")

    stdout, _stderr, code = _drive(["health"], neo4j_client=FakeNeo4jClient(entities=[]))
    assert code == 0
    assert "HEALTH-TEXT" in stdout


def test_health_default_neo4j_client_calls_get_client(monkeypatch) -> None:
    """When no neo4j_client passed, _cmd_health resolves via graph_client.get_client."""
    from types import SimpleNamespace

    import kairix.knowledge.graph.client as graph_client
    import kairix.knowledge.store.health as health_mod

    fake = FakeNeo4jClient(entities=[])
    monkeypatch.setattr(graph_client, "get_client", lambda: fake)

    seen: list[Any] = []

    def _run(**kw: Any) -> Any:
        seen.append(kw["neo4j_client"])
        return SimpleNamespace(
            ok=False,
            neo4j_available=False,
            total_entities=0,
            organisations=0,
            persons=0,
            outcomes=0,
            edges=0,
            document_root=None,
            issues=["Neo4j unavailable"],
        )

    monkeypatch.setattr(health_mod, "run_store_health", _run)
    monkeypatch.setattr(health_mod, "format_health_text", lambda r: "bad")

    _stdout, _stderr, code = _drive(["health"])
    # ok=False → exit 1.
    assert code == 1
    assert seen == [fake]
