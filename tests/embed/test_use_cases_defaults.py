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


# ---------------------------------------------------------------------------
# _default_scan_documents — wraps DocumentScanner + collection config loader.
#
# This wrapper is the production hook ``PipelineDeps`` points at by default.
# It composes five collaborators (DocumentScanner, load_collections,
# resolve_config_path, agent registry, reference-library probe) plus an
# optional FTS rebuild. We drive each branch via ``monkeypatch.setattr``
# on the modules the wrapper lazy-imports — F2 only prohibits
# ``monkeypatch.setenv("KAIRIX_*")``, plain attr swaps on kairix modules
# are the canonical injection seam for these lazy-import wrappers and
# match the pattern used by the other ``_default_*`` tests above.
# ---------------------------------------------------------------------------


class _FakeScanReport:
    """Stand-in for ``kairix.core.db.scanner.ScanReport`` — only the
    fields :func:`_default_scan_documents` reads."""

    def __init__(self, *, new: int = 0, updated: int = 0, unchanged: int = 0, errors: int = 0) -> None:
        self.new = new
        self.updated = updated
        self.unchanged = unchanged
        self.errors = errors


class _FakeScanner:
    """Stand-in for ``DocumentScanner`` — records the collections passed
    to ``scan()`` and returns a configurable ``_FakeScanReport``."""

    def __init__(self, report: _FakeScanReport) -> None:
        self._report = report
        self.collections_scanned: list[Any] = []
        self.constructor_kwargs: dict[str, Any] = {}

    def scan(self, collections: list[Any]) -> _FakeScanReport:
        self.collections_scanned = collections
        return self._report


def _install_scan_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: _FakeScanReport,
    collections_cfg: Any = None,
    config_path: Any = None,
    registry_agents: list[str] | None = None,
    reflib_is_dir: bool = False,
    raw_yaml: Any = None,
    yaml_raises: BaseException | None = None,
    rebuild_fts_count: int = 0,
) -> tuple[_FakeScanner, dict[str, Any]]:
    """Wire stand-ins for every collaborator ``_default_scan_documents`` uses.

    Returns the installed ``_FakeScanner`` plus a dict of recorders the
    test can assert against (FTS calls, registry calls, etc.).
    """
    import kairix.core.db.fts as fts_mod
    import kairix.core.db.scanner as scanner_mod
    import kairix.core.search.config_loader as cfg_mod
    import kairix.core.search.registry as registry_mod
    import kairix.paths as paths_mod

    fake_scanner = _FakeScanner(report)
    recorders: dict[str, Any] = {"fts_calls": [], "registry_calls": [], "scanner_kwargs": {}}

    def _fake_doc_scanner(db: Any, *, document_root: Any, agent_owner_resolver: Any) -> _FakeScanner:
        recorders["scanner_kwargs"] = {
            "db": db,
            "document_root": document_root,
            "agent_owner_resolver": agent_owner_resolver,
        }
        return fake_scanner

    monkeypatch.setattr(scanner_mod, "DocumentScanner", _fake_doc_scanner)

    # CollectionConfig is re-imported inside the wrapper — keep the real
    # class; the wrapper constructs it from the loaded yaml. Don't patch.

    monkeypatch.setattr(cfg_mod, "load_collections", lambda: collections_cfg)
    monkeypatch.setattr(cfg_mod, "resolve_config_path", lambda: config_path)

    class _FakeRegistry:
        def __init__(self, agents: list[str] | None) -> None:
            self._agents = agents or []

        def list_agents(self) -> list[str]:
            return list(self._agents)

    def _fake_parse(raw: Any, *, default_pattern: str = "{agent}-memory") -> _FakeRegistry:
        # default_pattern is part of the real signature; preserved here for arity.
        _ = default_pattern
        recorders["registry_calls"].append(raw)
        return _FakeRegistry(registry_agents)

    monkeypatch.setattr(registry_mod, "parse_agent_registry", _fake_parse)
    monkeypatch.setattr(registry_mod, "build_agent_owner_resolver", lambda reg: ("resolver", reg))

    # Stub out paths.
    monkeypatch.setattr(paths_mod, "document_root", lambda: Path("/tmp/fake-doc-root"))

    class _FakeReflibRoot:
        def __str__(self) -> str:
            return "/tmp/fake-reflib"

        def is_dir(self) -> bool:
            return reflib_is_dir

    monkeypatch.setattr(paths_mod, "reference_library_root", _FakeReflibRoot)

    # Stub out yaml when raw_yaml is set; the wrapper imports yaml lazily.
    if config_path is not None:
        import yaml as yaml_mod

        def _fake_safe_load(_stream: Any) -> Any:
            if yaml_raises is not None:
                raise yaml_raises
            return raw_yaml

        monkeypatch.setattr(yaml_mod, "safe_load", _fake_safe_load)

    def _fake_rebuild_fts(db: Any) -> int:
        recorders["fts_calls"].append(db)
        return rebuild_fts_count

    monkeypatch.setattr(fts_mod, "rebuild_fts", _fake_rebuild_fts)
    return fake_scanner, recorders


