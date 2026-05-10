"""Lazy production-default wrappers for ``run_incremental_embed_pipeline``.

Each function is a thin pass-through to the real implementation. They
live in a dedicated module so:

  - The use case (``use_cases.py``) stays focused on pure orchestration
    logic that unit-tests can drive 100% via DI.
  - Production wiring lives in one place and is exercised by the
    integration tests that run the full pipeline against a real DB.
  - The lazy ``from kairix.core.db import ...`` calls in each wrapper
    keep import-time cost off any unit test that injects stand-ins.

We deliberately do NOT add unit tests here — these wrappers are tested
in aggregate by ``tests/integration/test_recall_gate_pipeline.py`` and
the production embed flow on the VM. The file is on the F7
per-file-coverage baseline as production-wiring code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def default_db_path() -> str:
    from kairix.core.db import get_db_path

    return str(get_db_path())


def default_open_db(path: Path) -> Any:
    from kairix.core.db import open_db

    return open_db(path)


def default_create_schema(db: Any) -> None:
    from kairix.core.db.schema import create_schema

    create_schema(db)


def default_validate_schema(db: Any) -> None:
    from kairix.core.db.schema import validate_schema

    validate_schema(db)


def default_acquire_lock() -> Any:
    from kairix.core.embed.cli import acquire_lock

    return acquire_lock()


def default_release_lock(lock_fh: Any) -> None:
    from kairix.core.embed.cli import release_lock

    release_lock(lock_fh)


def default_save_run_log(entry: dict[str, Any]) -> None:
    from kairix.core.embed.schema import save_run_log

    save_run_log(entry)


def default_run_embed(**kwargs: Any) -> dict[str, Any]:
    from kairix.core.embed.embed import run_embed

    return run_embed(**kwargs)


def default_run_recall_gate(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
    from kairix.core.embed.recall_check import run_recall_gate

    return run_recall_gate(**kwargs)


def default_scan_documents(db: Any, diagnostics: list[str]) -> tuple[int, int, int]:
    """Scan the document root for new/changed files and rebuild FTS.

    Extracted from ``use_cases.py`` so the use case stays focused on
    pure orchestration. This function is integration-tested via the
    full embed flow against a real DB; unit testing it would require
    a fake document corpus and is not load-bearing for the worker fix.
    """
    import logging

    from kairix.core.db.scanner import CollectionConfig, DocumentScanner
    from kairix.core.search.config_loader import _resolve_config_path, load_collections
    from kairix.core.search.registry import build_agent_owner_resolver, parse_agent_registry
    from kairix.paths import document_root, reference_library_root

    logger = logging.getLogger(__name__)
    droot = document_root()

    agent_resolver = None
    try:
        config_path = _resolve_config_path()
        if config_path is not None:
            import yaml as _yaml

            with config_path.open(encoding="utf-8") as _f:
                _raw_yaml = _yaml.safe_load(_f) or {}
            _registry = parse_agent_registry(_raw_yaml)
            if _registry.list_agents():
                agent_resolver = build_agent_owner_resolver(_registry)
    # Agent-resolver construction is best-effort; we'd rather scan with
    # agent_owner=NULL than skip the scan entirely.
    except Exception as exc:
        diagnostics.append(f"agent_resolver_unavailable: {exc}")

    scanner = DocumentScanner(db, document_root=droot, agent_owner_resolver=agent_resolver)

    collections_cfg = load_collections()
    if collections_cfg and collections_cfg.shared:
        scan_collections = [CollectionConfig(name=c.name, path=c.path, glob=c.glob) for c in collections_cfg.shared]
        logger.info("Using %d configured collections", len(scan_collections))
    else:
        scan_collections = [CollectionConfig(name="default", path=".")]

    reflib_root = reference_library_root()
    if reflib_root.is_dir():
        scan_collections.append(CollectionConfig(name="reference-library", path=str(reflib_root), glob="**/*.md"))

    scan_report = scanner.scan(scan_collections)
    if scan_report.new > 0 or scan_report.updated > 0:
        logger.info(
            "Scanned documents: %d new, %d updated, %d unchanged",
            scan_report.new,
            scan_report.updated,
            scan_report.unchanged,
        )
        from kairix.core.db.fts import rebuild_fts

        fts_count = rebuild_fts(db)
        logger.info("FTS index rebuilt: %d documents", fts_count)

    return scan_report.new, scan_report.updated, scan_report.errors
