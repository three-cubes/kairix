"""F67: every staging table with a ``pushed_to_<sink>`` flag has a drain code path.

Motivation (GH #334)
--------------------
The ``entity_signals`` staging table shipped in Wave 2 with a
``pushed_to_neo4j INTEGER DEFAULT 0`` column and a sink-side
:class:`kairix.worker._SqliteEntityGraphSink.buffer` writer. The Wave-2
docstring promised "a separate worker job (Wave 3+) drains the table
and pushes to Neo4j" — but nobody wrote that drain. Two years of
production ingest accumulated 2.3M un-pushed rows; nothing flipped
``pushed_to_neo4j`` from 0 → 1 anywhere in the codebase. The integrity
preflight's ``LIMIT 1000`` cap masked the scale (count read out as
1000 instead of 2.3M).

The mechanical contract that would have caught this on day one:

    For every SQLite table in ``kairix/core/db/schema.py`` whose
    schema contains a column matching ``pushed_to_<sink>``, the
    codebase under ``kairix/**/*.py`` (excluding ``tests/``) MUST
    contain at least one ``UPDATE <table> SET pushed_to_<sink> = 1``
    statement.

That's F67. Forward-only — fires on any future "ship a staging table,
forget to ship the drain" combination.

Detection
---------
1. Scan ``kairix/core/db/schema.py`` for ``CREATE TABLE`` blocks. For
   each, harvest the table name and the set of column names that match
   the regex ``pushed_to_([a-z_]+)`` (case-insensitive).
2. For each ``(table, sink_suffix)`` tuple found, scan every
   ``kairix/**/*.py`` source file (excluding ``tests/`` and the
   schema module itself) for at least one ``UPDATE <table> SET ...
   pushed_to_<sink_suffix> = 1`` statement. The check is conservative:
   it matches a regex over the raw text of each file.
3. Tables that have no matching UPDATE are reported as violations.

The violation is reported keyed by the schema file path (so the
operator has a single place to grep / add the drain).

Exemption: a table that genuinely should not have a drain (e.g. an
audit-log staging table that simply accumulates) may carry a
``# F67-exempt: <rationale>`` comment immediately above its
``CREATE TABLE`` block. Use sparingly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

SCHEMA_PATH = Path("kairix") / "core" / "db" / "schema.py"
KAIRIX_ROOT = Path("kairix")
TESTS_ROOT = Path("tests")

# Match ``CREATE TABLE IF NOT EXISTS <name> ( ... );`` blocks; greedy
# enough to capture the column list, lazy enough to stop at the first
# ``);`` after the block opener.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)

# Column name pattern — ``pushed_to_<sink>`` where sink is one or more
# lowercase letters / digits / underscores (covers e.g. ``pushed_to_neo4j``).
# Captures the suffix.
_PUSHED_TO_COL_RE = re.compile(r"pushed_to_([a-z][a-z0-9_]*)", re.IGNORECASE)

# Pre-block exemption marker — a comment on the line immediately above
# a CREATE TABLE that opts the table out of F67.
_F67_EXEMPT_RE = re.compile(r"#\s*F67-exempt:\s*\S+")

REMEDIATION = """F67: staging table <name> declares pushed_to_<sink>=0 but no
code path updates pushed_to_<sink> to 1 — the staged rows will accumulate forever.

This is the GH #334 failure mode. The ``entity_signals`` table sat in
production for over two years with 2.3M un-pushed rows because the
drain that flips ``pushed_to_neo4j`` from 0 → 1 was never written.
F67 mechanically prevents the same failure mode for any future
``pushed_to_<sink>`` staging table.

fix: implement a drain function for the table. Pattern:

    def run_<sink>_drain_tick(db, repo, *, batch_size=500) -> ...:
        rows = db.execute("SELECT id, ... FROM <table> WHERE pushed_to_<sink> = 0 "
                          "ORDER BY modified_at ASC LIMIT ?", (batch_size,)).fetchall()
        for row in rows:
            # ... push to <sink> ...
            db.execute("UPDATE <table> SET pushed_to_<sink> = 1, pushed_at = ? WHERE id = ?",
                       (utc_now_iso(), row[0]))
        db.commit()

