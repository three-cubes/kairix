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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from kairix.knowledge.store.cli import StoreCliDeps
from kairix.knowledge.store.cli import main as store_cli_main
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.unit


def _drive(args: list[str], **kw: Any) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            store_cli_main(args, **kw)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def test_crawl_exits_1_when_document_root_missing() -> None:
    """Public surface: ``kairix store crawl`` with no --document-root and
    no KAIRIX_DOCUMENT_ROOT env → exit 1 with the expected error.

    Sabotage: drop the ``sys.exit(1)`` in ``_resolve_document_root`` →
    the CLI proceeds and code is no longer 1 / stderr message is missing.
    """
    _stdout, stderr, code = _drive(["crawl"], deps=StoreCliDeps(environ={}))
    assert code == 1
    assert "KAIRIX_DOCUMENT_ROOT" in stderr


def test_crawl_uses_document_root_from_env_when_arg_missing(tmp_path: Path) -> None:
    """Public surface: when --document-root is omitted, the env var is the
    fallback the crawler sees.

    Sabotage: drop the ``env = document_root_override(); if env: return env``
    branch → the crawler captures None / wrong value, this assertion fails.
    """
    env = {"KAIRIX_DOCUMENT_ROOT": str(tmp_path)}
    captured: dict[str, Any] = {}

    def _capturing_crawl(**kw: Any) -> Any:
        captured["document_root"] = kw["document_root"]
        from types import SimpleNamespace

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

    _stdout, _stderr, code = _drive(
        ["crawl", "--dry-run"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        crawler=_capturing_crawl,
        deps=StoreCliDeps(environ=env),
    )
    assert code == 0
    assert captured["document_root"] == str(tmp_path)


def test_crawl_document_root_arg_wins_over_env(tmp_path: Path) -> None:
    """Public surface: --document-root takes precedence over the env var.

    Sabotage: invert the priority (``return env if env else arg``) → the
    crawler sees ``/env-path`` and this assertion fails.
    """
    arg_path = tmp_path / "arg-target"
    arg_path.mkdir()
    env = {"KAIRIX_DOCUMENT_ROOT": "/env-path"}
    captured: dict[str, Any] = {}

    def _capturing_crawl(**kw: Any) -> Any:
        captured["document_root"] = kw["document_root"]
        from types import SimpleNamespace

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

    _stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(arg_path), "--dry-run"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        crawler=_capturing_crawl,
        deps=StoreCliDeps(environ=env),
    )
    assert code == 0
    assert captured["document_root"] == str(arg_path)


# ---------------------------------------------------------------------------
# _cmd_crawl branches
# ---------------------------------------------------------------------------


def test_crawl_verbose_path_runs_to_exit_0(tmp_path: Path) -> None:
    from types import SimpleNamespace

    def _fake_crawl(**_kw: Any) -> Any:
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

    _stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--dry-run", "--verbose"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        deps=StoreCliDeps(crawl_fn=_fake_crawl),
    )
    assert code == 0


def test_crawl_default_neo4j_client_calls_get_client(tmp_path: Path) -> None:
    """When no neo4j_client passed, _cmd_crawl resolves via deps.get_neo4j_client_fn.

    Sabotage: drop the ``neo4j_client = d.get_neo4j_client_fn()`` line in
    ``_cmd_crawl`` and ``seen_clients`` no longer contains the fake.
    """
    from types import SimpleNamespace

    fake = FakeNeo4jClient(entities=[])
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

    _stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        deps=StoreCliDeps(crawl_fn=_crawl, get_neo4j_client_fn=lambda: fake),
    )
    assert code == 0
    assert seen_clients == [fake]


def test_crawl_neo4j_unavailable_forces_dry_run(tmp_path: Path) -> None:
    """When neo4j_client.available is False and dry_run is False, CLI auto-flips to dry-run."""
    from types import SimpleNamespace

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

    class _Unavailable(FakeNeo4jClient):
        available = False

    _stdout, stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=_Unavailable(entities=[]),
        deps=StoreCliDeps(crawl_fn=_crawl),
    )
    assert code == 0
    assert seen == [True], "expected dry_run to flip True when Neo4j unavailable"
    assert "Neo4j unavailable" in stderr


def test_crawl_with_errors_exits_1(tmp_path: Path) -> None:
    """When the crawl report has errors, CLI prints them and exits 1."""
    from types import SimpleNamespace

    def _crawl(**_kw: Any) -> Any:
        return SimpleNamespace(
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
        )

    stdout, stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=FakeNeo4jClient(entities=[]),
        deps=StoreCliDeps(crawl_fn=_crawl),
    )
    assert code == 1
    assert "Errors (2)" in stdout
    assert "err1" in stderr
    assert "err2" in stderr


