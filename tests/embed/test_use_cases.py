"""Unit tests for the incremental embed pipeline use case.

The use case (``run_incremental_embed_pipeline``) is the orchestration
seam the worker calls in production. We drive it through its DI
parameter (``pipeline_deps``) with light-weight stand-ins for every
heavy collaborator (DB open, schema, scan, embed, recall gate). Tests
verify:

  - The pipeline composes the helper outcomes into the
    ``EmbedPipelineResult`` dataclass shape.
  - A recall-gate failure is surfaced as ``recall_passed=False`` with
    the alert text — NOT as a raised exception.
  - A recall-gate that raises is captured as a ``diagnostics`` entry
    rather than propagating out of the use case.
  - ``skip_recall_check=True`` short-circuits the gate.
  - The lock is released on every exit path (success and failure).
  - Embed-step exceptions propagate (DB unreachable is unrecoverable).
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.embed.use_cases import (
    EmbedPipelineResult,
    PipelineDeps,
    run_incremental_embed_pipeline,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stand-in helpers — produce predictable outcomes for the pipeline to
# stitch together. None of these require sqlite, the filesystem, or Azure.
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimal DB stand-in that records close() and raises nothing."""

    closed: bool = False

    def close(self) -> None:
        self.closed = True


class _LockTracker:
    """Records lock acquisition/release ordering for the contract tests."""

    def __init__(self) -> None:
        self.acquired: int = 0
        self.released: int = 0

    def acquire(self) -> object:
        self.acquired += 1
        return object()

    def release(self, _handle: object) -> None:
        self.released += 1


def _make_deps(
    *,
    embed_result: dict[str, Any] | None = None,
    embed_raises: BaseException | None = None,
    recall_passed: bool = True,
    recall_score: float = 0.85,
    recall_alert: str | None = None,
    recall_raises: BaseException | None = None,
    scan_counts: tuple[int, int, int] = (0, 0, 0),
    lock_tracker: _LockTracker | None = None,
    log_capture: list[dict[str, Any]] | None = None,
) -> PipelineDeps:
    """Build a ``PipelineDeps`` populated with deterministic stand-ins."""
    db = _FakeDb()
    lock = lock_tracker or _LockTracker()
    captured_log = log_capture if log_capture is not None else []

    def _embed(**_: Any) -> dict[str, Any]:
        if embed_raises is not None:
            raise embed_raises
        return dict(
            embed_result or {"embedded": 3, "failed": 0, "skipped": 1, "duration_s": 1.5, "estimated_cost_usd": 0.001}
        )

    def _recall(*, alert_callback: Any = None, rebuild_canaries: bool = False) -> tuple[bool, dict[str, Any]]:
        if recall_raises is not None:
            raise recall_raises
        if recall_alert is not None and alert_callback is not None:
            alert_callback(recall_alert)
        return recall_passed, {"score": recall_score, "passed": round(recall_score * 5), "total": 5}

    return PipelineDeps(
        db_path_fn=lambda: "/tmp/test-kairix.sqlite",
        open_db_fn=lambda _path: db,
        schema_fn=lambda _db: None,
        validate_schema_fn=lambda _db: None,
        acquire_lock_fn=lock.acquire,
        release_lock_fn=lock.release,
        save_run_log_fn=lambda entry: captured_log.append(dict(entry)),
        run_embed_fn=_embed,
        run_recall_gate_fn=_recall,
        scan_documents_fn=lambda _db, _diag: scan_counts,
    )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_pipeline_returns_structured_result_on_happy_path() -> None:
    deps = _make_deps()

    result = run_incremental_embed_pipeline(pipeline_deps=deps)

    assert isinstance(result, EmbedPipelineResult)
    assert result.embedded == 3
    assert result.failed == 0
    assert result.skipped == 1
    assert result.duration_s == pytest.approx(1.5)
    assert result.cost_usd == pytest.approx(0.001)
    assert result.recall_passed is True
    assert result.recall_score == pytest.approx(0.85)
    assert result.recall_alert is None
    assert result.success is True


def test_pipeline_propagates_scan_counters_into_result() -> None:
    deps = _make_deps(scan_counts=(7, 3, 1))

    result = run_incremental_embed_pipeline(pipeline_deps=deps)

    assert result.scan_new == 7
    assert result.scan_updated == 3
    assert result.scan_errors == 1


def test_pipeline_writes_run_log_with_embed_metadata() -> None:
    log: list[dict[str, Any]] = []
    deps = _make_deps(log_capture=log)

    run_incremental_embed_pipeline(pipeline_deps=deps)

    assert len(log) == 1
    entry = log[0]
    assert entry["command"] == "embed"
    assert entry["db_path"] == "/tmp/test-kairix.sqlite"
    assert isinstance(entry["timestamp"], int)


