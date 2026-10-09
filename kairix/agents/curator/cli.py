"""
Curator agent CLI for Kairix.

Usage:
  kairix curator health [--format text|json] [--output FILE]
                        [--staleness-days N]
  kairix curator drain  [--batch-size N] [--max-batches N]
                        [--db-path PATH] [--format text|json] [--dry-run]

`health` reports on entity-graph data quality (CA-1).

`drain` (GH #334) pushes staged ``entity_signals`` rows into Neo4j.
Operators run it manually for catch-up; the worker tick runs it on a
10-minute cadence (see ``kairix/worker.py``).

Exit code:
  * `health`  always 0 — health issues are surfaced via the report.
  * `drain`   0 on success / no-op; 0 when Neo4j unavailable (the
              envelope reports ``neo4j_available=false``); 2 on
              argparse error. Per-row failures stay 0 because the
              drain is designed to keep ticking through transient
              graph errors.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _default_neo4j_client_factory() -> Any:
    """Production factory: defers the heavy graph-client import until call time."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _default_drain_db_factory(db_path: Path | None) -> sqlite3.Connection:
    """Production seam: open the worker DB or the CLI-supplied path.

    Lifted out so the drain command's outcome test (F30) injects a
    sandboxed DB via the ``--db-path`` flag rather than relying on the
    KAIRIX_DB_PATH env var (which the test brief explicitly forbids
    in subprocess invocations).
    """
    if db_path is not None:
        # F77-allow: operator CLI subcommand (curator drain); per-invocation read of the worker DB.
        return sqlite3.connect(str(db_path))
    from kairix.paths import db_path as _db_path

    # F77-allow: operator CLI subcommand (curator drain); per-invocation read of the worker DB.
    return sqlite3.connect(str(_db_path()))


def _default_drain_repo_factory(client: Any) -> Any:
    """Production seam: wrap a Neo4jClient in the GraphRepository facade.

    Tests pass a :class:`tests.fakes.FakeDrainGraphRepository` directly
    via the ``drain_repo`` kwarg on :func:`main`, bypassing this factory.
    """
    from kairix.knowledge.graph.repository import Neo4jGraphRepository

    # raise_on_error: a rejected MERGE must mark the signal failed, never pushed.
    return Neo4jGraphRepository(client, raise_on_error=True)


def _health_cmd(
    args: argparse.Namespace,
    *,
    neo4j_client: Any = None,
    client_factory: Callable[[], Any] = _default_neo4j_client_factory,
) -> None:
    from kairix.agents.curator.health import (
        format_report_json,
        format_report_text,
        run_health_check,
    )

    if neo4j_client is None:
        neo4j_client = client_factory()

    report = run_health_check(neo4j_client, staleness_days=args.staleness_days)

    output = format_report_json(report) if args.format == "json" else format_report_text(report)

    if args.output:
        # S8707: --output is an agent/operator-supplied write path. Confine it
        # to the working-area allow-list before writing so a crafted escape is
        # rejected rather than written off-tree.
        from kairix.paths import PathTraversalError, agent_cli_roots, confine_to_roots

        try:
            confine_to_roots(args.output, agent_cli_roots()).write_text(output, encoding="utf-8")
        except PathTraversalError as exc:
            print(f"ERROR: could not write output file: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Health report written to {args.output}")
    else:
        print(output, end="")

    sys.exit(0)


def _format_drain_result_text(result_dict: dict[str, Any], *, batches_run: int, dry_run: bool) -> str:
    """Render the drain envelope as one human-readable line per field.

    Operators run `kairix curator drain` interactively to catch up the
    backlog; the text format is the default so the most common shell
    invocation prints something useful without a JSON pipe. The JSON
    format stays available via `--format json` for cron / agents.
    """
    prefix = "[DRY RUN] " if dry_run else ""
    return (
        f"{prefix}neo4j drain complete\n"
        f"  batches_run            : {batches_run}\n"
        f"  pushed                 : {result_dict['pushed']}\n"
        f"  failed                 : {result_dict['failed']}\n"
        f"  skipped_relationships  : {result_dict['skipped_relationships']}\n"
        f"  neo4j_available        : {result_dict['neo4j_available']}\n"
        f"  elapsed_ms             : {result_dict['elapsed_ms']}\n"
    )


def _resolve_drain_repo(
    *,
    drain_repo: Any,
    neo4j_client: Any,
    client_factory: Callable[[], Any],
    repo_factory: Callable[[Any], Any],
) -> Any:
    """Pick the graph repository to drain into.

    Resolution order — test-injected ``drain_repo`` wins; otherwise the
    ``neo4j_client`` (test-injected) is wrapped via ``repo_factory``;
    otherwise the production client factory builds both.
    """
    if drain_repo is not None:
        return drain_repo
    client = neo4j_client if neo4j_client is not None else client_factory()
    return repo_factory(client)


