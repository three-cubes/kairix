"""F77: ``sqlite3.connect`` call sites must live in the allow-list.

Engagement-scope writes route through the worker's single coordinator
(the per-tick transaction in ``ConnectorPipeline._process_item`` and
the per-pass transaction in ``MaintenanceScheduler.tick``). SQLite WAL
keeps correctness across multiple connections but contention is
invisible — two writers locking the same page means each waits, and
the visible signal is latency-under-load, never a test failure.

F77 makes the writer-coordinator discipline structural. Every
``sqlite3.connect(...)`` call site under ``kairix/`` must sit inside the
rule's declared writer-coordinator scope (``_ALLOWLIST_PATHS`` — the
rule definition, not a grandfather list); any other connect site trips
the gate.

Acknowledged limitation (status: proxy)
---------------------------------------
This is a STRUCTURAL approximation of the writer-coordinator concern.
The real contract is "no two SQLite connections write to the same
engagement-scope DB concurrently." A test could pass F77 (no new
``sqlite3.connect``) and still bypass the coordinator by passing a
worker-owned connection into a background thread. The mechanical
fitness function can't see the thread boundary; the operator must.

The allow-list captures legitimate writer sites:

* ``kairix/worker.py`` — the tick loop, owner of the engagement DB
  connection.
* ``kairix/core/factory.py`` — wires the connection into the
  ConnectorPipeline + MaintenanceScheduler constructors.
* ``kairix/core/db/`` — schema bootstrap, integrity preflights, scanners.
  These run at start-up and operator-invoked diagnostic time.
* ``kairix/agents/mcp/`` — MCP tools that read (and sometimes write)
  through the same factory wiring.
* ``kairix/cli.py`` + per-subcommand modules — operator commands.
* ``scripts/`` — out-of-process maintenance / migration utilities.
* ``tests/`` — fixtures + harnesses use ``:memory:`` + tmp_path
  databases; not the production engagement DB.

If a new component genuinely needs its own connection (a new
out-of-process daemon, a new operator CLI surface), add its path to
``_ALLOWLIST_PATHS`` in a commit that also explains *why* the
coordinator can't own the connection.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import python_files, repo_relative  # noqa: F401 — back-compat

# Exact-path or prefix allow-list for ``sqlite3.connect`` call sites.
# Match: rel-path starts with one of these prefixes (Path semantics).
_ALLOWLIST_PATHS: tuple[str, ...] = (
    "kairix/worker.py",
    "kairix/cli.py",
    "kairix/core/factory.py",
    "kairix/core/db/",
    "kairix/agents/mcp/",
    "kairix/core/connectors/",  # framework — already coordinated via factory
    "kairix/core/maintenance/",  # tick-driven, same coordinator
    # ADR-029 G.1: agent-query-queue worker needs its own connection because
    # work runs on a background ThreadPoolExecutor and the existing factory
    # connection is single-thread. The queue's writes are scoped to a single
    # table (pending_queries) and are serialised by a module-level
    # threading.Lock so contention stays bounded. See module docstring at
    # kairix/core/queue/dispatch.py for the rationale.
    "kairix/core/queue/",
)

_EXEMPT_COMMENT = "# F77-allow:"

REMEDIATION = """F77: net-new sqlite3.connect(...) call site outside the writer-coordinator allow-list.

Every engagement-scope SQLite write must serialize through the worker
tick loop's transaction or the maintenance scheduler's pass — both
own a single connection passed in via kairix/core/factory.py. A new
sqlite3.connect() call opens a SECOND writer; SQLite WAL keeps
correctness but contention becomes invisible (the visible signal is
latency-under-load, never a test failure).

fix: route through the existing factory. ConnectorPipeline +
  MaintenanceScheduler both accept ``db: sqlite3.Connection`` in
  their constructors; the factory wires the shared connection from
  WorkerDeps. If you genuinely need a new out-of-process daemon
  with its own connection, add the path to _ALLOWLIST_PATHS in
  scripts/checks/check_f77_sqlite_single_writer.py with a rationale
  explaining why the coordinator can't own it.
next: re-run python3 scripts/checks/check_f77_sqlite_single_writer.py
  to confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(<area>): route SQLite access through factory"

Pass example:
  # accept the connection; never open one
  class MyConnector:
      def __init__(self, db: sqlite3.Connection) -> None:
          self._db = db

Forbidden example:
  # kairix/connectors/sharepoint/sync.py — F77 fires
  import sqlite3
  db = sqlite3.connect("/data/kairix/index.sqlite")  # SECOND writer!

Allowed exemption (rare, justified):
  # F77-allow: out-of-process diagnostic CLI, never runs in the worker
  db = sqlite3.connect(path)

Why: see ADR-026 blindspot audit — concurrency / coordinator discipline.
Production has 989K chunks + 2.1M vectors; a second writer opens a
contention surface that doesn't surface until production load profile
hits. The structural rule + allow-list keeps the writer count
auditable across the tree."""


def _is_allowlisted(rel_path: Path) -> bool:
    """True if ``rel_path`` (repo-relative POSIX-style) starts with any
    allowlist prefix.
    """
    s = str(rel_path).replace("\\", "/")
    for prefix in _ALLOWLIST_PATHS:
        if s == prefix or s.startswith(prefix):
            return True
    return False


def _is_sqlite_connect_call(node: ast.Call) -> bool:
    """True if ``node`` is ``sqlite3.connect(...)``.

    Matches both the canonical ``sqlite3.connect`` Attribute form and
    the rebound ``connect`` Name form when imported via
    ``from sqlite3 import connect``.
    """
    if isinstance(node.func, ast.Attribute) and node.func.attr == "connect":
        recv = node.func.value
        if isinstance(recv, ast.Name) and recv.id == "sqlite3":
            return True
    return False


def _line_carries_exempt(source: str, lineno: int) -> bool:
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        if _EXEMPT_COMMENT in lines[lineno - 1]:
            return True
    if 2 <= lineno <= len(lines):
        if _EXEMPT_COMMENT in lines[lineno - 2]:
            return True
    return False


def _file_has_violation(path: Path, repo_root: Path) -> bool:
    """True if ``path`` opens a ``sqlite3.connect`` outside the allow-list
    and without an F77-allow rationale.
    """
    try:
        rel = path.resolve().relative_to(repo_root)
    except ValueError:
        return False
    if _is_allowlisted(rel):
        return False

    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    # Cheap pre-filter — skip AST parse when the keyword isn't present.
    if "sqlite3" not in source:
        return False

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_sqlite_connect_call(node):
            if _line_carries_exempt(source, node.lineno):
                continue
            return True
    return False


class F77(FitnessRule):
    """F77 as a FitnessRule subclass — see module docstring."""

    name = "f77-sqlite-single-writer"
    remediation = REMEDIATION
    roots = ("kairix",)

    def file_has_violation(self, path: Path) -> bool:
        return _file_has_violation(path, self._repo_root)


def main() -> int:
    return F77().run()


if __name__ == "__main__":
    sys.exit(main())
