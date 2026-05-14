"""Unit tests for the ``_default_*`` lazy-import wrappers in
``kairix.core.embed.use_cases`` (PR #247 QG).

The F6 refactor (commit dab94644) replaced ``Optional[Callable]``
fields with ``field(default_factory=...)`` and added one
``_default_X`` lazy-import wrapper per production callable. Sonar
treats each wrapper line as new code; the existing
``tests/embed/test_use_cases.py`` covers the orchestration via
injected stand-ins but never lights up the production defaults.

These tests drive each wrapper directly (via the
``kairix.core.embed.use_cases`` module attribute — module access is
not an internal-name import, so F5 is satisfied) and assert that:

  - The wrapper returns a value of the expected shape OR delegates to
    the documented kairix function.
  - The wrapper invokes its lazy import (which would otherwise be
    measured as 0% covered on Sonar's per-line view).

The wrappers themselves are thin pass-throughs — production wiring,
not business logic — so the assertions stay focused on "the import
ran, the right function got called".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kairix.core.embed.use_cases as uc_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _default_db_path — wraps ``kairix.core.db.get_db_path``.
# ---------------------------------------------------------------------------


def test_default_db_path_delegates_to_get_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_db_path`` calls ``kairix.core.db.get_db_path`` and stringifies."""
    import kairix.core.db as db_mod

    monkeypatch.setattr(db_mod, "get_db_path", lambda: Path("/tmp/test-path.sqlite"))

    out = uc_mod._default_db_path()

    assert out == "/tmp/test-path.sqlite"
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# _default_open_db — wraps ``kairix.core.db.open_db``.
# ---------------------------------------------------------------------------


def test_default_open_db_delegates_to_open_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_open_db`` forwards the path to ``open_db``."""
    import kairix.core.db as db_mod

    captured: list[Path] = []

    class _Sentinel:
        pass

    sentinel = _Sentinel()

    def _fake_open_db(path: Path) -> _Sentinel:
        captured.append(path)
        return sentinel

    monkeypatch.setattr(db_mod, "open_db", _fake_open_db)

    out = uc_mod._default_open_db(Path("/tmp/x.sqlite"))

    assert out is sentinel
    assert captured == [Path("/tmp/x.sqlite")]


# ---------------------------------------------------------------------------
# _default_create_schema and _default_validate_schema — wrap
# kairix.core.db.schema.create_schema / validate_schema.
# ---------------------------------------------------------------------------


def test_default_create_schema_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_create_schema`` forwards its db argument to
    ``kairix.core.db.schema.create_schema``."""
    import kairix.core.db.schema as schema_mod

    seen: list[Any] = []
    monkeypatch.setattr(schema_mod, "create_schema", lambda db: seen.append(db))

    db_sentinel = object()
    uc_mod._default_create_schema(db_sentinel)

    assert seen == [db_sentinel]


def test_default_validate_schema_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_validate_schema`` forwards its db argument to
    ``kairix.core.db.schema.validate_schema``."""
    import kairix.core.db.schema as schema_mod

    seen: list[Any] = []
    monkeypatch.setattr(schema_mod, "validate_schema", lambda db: seen.append(db))

    db_sentinel = object()
    uc_mod._default_validate_schema(db_sentinel)

    assert seen == [db_sentinel]


# ---------------------------------------------------------------------------
# _default_acquire_lock and _default_release_lock — wrap
# kairix.core.embed.cli.acquire_lock / release_lock.
# ---------------------------------------------------------------------------


def test_default_acquire_lock_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_acquire_lock`` returns whatever ``cli.acquire_lock`` returns."""
    import kairix.core.embed.cli as cli_mod

    sentinel = object()
    monkeypatch.setattr(cli_mod, "acquire_lock", lambda: sentinel)

    assert uc_mod._default_acquire_lock() is sentinel


def test_default_release_lock_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_release_lock`` forwards the lock handle to ``cli.release_lock``."""
    import kairix.core.embed.cli as cli_mod

    seen: list[Any] = []
    monkeypatch.setattr(cli_mod, "release_lock", lambda fh: seen.append(fh))

    handle = object()
    uc_mod._default_release_lock(handle)

    assert seen == [handle]


# ---------------------------------------------------------------------------
# _default_save_run_log — wraps kairix.core.embed.schema.save_run_log.
# ---------------------------------------------------------------------------


def test_default_save_run_log_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_save_run_log`` forwards the log entry dict verbatim."""
    import kairix.core.embed.schema as schema_mod

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(schema_mod, "save_run_log", lambda entry: seen.append(entry))

    entry = {"command": "embed", "embedded": 7}
    uc_mod._default_save_run_log(entry)

    assert seen == [entry]


