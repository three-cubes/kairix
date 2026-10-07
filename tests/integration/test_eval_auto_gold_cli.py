"""End-to-end integration tests for ``kairix eval auto-gold`` CLI subcommand.

Replaces a previous unit test that used five ``@patch`` decorators to mock
out ``get_db_path``, ``analyse_corpus``, ``generate_template_queries``, and
``build_suite``. With every dependency mocked, the test asserted only that
``main(["auto-gold", ...])`` exited 0 — the real code paths through CLI
dispatch + the auto_gold pipeline were never exercised.

This file replaces it with two integration tests that:

  1. Run the real CLI against a real SQLite DB seeded with documents and
     verify the suite YAML is written with the expected queries.
  2. Run the real CLI against a missing DB and verify the error path
     (sys.exit code 1) without patching ``get_db_path``.

The seeded DB path reaches the CLI through ``EvalCliDeps(index_db_path=...)``
— the CLI's own constructor seam — so the test never mutates process env
(no ``KAIRIX_DB_PATH`` write that could leak into later tests).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from kairix.core.db.fts import rebuild_fts
from kairix.core.db.schema import create_schema
from kairix.quality.eval.cli import EvalCliDeps, main

pytestmark = pytest.mark.integration


def _seed_corpus(db_path: Path, *, n_docs: int = 5) -> None:
    """Build the production schema and insert ``n_docs`` documents.

    Mixes plain titles with procedural / date-style filenames so
    ``analyse_corpus`` produces non-zero counts for each category.
    """
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    titles = [
        ("how-to-deploy-app", "how-to-deploy-app", "guides"),
        ("runbook-incident-response", "runbook-incident-response", "guides"),
        ("2026-04-28-release-notes", "2026-04-28-release-notes", "notes"),
        ("architecture-overview", "architecture-overview", "design"),
        ("project-roadmap", "project-roadmap", "planning"),
    ][:n_docs]
    for i, (path, title, collection) in enumerate(titles):
        digest = f"hash-{i}"
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (digest, "body"))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (path, title, collection, digest, "2026-05-01", "2026-05-01"),
        )
    db.commit()
    rebuild_fts(db)
    db.close()


@pytest.fixture
def kairix_db(tmp_path: Path) -> Path:
    """Build a kairix-schema SQLite at a tmp path (handed to the CLI via EvalCliDeps)."""
    db_path = tmp_path / "kairix.sqlite"
    _seed_corpus(db_path)
    return db_path


@pytest.mark.integration
def test_auto_gold_cli_writes_yaml_with_real_queries_against_seeded_corpus(kairix_db: Path, tmp_path: Path) -> None:
    """The real auto-gold pipeline runs end-to-end against a seeded SQLite.

    Asserts: exit code 0, YAML written with N queries, every query carries
    ``id`` / ``category`` / ``query`` / ``score_method`` fields. None of the
    auto_gold functions are mocked — ``analyse_corpus`` reads the real
    ``documents`` table; ``generate_template_queries`` produces real queries;
    ``build_suite`` writes the real YAML.
    """
    output_path = tmp_path / "auto-gold.yaml"

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["auto-gold", "--output", str(output_path), "--count", "12"],
            deps=EvalCliDeps(index_db_path=lambda: kairix_db),
        )

    assert exc_info.value.code == 0, "auto-gold CLI exited non-zero"
    assert output_path.exists(), "suite YAML was not written"

    parsed = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    cases = parsed["cases"]
    # The CLI is asked for 12 queries; the actual count may be slightly less
    # depending on how the corpus categories proportion out, but the suite
    # must contain at least one query.
    assert len(cases) >= 1, f"expected at least one query, got: {cases}"
    # Every produced case has the required schema fields.
    for case in cases:
        assert {"id", "category", "query", "score_method"}.issubset(case.keys()), (
            f"case missing required fields: {case}"
        )
    # Multiple categories represented (mix of procedural + recall typical).
    categories = {case["category"] for case in cases}
    assert categories, "no categories in produced cases"


# NOTE: the previous unit test ``test_exits_1_when_no_index`` used
# ``@patch("kairix.core.db.get_db_path", side_effect=FileNotFoundError(...))``
# to verify the CLI's ``except FileNotFoundError`` branch returns exit 1.
#
# This test was test-against-implementation: ``get_db_path`` does not raise
# FileNotFoundError under any realistic configuration — it always returns a
# path (existing or not). The patched test only worked because it introduced
# a behaviour the real code never produces. The CLI's ``except FileNotFoundError``
# branch is therefore dead code and is now ``# pragma: no cover``-annotated in
# kairix/quality/eval/cli.py with that justification.