class _DryRunDrainRepo:
    """Drain-side stand-in used when ``--dry-run`` is on.

    Reports as ``available`` so the drain reads rows + dispatches, but
    every ``cypher`` call is a no-op that records the would-have-pushed
    query. The drain itself never flips any flag because the dry-run
    DB wrapper (:class:`_DryRunDbWrapper`) intercepts UPDATE writes.
    """

    available: bool = True

    def __init__(self) -> None:
        self.cypher_calls: list[tuple[str, dict[str, Any]]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.cypher_calls.append((query, dict(params or {})))
        return []


class _DryRunDbWrapper:
    """SQLite connection wrapper that drops UPDATE / INSERT / DELETE writes.

    SELECT (and PRAGMA) statements pass through unchanged so the drain
    can read its work queue. Any ``execute(<UPDATE|INSERT|DELETE>...)``
    call is intercepted: the wrapper hands back a no-op cursor whose
    ``rowcount`` is 0. ``commit`` is also a no-op so the underlying
    connection's state is never written.

    This is the dry-run isolation primitive — letting the drain run
    its full read-and-dispatch path without modifying the DB, so the
    operator sees an honest envelope (``pushed=N``, etc.) without any
    persistent state change. Earlier attempts used a SAVEPOINT but
    broke when the drain's mid-batch ``commit()`` ended the transaction
    the savepoint was anchored in.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> Any:
        sql_lead = sql.lstrip().upper()[:6]
        if sql_lead.startswith(("UPDATE", "INSERT", "DELETE")):
            return _NoopCursor()
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Any) -> Any:
        sql_lead = sql.lstrip().upper()[:6]
        if sql_lead.startswith(("UPDATE", "INSERT", "DELETE")):
            return _NoopCursor()
        return self._conn.executemany(sql, params)

    def commit(self) -> None:
        # Intentionally a no-op — the dry-run path never persists.
        return None

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class _NoopCursor:
    """Cursor stand-in for the dry-run UPDATE/INSERT/DELETE path."""

    rowcount: int = 0
    lastrowid: int | None = None

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> Any:
        return None


def _run_drain_batches(drain_db: Any, repo: Any, *, max_batches: int, batch_size: int) -> tuple[Any, int]:
    """Run up to ``max_batches`` drain ticks, summing them into one result.

    ``max_batches`` is at least 1 (``--max-batches`` rejects anything
    smaller at the parser), so the first tick always runs and seeds the
    aggregate — the reported ``neo4j_available`` is always an observed
    value, never an assumed default. Stops early when Neo4j is
    unavailable or a tick reports nothing pushed / failed / skipped (the
    queue is empty). Returns the aggregate ``NeoDrainResult`` and the
    number of ticks actually run.
    """
    from kairix.core.curator.drain import NeoDrainResult, run_neo4j_drain_tick

    tick = run_neo4j_drain_tick(drain_db, repo, batch_size=batch_size)
    aggregate = tick
    batches_run = 1
    # Keep ticking while Neo4j is reachable, the queue still yielded work
    # last tick, and the batch budget isn't spent.
    while (
        batches_run < max_batches
        and tick.neo4j_available
        and (tick.pushed or tick.failed or tick.skipped_relationships)
    ):
        tick = run_neo4j_drain_tick(drain_db, repo, batch_size=batch_size)
        batches_run += 1
        aggregate = NeoDrainResult(
            pushed=aggregate.pushed + tick.pushed,
            failed=aggregate.failed + tick.failed,
            skipped_relationships=aggregate.skipped_relationships + tick.skipped_relationships,
            neo4j_available=tick.neo4j_available,
            elapsed_ms=aggregate.elapsed_ms + tick.elapsed_ms,
        )
    return aggregate, batches_run


def _positive_int(option_value: str) -> int:
    """argparse ``type=`` for counts that must be at least 1."""
    try:
        value = int(option_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number >= 1, got {option_value!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _drain_cmd(
    args: argparse.Namespace,
    *,
    neo4j_client: Any = None,
    client_factory: Callable[[], Any] = _default_neo4j_client_factory,
    drain_repo: Any = None,
    repo_factory: Callable[[Any], Any] = _default_drain_repo_factory,
    db_factory: Callable[[Path | None], sqlite3.Connection] = _default_drain_db_factory,
) -> None:
    """GH #334 — drain ``entity_signals`` → Neo4j.

    Iterates the drain tick up to ``--max-batches`` times so an
    operator catch-up converges within one CLI invocation. Stops early
    when a tick reports ``pushed=0 AND failed=0`` (queue is empty).

    ``--dry-run`` substitutes a stand-in repo + a DB wrapper that
    silently drops every write — the drain runs its full read-and-
    dispatch path without modifying any state. Useful for "what would
    this batch touch?" before committing to a real drain.
    """
    db_path = Path(args.db_path) if args.db_path else None
    raw_db = db_factory(db_path)
    repo: Any
    drain_db: Any
    if args.dry_run:
        repo = _DryRunDrainRepo()
        drain_db = _DryRunDbWrapper(raw_db)
    else:
        repo = _resolve_drain_repo(
            drain_repo=drain_repo,
            neo4j_client=neo4j_client,
            client_factory=client_factory,
            repo_factory=repo_factory,
        )
        drain_db = raw_db

    try:
        aggregate, batches_run = _run_drain_batches(
            drain_db, repo, max_batches=args.max_batches, batch_size=args.batch_size
        )
    finally:
        raw_db.close()

    result_dict = asdict(aggregate)
    if args.format == "json":
        print(json.dumps({**result_dict, "batches_run": batches_run, "dry_run": bool(args.dry_run)}))
    else:
        print(_format_drain_result_text(result_dict, batches_run=batches_run, dry_run=bool(args.dry_run)), end="")
    sys.exit(0)


def _add_health_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    health_parser = subparsers.add_parser(
        "health",
        help="Run entity graph health check (CA-1)",
    )
    health_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text (vault-ready Markdown) or json (default: text)",
    )
    health_parser.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Write report to FILE instead of stdout",
    )
    health_parser.add_argument(
        "--staleness-days",
        type=int,
        default=90,
        dest="staleness_days",
        metavar="N",
        help="Flag entities with no activity for N days as stale (default: 90)",
    )
    health_parser.set_defaults(func=_health_cmd)


def _add_drain_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    drain_parser = subparsers.add_parser(
        "drain",
        help="GH #334 — drain entity_signals into Neo4j (Wave-3 Curator boundary)",
    )
    drain_parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        dest="batch_size",
        metavar="N",
        help="Rows drained per tick (default: 500)",
    )
    drain_parser.add_argument(
        "--max-batches",
        type=_positive_int,
        default=1,
        dest="max_batches",
        metavar="N",
        help="Maximum number of ticks to run before exiting, at least 1 (default: 1)",
    )
    drain_parser.add_argument(
        "--db-path",
        default=None,
        dest="db_path",
        metavar="PATH",
        help="SQLite database path (default: production kairix DB resolved via paths.db_path)",
    )
    drain_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text (operator-readable) or json (default: text)",
    )
    drain_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Read rows + log what would be MERGEd; do NOT flip any flag (Neo4j stays untouched)",
    )
    drain_parser.set_defaults(func=_drain_cmd)


def main(
    argv: list[str] | None = None,
    *,
    neo4j_client: Any = None,
    client_factory: Callable[[], Any] = _default_neo4j_client_factory,
    drain_repo: Any = None,
    repo_factory: Callable[[Any], Any] = _default_drain_repo_factory,
    db_factory: Callable[[Path | None], sqlite3.Connection] = _default_drain_db_factory,
) -> None:
    """Entry point for `kairix curator` subcommand.

    The ``neo4j_client`` keyword lets BDD/integration tests inject a
    ``FakeNeo4jClient`` directly into the health command path. The
    ``drain_repo`` keyword is the equivalent seam for the drain path —
    it injects a :class:`tests.fakes.FakeDrainGraphRepository` so tests
    never instantiate the real Neo4j driver.

    The ``db_factory`` keyword is the drain's DB seam — production
    omits it and the default reads ``kairix.paths.db_path()``; tests
    pass a closure that returns a sqlite3 connection rooted at
    ``tmp_path``.
    """
    parser = argparse.ArgumentParser(
        prog="kairix curator",
        description="Curator agent: entity graph health monitoring + Neo4j drain.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    _add_health_parser(subparsers)
    _add_drain_parser(subparsers)

    parsed = parser.parse_args(argv)
    if parsed.func is _health_cmd:
        _health_cmd(parsed, neo4j_client=neo4j_client, client_factory=client_factory)
    elif parsed.func is _drain_cmd:
        _drain_cmd(
            parsed,
            neo4j_client=neo4j_client,
            client_factory=client_factory,
            drain_repo=drain_repo,
            repo_factory=repo_factory,
            db_factory=db_factory,
        )
    else:  # pragma: no cover — argparse rejects unknown subcommands before this branch
        parsed.func(parsed)


if __name__ == "__main__":
    main()
