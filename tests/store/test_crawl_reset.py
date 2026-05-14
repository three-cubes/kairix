"""Unit tests for ``kairix store crawl --reset`` — closes #262.

Drives the CLI through its public surface (:func:`store_cli.main`) with
the ``FakeNeo4jClient`` injected via the documented seam. Each test sits
on top of the ``noninteractive`` kwarg seam — never ``monkeypatch.setenv``
— so the CLI's confirm/noninteractive interlock is exercised without
touching the live environment (F2-clean).

Sabotage-proof (per ``feedback_no_assertions_that_pass_either_way``):
inverting any of the asserted invariants here flips at least one test
red. Mutating the crawler so ``reset_graph()`` runs even on dry-run
trips :func:`test_reset_dry_run_does_not_invoke_reset_graph`; removing
the ``--confirm`` gate trips
:func:`test_reset_without_confirm_refuses_in_interactive_mode`; replacing
``args.reset`` with ``False`` in the CLI trips
:func:`test_reset_with_confirm_invokes_reset_graph_and_reports_counts`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kairix.knowledge.store.cli as store_cli
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.unit


def _drive(
    args: list[str],
    *,
    neo4j_client: Any,
    noninteractive: bool | None = None,
) -> tuple[str, str, int]:
    """Invoke ``store_cli.main`` and capture stdout/stderr/exit-code."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            store_cli.main(args, neo4j_client=neo4j_client, noninteractive=noninteractive)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def _stub_crawl(monkeypatch: pytest.MonkeyPatch, **extra: Any) -> list[dict[str, Any]]:
    """Replace ``crawler.crawl`` with a recorder + canned-report stub.

    Returns the list of recorded kwargs so tests can assert the CLI
    forwarded the right flags. Extra report fields can be supplied via
    ``**extra`` (e.g. ``reset_nodes_deleted=...``) without touching every
    test's default report.
    """
    import kairix.knowledge.store.crawler as crawler

    captured: list[dict[str, Any]] = []

    def _record(**kw: Any) -> Any:
        captured.append(dict(kw))
        # The CLI calls reset_graph on the client directly via crawl();
        # the stub mirrors that so the FakeNeo4jClient call recorder
        # still ticks (otherwise the CLI's dry-run-vs-execute branch
        # would short-circuit reset entirely and break sabotage).
        if kw.get("reset") and not kw.get("dry_run"):
            kw["neo4j_client"].reset_graph()
        defaults: dict[str, Any] = {
            "dry_run": kw.get("dry_run", False),
            "organisations_found": 0,
            "organisations_upserted": 0,
            "persons_found": 0,
            "persons_upserted": 0,
            "outcomes_found": 0,
            "outcomes_upserted": 0,
            "edges_found": 0,
            "edges_upserted": 0,
            "errors": [],
            "reset_nodes_deleted": None,
            "reset_relationships_deleted": None,
            "override_coverage": None,
            "override_coverage_path": None,
        }
        if kw.get("reset"):
            if kw.get("dry_run"):
                defaults["reset_nodes_deleted"] = 0
                defaults["reset_relationships_deleted"] = 0
            else:
                defaults["reset_nodes_deleted"] = 7
                defaults["reset_relationships_deleted"] = 11
        defaults.update(extra)
        return SimpleNamespace(**defaults)

    monkeypatch.setattr(crawler, "crawl", _record)
    return captured


