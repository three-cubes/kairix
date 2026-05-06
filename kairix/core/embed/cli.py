"""
CLI entrypoint for kairix embed.

Usage:
  kairix embed [--force] [--limit N] [--batch-size N] [--skip-recall-check]
  kairix embed recall-check
  kairix embed status
"""

import argparse
import fcntl
import logging
import os
import sys
import time
from pathlib import Path
from typing import IO

from kairix.core.db import get_db_path, open_db

from .embed import DEFAULT_BATCH_SIZE, run_embed
from .recall_check import run_recall_gate
from .schema import save_run_log, validate_schema

LOG_FILE = Path(
    os.environ.get(
        "KAIRIX_EMBED_LOG",
        str(Path.home() / ".cache" / "kairix" / "logs" / "embed.log"),
    )
)


def _default_lockfile() -> Path:
    """Lockfile in user cache dir — avoids world-writable /tmp on multi-user systems."""
    from kairix.core.db import get_db_path

    return get_db_path().parent / "embed.lock"


LOCKFILE = _default_lockfile()
LOCK_WAIT_SECS = 60


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.FileHandler(LOG_FILE))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def acquire_lock() -> IO[str]:
    """
    Acquire exclusive lock using the same lockfile as kairix-maintenance.sh.
    Waits up to LOCK_WAIT_SECS. If the lock holder is dead, takes over.
    Exits with code 3 if timeout and holder is still alive.
    """
    lock_fh = open(LOCKFILE, "w")
    deadline = time.time() + LOCK_WAIT_SECS
    while time.time() < deadline:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fh.write(str(os.getpid()))
            lock_fh.flush()
            return lock_fh
        except BlockingIOError:
            logging.info("Waiting for embed lock...")
            time.sleep(5)

    # Timeout — check if the lock holder is still alive
    lock_fh.close()
    try:
        holder_pid = int(LOCKFILE.read_text().strip())
        # NOSONAR(python:S4828): signal 0 is documented as a process-existence
        # probe — does not deliver a real signal. Used here to detect a stale
        # lockfile from a dead PID before taking it over.
        os.kill(holder_pid, 0)
        # Process is alive — genuine contention
        logging.error(
            "Could not acquire lock after %ds — PID %d is still running",
            LOCK_WAIT_SECS,
            holder_pid,
        )
        sys.exit(3)
    except (ProcessLookupError, ValueError, OSError):
        # Holder is dead or PID unreadable — stale lock
        logging.warning("Stale embed lock (holder no longer running) — taking over")
        LOCKFILE.unlink(missing_ok=True)
        lock_fh = open(LOCKFILE, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fh.write(str(os.getpid()))
            lock_fh.flush()
            return lock_fh
        except BlockingIOError:
            logging.error("Failed to acquire lock even after stale lock cleanup")
            sys.exit(3)


def release_lock(lock_fh: IO[str]) -> None:
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        LOCKFILE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def cmd_embed(args: argparse.Namespace) -> int:
    """Run the embedding pipeline."""
    logging.info(f"kairix embed starting — force={args.force} limit={args.limit} batch_size={args.batch_size}")

    lock_fh = acquire_lock()
    db_path = get_db_path()
    start = time.time()
    result = None

    try:
        db = open_db(Path(db_path))
        try:
            from kairix.core.db.schema import create_schema

            create_schema(db)
            validate_schema(db)

            # Scan document store for new/changed documents before embedding.
            # This ensures first-run embed works without a separate scan step.
            from kairix.core.db.scanner import CollectionConfig, DocumentScanner
            from kairix.core.search.config_loader import _resolve_config_path, load_collections
            from kairix.core.search.registry import build_agent_owner_resolver, parse_agent_registry
            from kairix.paths import document_root

            droot = document_root()

            # Build agent_owner resolver from the agents: section of kairix.config.yaml
            # so each scanned document is tagged with its owning agent (#114).
            # Documents not under any agent's write_path get agent_owner=NULL.
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
            except Exception as _exc:
                logging.warning("embed: agent_owner resolver unavailable — %s", _exc)

            scanner = DocumentScanner(db, document_root=droot, agent_owner_resolver=agent_resolver)

            # Load collections from config; fall back to scanning entire document root
            collections_cfg = load_collections()
            if collections_cfg and collections_cfg.shared:
                scan_collections = [
                    CollectionConfig(name=c.name, path=c.path, glob=c.glob) for c in collections_cfg.shared
                ]
                logging.info("Using %d configured collections", len(scan_collections))
            else:
                scan_collections = [CollectionConfig(name="default", path=".")]

            # Auto-append reference library if present (ships inside Docker container)
            from kairix.paths import reference_library_root

            reflib_root = reference_library_root()
            if reflib_root.is_dir():
                scan_collections.append(
                    CollectionConfig(name="reference-library", path=str(reflib_root), glob="**/*.md")
                )

            scan_report = scanner.scan(scan_collections)
            if scan_report.new > 0 or scan_report.updated > 0:
                logging.info(
                    "Scanned documents: %d new, %d updated, %d unchanged",
                    scan_report.new,
                    scan_report.updated,
                    scan_report.unchanged,
                )
                # Rebuild FTS index after scanning new/changed documents
                from kairix.core.db.fts import rebuild_fts

                fts_count = rebuild_fts(db)
                logging.info("FTS index rebuilt: %d documents", fts_count)

            result = run_embed(
                db=db,
                force=args.force,
                batch_size=args.batch_size,
                limit=args.limit,
            )

            result["command"] = "embed"
            result["db_path"] = str(db_path)
            result["timestamp"] = int(start)
            save_run_log(result)

            logging.info(
                f"Done — embedded={result['embedded']} failed={result['failed']} "
                f"duration={result['duration_s']}s cost=${result['estimated_cost_usd']:.4f}"
            )

            if result["failed"] > 0:
                logging.warning(f"{result['failed']} chunks failed. Re-run without --force to retry failed chunks.")
        finally:
            db.close()

    except Exception:
        logging.exception("Embed failed")
        return 2
    finally:
        release_lock(lock_fh)

    if args.skip_recall_check:
        logging.info("Skipping recall check (--skip-recall-check)")
        return 0 if result["failed"] == 0 else 1

    # Recall gate
    logging.info("Running post-embed recall check...")
    gate_passed, recall_result = run_recall_gate()
    logging.info(f"Recall: {recall_result['passed']}/{recall_result['total']} ({recall_result['score']:.0%})")

    if not gate_passed:
        logging.error("Recall gate FAILED — search quality degraded. Check logs.")
        return 1

    # Post-embed summarise — generate L0 summaries for stale/new docs
    if not args.skip_summarise:
        _run_post_embed_summarise()

    return 0 if result["failed"] == 0 else 1


def _run_post_embed_summarise() -> None:
    """Generate L0 summaries for documents that don't have them yet.

    Non-critical: failures are logged but don't block the embed return code.
    """
    try:
        from kairix.paths import document_root

        droot = document_root()
        all_docs = [str(p) for p in droot.rglob("*.md") if p.is_file()]
        if not all_docs:
            return

        # Open summaries DB and find stale/missing docs
        import sqlite3

        from kairix.knowledge.summaries.staleness import (
            get_stale_paths,
            init_summaries_db,
        )
        from kairix.paths import summaries_db_path

        db = sqlite3.connect(str(summaries_db_path()))
        init_summaries_db(db)

        stale = get_stale_paths(all_docs, db)
        if not stale:
            logging.info("Summarise: all %d docs have current summaries", len(all_docs))
            db.close()
            return

        # Cap at 100 docs per embed run to limit API cost
        batch = stale[:100]
        logging.info(
            "Summarise: generating L0 for %d of %d stale docs (capped at 100)",
            len(batch),
            len(stale),
        )

        from kairix.knowledge.summaries.generate import generate_summaries
        from kairix.knowledge.summaries.staleness import write_summary

        results = generate_summaries(paths=batch, api_key="", endpoint="", deployment="gpt-4o-mini")
        for r in results:
            write_summary(r, db)

        logging.info("Summarise: %d L0 summaries generated", len(results))
        db.close()

    except Exception:
        logging.warning("Post-embed summarise failed (non-critical)", exc_info=True)


def cmd_recall(_args: argparse.Namespace) -> int:
    """Run the recall check standalone."""
    passed, result = run_recall_gate()
    print(f"Recall: {result['passed']}/{result['total']} ({result['score']:.0%})")
    for d in result["detail"]:
        status = "✓" if d["hit"] else "✗"
        print(f"  {status} [{d['id']}] {d['query'][:60]}")
    return 0 if passed else 1


def cmd_status(_args: argparse.Namespace) -> int:
    """Show current embedding status."""
    from .schema import get_pending_chunks

    db_path = get_db_path()
    db = open_db(Path(db_path))
    try:
        pending = get_pending_chunks(db)
        total_vecs = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
        total_docs = db.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]

        print(f"Kairix index: {db_path}")
        print(f"Documents: {total_docs}")
        print(f"Vectors:   {total_vecs}")
        print(f"Pending:   {len(pending)} documents need embedding")

        # Last run
        log_path = Path.home() / ".cache" / "kairix" / "azure-embed-runs.json"
        if log_path.exists():
            import json

            try:
                runs = json.loads(log_path.read_text())
                if runs:
                    last = runs[-1]
                    import datetime

                    ts = datetime.datetime.fromtimestamp(last.get("timestamp", 0))
                    print(
                        f"Last run:  {ts.strftime('%Y-%m-%d %H:%M')} — "
                        f"embedded={last.get('embedded')} cost=${last.get('estimated_cost_usd'):.4f}"
                    )
            except Exception:  # nosec S110 display failure is non-critical, logging not yet initialised
                pass  # non-critical: status display failed
    finally:
        db.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kairix embed",
        description="Embed documents into the kairix vector index",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # embed (default)
    embed_p = sub.add_parser("embed", help="Run embedding pipeline (default)")
    embed_p.add_argument(
        "--force",
        action="store_true",
        help="Re-embed all chunks (clears existing vectors)",
    )
    embed_p.add_argument("--limit", type=int, default=None, help="Cap total chunks (for validation)")
    embed_p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Chunks per Azure API call",
    )
    embed_p.add_argument("--skip-recall-check", action="store_true", help="Skip post-embed quality gate")
    embed_p.add_argument(
        "--skip-summarise",
        action="store_true",
        help="Skip post-embed L0 summary generation",
    )

    # recall-check
    sub.add_parser("recall-check", help="Run recall quality check standalone")

    # status
    sub.add_parser("status", help="Show embedding status")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command is None or args.command == "embed":
        if not hasattr(args, "force"):
            # Default subcommand
            args.force = False
            args.limit = None
            args.batch_size = DEFAULT_BATCH_SIZE
            args.skip_recall_check = False
            args.skip_summarise = False
        sys.exit(cmd_embed(args))
    elif args.command == "recall-check":
        sys.exit(cmd_recall(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))


if __name__ == "__main__":
    main()