Then wire it into the worker tick loop alongside the connector_sync slot.
See kairix/core/curator/drain.py for the canonical implementation
(GH #334; Neo4j entity-graph drain).

next: re-run python3 scripts/checks/check_f67_staging_drain_symmetry.py
run: bash scripts/safe-commit.sh "feat: implement drain for <table>"

Pass example:

    kairix/core/db/schema.py:
        CREATE TABLE entity_signals (... pushed_to_neo4j INTEGER DEFAULT 0, ...);

    kairix/core/curator/drain.py:
        db.execute("UPDATE entity_signals SET pushed_to_neo4j = 1, ...")

Forbidden example: (this is the GH #334 anti-pattern):

    kairix/core/db/schema.py:
        CREATE TABLE entity_signals (... pushed_to_neo4j INTEGER DEFAULT 0, ...);
    # ... no UPDATE pushed_to_neo4j = 1 anywhere in kairix/

Allowed exemption (rare):

    # F67-exempt: audit-log staging; rows aggregate then expire via TTL GC
    CREATE TABLE audit_signals (... pushed_to_audit_log INTEGER DEFAULT 0, ...);
"""


def _read_schema_source(repo_root: Path) -> str:
    schema_file = repo_root / SCHEMA_PATH
    if not schema_file.exists():
        return ""
    return schema_file.read_text(encoding="utf-8")


def _line_before_match(source: str, match_start: int) -> str:
    """Return the line of text immediately preceding the byte offset ``match_start``."""
    head = source[:match_start]
    lines = head.splitlines()
    if not lines:
        return ""
    return lines[-1].strip()


def _is_exempt_block(source: str, table_match: re.Match[str]) -> bool:
    """Return True if the line above the CREATE TABLE carries F67-exempt."""
    prior = _line_before_match(source, table_match.start())
    return bool(_F67_EXEMPT_RE.search(prior))


def _harvest_staging_tables(source: str) -> dict[str, list[str]]:
    """Return ``{table_name: [sink_suffix, ...]}`` for every staging table.

    A "staging table" here = any CREATE TABLE block that declares at
    least one column matching the pushed_to_<sink> pattern. The map
    value is the list of sink suffixes (most tables will carry exactly
    one; the multi-sink shape is supported for future flexibility).
    """
    staging: dict[str, list[str]] = {}
    for table_match in _CREATE_TABLE_RE.finditer(source):
        if _is_exempt_block(source, table_match):
            continue
        table_name = table_match.group(1)
        body = table_match.group(2)
        sinks: list[str] = []
        for col_match in _PUSHED_TO_COL_RE.finditer(body):
            sink = col_match.group(1).lower()
            if sink not in sinks:
                sinks.append(sink)
        if sinks:
            # Deduplicate per table — the schema declares the same
            # table twice (once for fresh-create, once for migration);
            # F67 only needs to know "this is a staging table" once.
            existing = staging.get(table_name, [])
            for s in sinks:
                if s not in existing:
                    existing.append(s)
            staging[table_name] = existing
    return staging


def _scan_for_drain_update(
    repo_root: Path,
    table: str,
    sink: str,
) -> bool:
    """Return True if any ``kairix/**/*.py`` file UPDATE-sets the flag to 1.

    The matcher is intentionally loose — any UPDATE statement that
    names both the table and ``pushed_to_<sink> = 1`` (in either
    order, single- or multi-line) counts. Misses a drain that uses
    parameterised constants or a string-builder; those are rare enough
    that the false-negative rate is acceptable. When a false negative
    DOES happen, the operator's escape hatch is a one-line
    F67-exempt comment with rationale.
    """
    # Match a string-literal-bounded UPDATE — the SQL must START
    # inside a quoted Python string so docstring/comment mentions
    # don't false-positive. Python's implicit string concatenation
    # means the body can carry interleaved quote chars (one string
    # per line); the gap tolerates them. The required leading ``"``
    # quote (with optional space) anchors the match to source-code
    # SQL, not prose.
    pattern = re.compile(
        rf'"\s*UPDATE\s+{re.escape(table)}\b.{{0,800}}?pushed_to_{re.escape(sink)}\s*=\s*1',
        re.IGNORECASE | re.DOTALL,
    )
    kairix_dir = repo_root / KAIRIX_ROOT
    if not kairix_dir.exists():
        return False
    schema_rel = SCHEMA_PATH
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            rel = path.resolve().relative_to(repo_root)
        except ValueError:
            continue
        # Exclude the schema module itself (it declares the column;
        # the drain logic must live elsewhere).
        if rel == schema_rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every staging table; flag any that has no matching drain UPDATE.

    Returns the set of repo-relative paths to report; today the only
    candidate is the schema file itself (which is where the violation
    is visible — the missing drain is implicit). Future enhancement
    might surface the suggested drain module path.
    """
    source = _read_schema_source(repo_root)
    if not source:
        return set()
    staging = _harvest_staging_tables(source)
    if not staging:
        return set()
    violations: set[Path] = set()
    for table, sinks in staging.items():
        for sink in sinks:
            if not _scan_for_drain_update(repo_root, table, sink):
                # Report keyed by a synthetic path that encodes the
                # table+sink so the failure output lists "what's
                # broken" in human-readable form. Operators remediate
                # by adding a drain UPDATE.
                violations.add(Path(f"kairix/core/db/schema.py::{table}::pushed_to_{sink}"))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f67-staging-drain-symmetry", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