def test_reset_without_confirm_refuses_in_interactive_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--reset`` alone, no env var set, must refuse with exit 2 and an actionable message."""
    captured = _stub_crawl(monkeypatch)
    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    stdout, stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--reset"],
        neo4j_client=fake,
        noninteractive=False,
    )

    assert code == 2, f"expected refusal exit 2, got {code}"
    assert captured == [], "crawl() must not run when reset is refused"
    assert fake.reset_graph_calls == 0, "reset_graph must not fire when refused"
    assert "--confirm" in stderr
    # F21 affordance — failure messages must include an action marker.
    assert "run:" in stderr.lower()
    assert "destructive" in stderr.lower() or "--reset" in stderr
    del stdout  # Intentionally unused — exit code + stderr carry the contract.


def test_reset_with_confirm_invokes_reset_graph_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--reset --confirm`` invokes ``reset_graph()`` and the summary prints the deletion counts."""
    captured = _stub_crawl(monkeypatch)
    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--reset", "--confirm"],
        neo4j_client=fake,
        noninteractive=False,
    )

    assert code == 0
    assert captured and captured[0]["reset"] is True
    assert fake.reset_graph_calls == 1, "reset_graph must fire exactly once"
    assert "Reset: deleted" in stdout
    # The fake's default tuple is (len(entities), len(entities)-1); the stub
    # uses (7, 11) when reset is requested.
    assert "7 entities" in stdout
    assert "11 relationships" in stdout


def test_reset_noninteractive_kwarg_bypasses_confirm_requirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``noninteractive=True`` kwarg bypasses ``--confirm`` without any env var read.

    This is the F2-clean replacement for ``monkeypatch.setenv("KAIRIX_NONINTERACTIVE", "1")``
    — tests pass the bool directly through the documented seam.
    """
    captured = _stub_crawl(monkeypatch)
    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--reset"],
        neo4j_client=fake,
        noninteractive=True,
    )

    assert code == 0, "noninteractive bypass should let --reset through with exit 0"
    assert captured and captured[0]["reset"] is True
    assert fake.reset_graph_calls == 1
    assert "Reset: deleted" in stdout


def test_reset_dry_run_does_not_invoke_reset_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--reset --confirm --dry-run`` reports intent but never touches the live graph."""
    captured = _stub_crawl(monkeypatch)
    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path), "--reset", "--confirm", "--dry-run"],
        neo4j_client=fake,
        noninteractive=False,
    )

    assert code == 0
    assert captured and captured[0]["dry_run"] is True and captured[0]["reset"] is True
    # In dry-run mode the crawl() function never calls reset_graph on the client.
    assert fake.reset_graph_calls == 0
    assert "dry run" in stdout.lower()


def test_crawl_without_reset_flag_leaves_graph_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A plain ``kairix store crawl`` must not touch ``reset_graph``."""
    captured = _stub_crawl(monkeypatch)
    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    _stdout, _stderr, code = _drive(
        ["crawl", "--document-root", str(tmp_path)],
        neo4j_client=fake,
        noninteractive=False,
    )

    assert code == 0
    assert captured and captured[0]["reset"] is False
    assert fake.reset_graph_calls == 0


def test_crawl_reset_calls_reset_graph_directly_via_crawler(
    tmp_path: Path,
) -> None:
    """End-to-end through the real ``crawl()`` function: ``reset=True`` clears Neo4j first.

    Uses the live ``crawl`` (no stub) against an empty document root so the
    only side-effect is the reset path. Exercises the report wiring without
    needing the wikilink / org / person scanners to produce output.
    """
    from kairix.knowledge.store.crawler import crawl

    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    report = crawl(document_root=tmp_path, neo4j_client=fake, reset=True)

    assert fake.reset_graph_calls == 1
    # Default FakeNeo4jClient.reset_graph_returns = (len(entities), len(entities)-1).
    assert report.reset_nodes_deleted == 1
    assert report.reset_relationships_deleted == 0


def test_crawl_reset_dry_run_skips_reset_graph_via_crawler(tmp_path: Path) -> None:
    """``dry_run=True`` short-circuits the destructive call even when ``reset=True``."""
    from kairix.knowledge.store.crawler import crawl

    fake = FakeNeo4jClient(entities=[{"id": "x", "name": "x", "label": "Organisation"}])

    report = crawl(document_root=tmp_path, neo4j_client=fake, reset=True, dry_run=True)

    assert fake.reset_graph_calls == 0
    assert report.reset_nodes_deleted == 0
    assert report.reset_relationships_deleted == 0