def test_crawl_non_dry_run_prints_upsert_counts(tmp_path: Path) -> None:
    """Non-dry-run report prints the 'upserted' continuation on each counter line."""
    from types import SimpleNamespace

    def _crawl(**_kw: Any) -> Any:
        return SimpleNamespace(
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
        )

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=FakeNeo4jClient(entities=[]),
        deps=StoreCliDeps(crawl_fn=_crawl),
    )
    assert code == 0
    assert "Organisations: 2 found, 2 upserted" in stdout
    assert "Persons:       1 found, 1 upserted" in stdout
    assert "Outcomes:      3 found, 3 upserted" in stdout
    assert "Edges:         4 found, 4 upserted" in stdout


def test_crawl_prints_override_coverage_summary(tmp_path: Path) -> None:
    """When the crawl report carries override_coverage, the CLI prints the summary lines.

    Drives ``main()`` through the public ``crawler`` seam (no monkey-patching).
    Sabotage: drop ``_print_override_coverage(report)`` from ``_print_crawl_report``
    and the override-coverage line disappears from stdout.
    """
    from types import SimpleNamespace

    fake_report = SimpleNamespace(
        dry_run=True,
        organisations_found=1,
        organisations_upserted=0,
        persons_found=0,
        persons_upserted=0,
        outcomes_found=0,
        outcomes_upserted=0,
        edges_found=0,
        edges_upserted=0,
        errors=[],
        override_coverage=SimpleNamespace(
            matched=3,
            total_overrides=5,
            never_matched=["alpha-org", "beta-org"],
        ),
        override_coverage_path="/tmp/override-coverage.json",
    )

    def _fake_crawl(**_kw: Any) -> Any:
        return fake_report

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--dry-run"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        crawler=_fake_crawl,
    )
    assert code == 0
    assert "Override coverage: 3/5 overrides matched (2 never used)" in stdout
    assert "Never-matched: ['alpha-org', 'beta-org']" in stdout
    assert "Coverage report written: /tmp/override-coverage.json" in stdout


def test_crawl_override_coverage_truncates_long_never_matched_list(tmp_path: Path) -> None:
    """Override-coverage prints first 10 never-matched names + "+N more" suffix.

    Sabotage: remove the ``suffix = "" if len(never) <= 10`` line and a list
    of 12 names prints all 12 without the "+2 more" tail.
    """
    from types import SimpleNamespace

    never = [f"org-{i:02d}" for i in range(12)]
    fake_report = SimpleNamespace(
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
        override_coverage=SimpleNamespace(
            matched=0,
            total_overrides=12,
            never_matched=never,
        ),
        override_coverage_path=None,
    )

    def _fake_crawl(**_kw: Any) -> Any:
        return fake_report

    stdout, _stderr, _code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--dry-run"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        crawler=_fake_crawl,
    )
    assert "+2 more" in stdout


# ---------------------------------------------------------------------------
# _cmd_health branches
# ---------------------------------------------------------------------------


def test_health_text_format_prints_human_summary() -> None:
    """Without --json, _cmd_health prints the human-readable summary via format_health_text."""
    from types import SimpleNamespace

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

    def _run(**_kw: Any) -> Any:
        return fake_report

    def _fmt(_r: Any) -> str:
        return "HEALTH-TEXT"

    stdout, _stderr, code = _drive(
        ["health"],
        neo4j_client=FakeNeo4jClient(entities=[]),
        deps=StoreCliDeps(run_store_health_fn=_run, format_health_text_fn=_fmt),
    )
    assert code == 0
    assert "HEALTH-TEXT" in stdout


def test_health_default_neo4j_client_calls_get_client() -> None:
    """When no neo4j_client passed, _cmd_health resolves via deps.get_neo4j_client_fn.

    Sabotage: drop the ``neo4j_client = d.get_neo4j_client_fn()`` line in
    ``_cmd_health`` and ``seen`` stays empty.
    """
    from types import SimpleNamespace

    fake = FakeNeo4jClient(entities=[])
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

    def _fmt(_r: Any) -> str:
        return "bad"

    _stdout, _stderr, code = _drive(
        ["health"],
        deps=StoreCliDeps(
            get_neo4j_client_fn=lambda: fake,
            run_store_health_fn=_run,
            format_health_text_fn=_fmt,
        ),
    )
    # ok=False → exit 1.
    assert code == 1
    assert seen == [fake]