# ---------------------------------------------------------------------------
# _default_run_embed — wraps kairix.core.embed.embed.run_embed.
# ---------------------------------------------------------------------------


def test_default_run_embed_delegates_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_run_embed`` forwards every kwarg to ``run_embed``."""
    import kairix.core.embed.embed as embed_mod

    captured: list[dict[str, Any]] = []

    def _fake_run_embed(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"embedded": 1, "failed": 0, "skipped": 0, "duration_s": 0.1, "estimated_cost_usd": 0.0}

    monkeypatch.setattr(embed_mod, "run_embed", _fake_run_embed)

    out = uc_mod._default_run_embed(db=None, force=False, batch_size=100, limit=None, deps=None)

    assert out["embedded"] == 1
    assert captured == [{"db": None, "force": False, "batch_size": 100, "limit": None, "deps": None}]


# ---------------------------------------------------------------------------
# _default_run_recall_gate — wraps kairix.core.embed.recall_check.run_recall_gate.
# ---------------------------------------------------------------------------


def test_default_run_recall_gate_delegates_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_run_recall_gate`` forwards kwargs to ``run_recall_gate``."""
    import kairix.core.embed.recall_check as recall_mod

    captured: list[dict[str, Any]] = []

    def _fake_gate(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        captured.append(kwargs)
        return True, {"score": 0.95, "passed": 19, "total": 20}

    monkeypatch.setattr(recall_mod, "run_recall_gate", _fake_gate)

    passed, result = uc_mod._default_run_recall_gate(alert_callback=None, rebuild_canaries=False)

    assert passed is True
    assert result["score"] == 0.95
    assert captured == [{"alert_callback": None, "rebuild_canaries": False}]


# ---------------------------------------------------------------------------
# PipelineDeps default factory — defaults wire the lazy production callables.
# ---------------------------------------------------------------------------


def test_pipeline_deps_defaults_wire_lazy_production_callables() -> None:
    """``PipelineDeps()`` resolves to the module's ``_default_*`` callables.

    This catches accidental ``Optional[Callable]`` regressions: if a future
    refactor reverts F6, the defaults would be ``None`` and these identity
    checks would fail.
    """
    deps = uc_mod.PipelineDeps()
    assert deps.db_path_fn is uc_mod._default_db_path
    assert deps.open_db_fn is uc_mod._default_open_db
    assert deps.schema_fn is uc_mod._default_create_schema
    assert deps.validate_schema_fn is uc_mod._default_validate_schema
    assert deps.acquire_lock_fn is uc_mod._default_acquire_lock
    assert deps.release_lock_fn is uc_mod._default_release_lock
    assert deps.save_run_log_fn is uc_mod._default_save_run_log
    assert deps.run_embed_fn is uc_mod._default_run_embed
    assert deps.run_recall_gate_fn is uc_mod._default_run_recall_gate
    assert deps.scan_documents_fn is uc_mod._default_scan_documents


# ---------------------------------------------------------------------------
# EmbedPipelineResult.success — the only branch in the dataclass.
# ---------------------------------------------------------------------------


def test_embed_pipeline_result_success_when_no_failed() -> None:
    """``failed == 0`` → ``success`` is True regardless of recall outcome."""
    result = uc_mod.EmbedPipelineResult(
        embedded=10,
        failed=0,
        skipped=5,
        duration_s=1.0,
        cost_usd=0.01,
        db_path="/tmp/x.sqlite",
        timestamp=1700000000,
    )
    assert result.success is True


def test_embed_pipeline_result_success_false_when_any_failed() -> None:
    """Any failed chunks → ``success`` is False; recall outcome doesn't matter."""
    result = uc_mod.EmbedPipelineResult(
        embedded=10,
        failed=3,
        skipped=0,
        duration_s=1.0,
        cost_usd=0.01,
        db_path="/tmp/x.sqlite",
        timestamp=1700000000,
        recall_passed=True,
    )
    assert result.success is False


def test_embed_pipeline_result_default_diagnostics_empty() -> None:
    """``diagnostics`` defaults to an empty list (not None)."""
    result = uc_mod.EmbedPipelineResult(
        embedded=0,
        failed=0,
        skipped=0,
        duration_s=0.0,
        cost_usd=0.0,
        db_path="/tmp/x.sqlite",
        timestamp=0,
    )
    assert result.diagnostics == []