# ---------------------------------------------------------------------------
# Recall-gate alert handling
# ---------------------------------------------------------------------------


def test_recall_gate_failure_surfaces_as_alert_not_exception() -> None:
    """The whole point of the v2026.5.10 fix: gate failure → structured alert."""
    deps = _make_deps(
        recall_passed=False,
        recall_score=0.20,
        recall_alert="Recall degraded: 20% (was 80%, delta -60%)",
    )

    result = run_incremental_embed_pipeline(pipeline_deps=deps)

    assert result.recall_passed is False
    assert result.recall_score == pytest.approx(0.20)
    assert result.recall_alert is not None
    assert "Recall degraded" in result.recall_alert
    # Embed itself succeeded — gate failure does NOT count as embed failure.
    assert result.success is True


def test_recall_gate_exception_is_captured_as_diagnostic() -> None:
    """A raising recall gate must NOT abort the pipeline — embed success stays intact."""
    deps = _make_deps(recall_raises=RuntimeError("recall lookup imploded"))

    result = run_incremental_embed_pipeline(pipeline_deps=deps)

    assert result.recall_score is None
    assert result.recall_passed is None
    assert any("recall_gate_error" in d for d in result.diagnostics)
    assert result.success is True


def test_skip_recall_check_short_circuits_the_gate() -> None:
    """``skip_recall_check=True`` returns immediately without invoking recall."""
    recall_invoked = {"hit": False}

    def _explode(**_kwargs: Any) -> tuple[bool, dict[str, Any]]:
        recall_invoked["hit"] = True
        raise AssertionError("recall gate must not be invoked when skip_recall_check=True")

    deps = _make_deps()
    deps_with_explode = PipelineDeps(
        db_path_fn=deps.db_path_fn,
        open_db_fn=deps.open_db_fn,
        schema_fn=deps.schema_fn,
        validate_schema_fn=deps.validate_schema_fn,
        acquire_lock_fn=deps.acquire_lock_fn,
        release_lock_fn=deps.release_lock_fn,
        save_run_log_fn=deps.save_run_log_fn,
        run_embed_fn=deps.run_embed_fn,
        run_recall_gate_fn=_explode,
        scan_documents_fn=deps.scan_documents_fn,
    )

    result = run_incremental_embed_pipeline(pipeline_deps=deps_with_explode, skip_recall_check=True)

    assert recall_invoked["hit"] is False
    assert result.recall_score is None
    assert result.recall_passed is None


# ---------------------------------------------------------------------------
# Lock contract
# ---------------------------------------------------------------------------


def test_lock_is_released_on_happy_path() -> None:
    lock = _LockTracker()
    deps = _make_deps(lock_tracker=lock)

    run_incremental_embed_pipeline(pipeline_deps=deps)

    assert lock.acquired == 1
    assert lock.released == 1


def test_lock_is_released_when_embed_raises() -> None:
    """Unrecoverable embed exceptions still release the lock."""
    lock = _LockTracker()
    deps = _make_deps(lock_tracker=lock, embed_raises=RuntimeError("DB unreachable"))

    with pytest.raises(RuntimeError, match="DB unreachable"):
        run_incremental_embed_pipeline(pipeline_deps=deps)

    assert lock.acquired == 1
    assert lock.released == 1, "lock must release even when embed raises"


# ---------------------------------------------------------------------------
# Failed-chunk semantics
# ---------------------------------------------------------------------------


def test_failed_chunks_make_success_false_but_not_an_exception() -> None:
    deps = _make_deps(
        embed_result={
            "embedded": 5,
            "failed": 3,
            "skipped": 0,
            "duration_s": 2.0,
            "estimated_cost_usd": 0.002,
        }
    )

    result = run_incremental_embed_pipeline(pipeline_deps=deps)

    assert result.failed == 3
    assert result.success is False
    # Recall gate still ran on the successful chunks.
    assert result.recall_passed is True


# ---------------------------------------------------------------------------
# Default-deps construction
# ---------------------------------------------------------------------------


def test_pipeline_deps_default_constructs_with_no_arguments() -> None:
    """``PipelineDeps()`` succeeds — every field has a ``default_factory``
    that wires the real production callable (F6: no ``Optional[Callable]``).
    """
    deps = PipelineDeps()
    # Every callable slot is wired to a production default callable;
    # tests inject stand-ins by passing the kwarg explicitly.
    assert callable(deps.db_path_fn)
    assert callable(deps.run_embed_fn)
    assert callable(deps.run_recall_gate_fn)