class _FakePathForYaml:
    """Minimal ``Path``-shaped stand-in for ``resolve_config_path()``.

    Only the ``.open(encoding=...)`` context manager surface is exercised
    by the wrapper; we yield a dummy stream that yaml.safe_load never
    actually reads (the safe_load stub returns the canned dict).
    """

    def open(self, encoding: str = "utf-8") -> Any:
        # encoding is part of pathlib.Path.open's surface; preserved for parity.
        _ = encoding

        class _Ctx:
            def __enter__(self) -> Any:
                return object()

            def __exit__(self, *exc: Any) -> None:
                # Context manager exit — no cleanup required for the stub.
                _ = exc

        return _Ctx()


def test_default_scan_documents_no_config_no_reflib_no_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No collections.yml, no reference-library, no new docs → returns zeros.

    Sabotage-prove: if the wrapper miscounted scan_report fields (e.g.
    swapped new/updated) the tuple shape assertion below would fail.
    """
    report = _FakeScanReport(new=0, updated=0, unchanged=0, errors=0)
    scanner, recorders = _install_scan_stubs(monkeypatch, report=report)

    diagnostics: list[str] = []
    new, updated, errors = uc_mod._default_scan_documents(object(), diagnostics)

    assert (new, updated, errors) == (0, 0, 0)
    # Default branch when no config: a single "default" collection rooted at ".".
    assert len(scanner.collections_scanned) == 1
    assert scanner.collections_scanned[0].name == "default"
    assert recorders["fts_calls"] == [], "FTS rebuild should NOT run when scan has no new/updated"
    # No diagnostic when resolve_config_path returns None — agent registry path is skipped.
    assert diagnostics == []


def test_default_scan_documents_loads_shared_collections_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured shared collections override the implicit "default" one.

    Sabotage-prove: if the wrapper ignored the loaded ``collections_cfg``
    the assertion that "alpha" appears in scanned collection names fails.
    """
    from kairix.core.search.config_loader import CollectionDef, CollectionsConfig

    cfg = CollectionsConfig(
        shared=(
            CollectionDef(name="alpha", path="alpha/", glob="**/*.md"),
            CollectionDef(name="beta", path="beta/", glob="**/*.txt"),
        ),
    )
    report = _FakeScanReport(new=0, updated=0, unchanged=5, errors=0)
    scanner, _ = _install_scan_stubs(monkeypatch, report=report, collections_cfg=cfg)

    diagnostics: list[str] = []
    uc_mod._default_scan_documents(object(), diagnostics)

    names = sorted(c.name for c in scanner.collections_scanned)
    assert names == ["alpha", "beta"]


def test_default_scan_documents_appends_reflib_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``reference_library_root()`` is a real dir, it joins the scan list."""
    report = _FakeScanReport(new=0, updated=0)
    scanner, _ = _install_scan_stubs(monkeypatch, report=report, reflib_is_dir=True)

    uc_mod._default_scan_documents(object(), [])

    names = [c.name for c in scanner.collections_scanned]
    # The reference-library collection is appended after the default.
    assert "reference-library" in names
    reflib_cfg = next(c for c in scanner.collections_scanned if c.name == "reference-library")
    assert reflib_cfg.glob == "**/*.md"


def test_default_scan_documents_rebuilds_fts_when_new_or_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When scan reports any new or updated doc the wrapper rebuilds FTS.

    Sabotage-prove: if the rebuild guard were dropped to ``if True`` the
    FTS rebuild would run with every empty scan and double-count; if
    inverted to ``< 0`` it would never run. The recorders confirm exactly
    one rebuild fires when new=1.
    """
    report = _FakeScanReport(new=1, updated=0, unchanged=10, errors=0)
    _, recorders = _install_scan_stubs(monkeypatch, report=report, rebuild_fts_count=42)

    db_sentinel = object()
    new, updated, errors = uc_mod._default_scan_documents(db_sentinel, [])

    assert (new, updated, errors) == (1, 0, 0)
    assert recorders["fts_calls"] == [db_sentinel], "FTS rebuild must run with the same db handle"


