"""End-to-end integration tests for the recall quality gate.

Drives ``RecallChecker.check`` and ``run_recall_gate`` against a real
SQLite database with the production schema and a real on-disk recall log.
The embedding and vector-search surfaces inject fakes from tests/fakes.py
because production calls would hit Azure / usearch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.schema import create_schema
from kairix.core.embed.recall_check import (
    RecallChecker,
    load_previous_score,
    run_recall_gate,
    save_recall_result,
)
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

pytestmark = pytest.mark.integration


def _seed_real_db(db_path: Path, docs: list[tuple[str, str]]) -> sqlite3.Connection:
    """Create the production schema and insert ``(path, title)`` rows as active documents."""
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    cur = db.cursor()
    for i, (path, title) in enumerate(docs):
        cur.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (f"h{i}", "body"))
        cur.execute(
            "INSERT INTO documents (path, title, collection, hash, created_at, modified_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (path, title, "shared", f"h{i}", "2026-05-01", "2026-05-01"),
        )
    db.commit()
    return db


@pytest.mark.integration
def test_recall_checker_uses_adaptive_queries_against_real_schema(tmp_path: Path) -> None:
    """RecallChecker.check derives adaptive queries from a real production-schema DB."""
    db_path = tmp_path / "kairix.sqlite"
    db = _seed_real_db(
        db_path,
        [
            ("docs/architecture.md", "architecture"),
            ("docs/deploy-guide.md", "deploy-guide"),
            ("docs/testing.md", "testing"),
        ],
    )

    # Vector searcher returns the architecture path back so the architecture query hits.
    searcher = FakeVectorSearcher(paths=["docs/architecture.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=searcher)
    # canary_cache_path=None: bypass the persistent canary cache so this
    # integration test exercises adaptive sampling end-to-end.
    result = checker.check(db=db, canary_cache_path=None)

    db.close()

    assert result["total"] == 3, f"expected 3 adaptive queries, got: {result}"
    # The architecture query's gold fragment ("architecture") appears in the returned path.
    arch_detail = next(d for d in result["detail"] if "architecture" in d["query"])
    assert arch_detail["hit"] is True
    assert arch_detail["returned"] == ["docs/architecture.md"]
    # The other two queries miss because the searcher returns architecture for all of them.
    other_hits = [d["hit"] for d in result["detail"] if "architecture" not in d["query"]]
    assert other_hits == [False, False]
    assert result["passed"] == 1


@pytest.mark.integration
def test_run_recall_gate_writes_log_and_loads_score_round_trip(tmp_path: Path) -> None:
    """A first gate run writes the log; a second gate sees the previous score and compares."""
    log_path = tmp_path / "recall-check.json"

    # First run: no previous log → must pass; the run is appended to the log.
    class _FirstChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.80, "passed": 4, "total": 5, "timestamp": 0, "detail": []}

    first_passed, first_result = run_recall_gate(checker=_FirstChecker(), log_path=log_path, alert_callback=None)
    assert first_passed is True
    assert first_result["score"] == pytest.approx(0.80)
    assert load_previous_score(log_path) == pytest.approx(0.80)

    # Second run: previous=0.80, current=0.50 → delta -0.30 (degraded) → fail + alert.
    class _SecondChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.50, "passed": 2, "total": 5, "timestamp": 1, "detail": []}

    captured: list[str] = []
    second_passed, second_result = run_recall_gate(
        checker=_SecondChecker(),
        log_path=log_path,
        alert_callback=captured.append,
    )
    assert second_passed is False
    assert second_result["score"] == pytest.approx(0.50)
    assert len(captured) == 1
    assert "80%" in captured[0] and "50%" in captured[0]

    # Both runs are persisted; the log retains them in order.
    runs = json.loads(log_path.read_text())
    assert len(runs) == 2
    assert runs[0]["score"] == pytest.approx(0.80)
    assert runs[1]["score"] == pytest.approx(0.50)


@pytest.mark.integration
def test_save_recall_result_then_load_returns_same_score(tmp_path: Path) -> None:
    """A round-trip through the on-disk log preserves the score exactly."""
    log_path = tmp_path / "recall-check.json"
    save_recall_result({"score": 0.4242, "passed": 2, "total": 5}, log_path)
    save_recall_result({"score": 0.7777, "passed": 4, "total": 5}, log_path)
    assert load_previous_score(log_path) == pytest.approx(0.7777)
