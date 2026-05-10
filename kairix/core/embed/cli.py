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

from .embed import DEFAULT_BATCH_SIZE
from .recall_check import run_recall_gate

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

    Waits up to ``LOCK_WAIT_SECS`` retrying ``LOCK_EX | LOCK_NB``. The kernel
    releases ``flock`` automatically when the holding process exits (clean or
    crash), so a "stale lockfile" is self-healing — the next worker simply
    succeeds at LOCK_NB once the holder is gone. No PID inspection needed.

    Exits with code 3 if the wait window expires while the holder is still
    actively running.
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

    # Wait window exhausted and we still couldn't acquire — the holder is
    # genuinely alive and working. Exit cleanly.
    lock_fh.close()
    logging.error(
        "Could not acquire lock after %ds — another embed is still running",
        LOCK_WAIT_SECS,
    )
    sys.exit(3)


def release_lock(lock_fh: IO[str]) -> None:
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        LOCKFILE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def cmd_embed(args: argparse.Namespace) -> int:
    """Run the embedding pipeline.

    Thin shim over ``run_incremental_embed_pipeline``. The use case
    encapsulates schema/scan/embed/gate; this function only maps its
    structured result to a process exit code so other CLI semantics
    (e.g. logging behaviour) stay testable in isolation.

    Exit codes:
      0  — embed succeeded and recall gate passed (or was skipped)
      1  — embed had failed chunks OR recall gate fired an alert
      2  — pipeline raised (DB unreachable, schema migration, etc.)
    """
    from kairix.core.embed.use_cases import run_incremental_embed_pipeline

    try:
        result = run_incremental_embed_pipeline(
            force=args.force,
            batch_size=args.batch_size,
            limit=args.limit,
            skip_recall_check=args.skip_recall_check,
            rebuild_canaries=getattr(args, "rebuild_canaries", False),
        )
    except Exception:
        logging.exception("Embed failed")
        return 2

    logging.info(
        f"Done — embedded={result.embedded} failed={result.failed} "
        f"duration={result.duration_s}s cost=${result.cost_usd:.4f}"
    )
    if result.failed > 0:
        logging.warning(f"{result.failed} chunks failed. Re-run without --force to retry failed chunks.")

    if args.skip_recall_check:
        logging.info("Skipping recall check (--skip-recall-check)")
    elif result.recall_score is not None:
        logging.info(
            "Recall: %.0f%% (gate %s)",
            result.recall_score * 100,
            "passed" if result.recall_passed else "FAILED",
        )
        if result.recall_passed is False:
            logging.error("Recall gate FAILED — search quality degraded. Check logs.")

    # Post-embed summarise — non-critical; failures only logged
    if not args.skip_summarise:
        _run_post_embed_summarise()

    if not result.success:
        return 1
    if result.recall_passed is False:
        return 1
    return 0


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


def main(argv: list[str] | None = None) -> None:
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
        "--rebuild-canaries",
        action="store_true",
        help=(
            "Discard the persisted recall canary suite and sample fresh from "
            "the corpus. Use after a major index rebuild."
        ),
    )
    embed_p.add_argument(
        "--skip-summarise",
        action="store_true",
        help="Skip post-embed L0 summary generation",
    )

    # recall-check
    sub.add_parser("recall-check", help="Run recall quality check standalone")

    # status
    sub.add_parser("status", help="Show embedding status")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.command is None or args.command == "embed":
        if not hasattr(args, "force"):
            # Default subcommand
            args.force = False
            args.limit = None
            args.batch_size = DEFAULT_BATCH_SIZE
            args.skip_recall_check = False
            args.rebuild_canaries = False
            args.skip_summarise = False
        sys.exit(cmd_embed(args))
    elif args.command == "recall-check":
        sys.exit(cmd_recall(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))


if __name__ == "__main__":
    main()