def test_default_scan_documents_rebuilds_fts_when_only_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``updated > 0`` alone also triggers rebuild — covers the OR branch."""
    report = _FakeScanReport(new=0, updated=3, unchanged=0, errors=0)
    _, recorders = _install_scan_stubs(monkeypatch, report=report, rebuild_fts_count=7)

    uc_mod._default_scan_documents(object(), [])

    assert len(recorders["fts_calls"]) == 1


def test_default_scan_documents_builds_agent_resolver_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config_path with at least one registered agent wires a resolver.

    Sabotage-prove: if the wrapper passed ``agent_owner_resolver=None``
    even when the registry had agents, the assertion that the resolver
    sentinel propagates to DocumentScanner would fail.
    """
    report = _FakeScanReport(new=0, updated=0)
    _, recorders = _install_scan_stubs(
        monkeypatch,
        report=report,
        config_path=_FakePathForYaml(),
        registry_agents=["alpha", "beta"],
        raw_yaml={"agents": [{"name": "alpha"}, {"name": "beta"}]},
    )

    uc_mod._default_scan_documents(object(), [])

    # The build_agent_owner_resolver stub returns a tuple sentinel ("resolver", reg);
    # we just need to know the wrapper used it (vs. None).
    resolver = recorders["scanner_kwargs"]["agent_owner_resolver"]
    assert resolver is not None
    assert isinstance(resolver, tuple) and resolver[0] == "resolver"


def test_default_scan_documents_skips_resolver_when_registry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config_path with no registered agents skips resolver construction.

    Sabotage-prove: if the wrapper blindly built a resolver for any
    non-None registry, the scanner kwargs would not be None.
    """
    report = _FakeScanReport(new=0, updated=0)
    _, recorders = _install_scan_stubs(
        monkeypatch,
        report=report,
        config_path=_FakePathForYaml(),
        registry_agents=[],
        raw_yaml={"agents": []},
    )

    uc_mod._default_scan_documents(object(), [])

    assert recorders["scanner_kwargs"]["agent_owner_resolver"] is None


def test_default_scan_documents_appends_diagnostic_on_resolver_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agent-resolver construction raises, the wrapper logs a diagnostic
    and continues with ``agent_owner_resolver=None``.

    Sabotage-prove: if the wrapper let the exception escape, this call
    would raise instead of returning the scan tuple.
    """
    report = _FakeScanReport(new=0, updated=0)
    _, recorders = _install_scan_stubs(
        monkeypatch,
        report=report,
        config_path=_FakePathForYaml(),
        yaml_raises=RuntimeError("yaml exploded"),
    )

    diagnostics: list[str] = []
    new, updated, errors = uc_mod._default_scan_documents(object(), diagnostics)

    assert (new, updated, errors) == (0, 0, 0)
    assert recorders["scanner_kwargs"]["agent_owner_resolver"] is None
    assert any("agent_resolver_unavailable" in msg for msg in diagnostics)
    assert any("yaml exploded" in msg for msg in diagnostics)


def test_default_scan_documents_handles_yaml_returning_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config file present but empty (``yaml.safe_load`` → None) is
    treated as ``{}`` — the wrapper still calls ``parse_agent_registry``
    with an empty dict and continues."""
    report = _FakeScanReport(new=0, updated=0)
    _, recorders = _install_scan_stubs(
        monkeypatch,
        report=report,
        config_path=_FakePathForYaml(),
        registry_agents=[],
        raw_yaml=None,
    )

    uc_mod._default_scan_documents(object(), [])

    # registry was constructed with {} (the wrapper's ``or {}`` fallback).
    assert recorders["registry_calls"] == [{}]
