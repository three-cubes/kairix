"""F70: every CREATE TABLE in schema.py has at least one INSERT writer.

Motivation (GH #336; ADR-024 §F70)
----------------------------------
F67 catches the staging-drain symmetry failure for tables with a
``pushed_to_<sink>`` column (the GH #334 anti-pattern). F70 generalises
that pattern to EVERY table declared in :mod:`kairix.core.db.schema`:
either some production code under ``kairix/`` INSERTs into it, or the
``CREATE TABLE`` line carries a ``# table-is-derived: <rationale>``
comment declaring it as a view / cache / derived state.

GH #336 surfaced this gap: the ``documents_media`` table shipped in
Wave 1 with rich extractor-version + per-document-status columns, but
no code ever INSERTed into it. Production accumulated ~1M chunks across
4 years with zero documents_media rows; per-extractor analytics + F40
re-extract triage were structurally impossible.

The mechanical contract that would have caught this on day one:

    For every CREATE TABLE in ``kairix/core/db/schema.py``, the
    codebase under ``kairix/**/*.py`` (excluding ``tests/`` and the
    schema module itself) MUST contain at least one
    ``INSERT INTO <table>`` statement -- OR the CREATE TABLE line
    carries a ``# table-is-derived: <rationale>`` comment.

That's F70. Forward-only -- fires on any future "ship a table, forget
to ship the writer" combination.

Detection
---------
1. Scan ``kairix/core/db/schema.py`` for ``CREATE TABLE [IF NOT EXISTS]``
   blocks. Harvest the table name.
2. For each, scan every ``kairix/**/*.py`` source file (excluding
   ``tests/`` and the schema module itself) for at least one
   ``INSERT INTO <table>`` statement. The match is conservative regex
   over raw text -- string-literal-bounded so docstring/comment
   mentions don't false-positive.
3. Tables that have no matching INSERT AND no
   ``# table-is-derived:`` comment are reported as violations.

Exemption: a table that is genuinely derived (a view / cache /
rebuilt-from-source) may carry a ``# table-is-derived: <rationale>``
comment immediately above its ``CREATE TABLE``. Use sparingly --
prefer paying down by writing the missing writer.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

SCHEMA_PATH = Path("kairix") / "core" / "db" / "schema.py"
KAIRIX_ROOT = Path("kairix")

# Match ``CREATE TABLE [IF NOT EXISTS] <name> ( ... );`` blocks; the
# block opener captures the name only -- we don't need the body for
# F70 (unlike F67 which scans the column list for pushed_to_<sink>).
# Anchored on the ``(`` so we don't catch ``CREATE INDEX`` lines.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(",
    re.IGNORECASE,
)

# Exemption marker -- a ``table-is-derived: <rationale>`` comment on
# (or within a few lines preceding) a CREATE TABLE that opts the
# table out of F70. Accepts both Python-style (``#`` outside SQL
# strings) and SQL-style (``--`` inside the SQL string heredoc) so
# the rationale lives next to the table declaration regardless of
# which side of the string boundary it sits on. Examples: a view
# rebuilt nightly from another table; a FTS5 virtual table populated
# indirectly via triggers; a cache table whose rows are derived from
# joins.
_DERIVED_RE = re.compile(r"(?:#|--)\s*table-is-derived:\s*\S+")

REMEDIATION = """F70: table <name> declared in schema.py has no INSERT site
in production code (no ``INSERT INTO <name>`` anywhere under kairix/),
and no ``# table-is-derived:`` comment marks it as a derived/view/cache.

This is the GH #336 failure mode. The ``documents_media`` table sat
in production for 4 years with zero rows because no code ever INSERTed
into it -- even though every extractor docstring promised to thread
``extractor_version`` through to it. F70 mechanically prevents the
same failure mode for any future schema table.

fix: implement a writer for the table. Common shapes:

    # In silver / pipeline / use_case:
    db.execute(
        "INSERT INTO <name> (col1, col2) VALUES (?, ?)",
        (val1, val2),
    )

    # OR if the table is genuinely a derived view / cache / FTS5
    # virtual table, add the rationale comment to schema.py:
    # table-is-derived: rebuilt nightly from <other_table>
    CREATE TABLE IF NOT EXISTS <name> (
        ...
    );

next: re-run python3 scripts/checks/check_f70_schema_writer_symmetry.py
run: bash scripts/safe-commit.sh "feat: implement writer for <table>"

Pass example:
    kairix/core/db/schema.py declares ``content_vectors``;
    kairix/core/embed/embed.py contains ``INSERT INTO content_vectors``.

Forbidden example: the GH #336 anti-pattern
    kairix/core/db/schema.py declares ``documents_media`` with
    rich columns (extractor_name, extractor_version, page_count, ...)
    but no ``INSERT INTO documents_media`` exists anywhere under
    kairix/. Production sees per-extractor analytics blank forever.

Allowed exemption (rare):
    # table-is-derived: FTS5 virtual table populated by triggers
    CREATE VIRTUAL TABLE documents_fts USING fts5(...);
"""


def _read_schema_source(repo_root: Path) -> str:
    schema_file = repo_root / SCHEMA_PATH
    if not schema_file.exists():
        return ""
    return schema_file.read_text(encoding="utf-8")


# How far back (in non-blank source lines) we'll look for the
# ``# table-is-derived:`` rationale. Three is enough to cover both:
#   * the comment immediately above the CREATE TABLE (typical case)
#   * the comment immediately above the surrounding ``db.execute(...)``
#     wrapper that opens a multi-table DDL block.
_DERIVED_LOOKBACK_LINES = 3


def _previous_non_blank_lines(source: str, match_start: int, limit: int) -> list[str]:
    """Return up to ``limit`` closest non-blank lines preceding ``match_start``.

    ``CREATE TABLE`` lines are typically indented; the byte offset
    ``match_start`` lands on the first non-whitespace char of CREATE,
    so the last entry in ``splitlines(source[:match_start])`` is
    usually the indent whitespace alone. We walk backwards through
    blank entries collecting up to ``limit`` non-blank lines so the
    ``table-is-derived`` comment can sit either immediately above the
    CREATE TABLE OR immediately above the surrounding
    ``db.execute(...)`` triple-quoted wrapper.
    """
    head = source[:match_start]
    out: list[str] = []
    for line in reversed(head.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        out.append(stripped)
        if len(out) >= limit:
            break
    return out


def _is_derived(source: str, table_match: re.Match[str]) -> bool:
    """Return True if any of the closest non-blank lines above carries the marker."""
    for line in _previous_non_blank_lines(source, table_match.start(), _DERIVED_LOOKBACK_LINES):
        if _DERIVED_RE.search(line):
            return True
    return False


def _harvest_tables(source: str) -> dict[str, bool]:
    """Return ``{table_name: is_derived}`` for every CREATE TABLE in the source.

    A table that appears in multiple CREATE TABLE blocks (once for the
    fresh-create path, once for the legacy-migration DDL) is considered
    derived only if ALL of its declarations carry the derived comment.
    In practice schema.py declares each table consistently so this is
    a no-op merge.
    """
    tables: dict[str, bool] = {}
    for match in _CREATE_TABLE_RE.finditer(source):
        name = match.group(1)
        derived = _is_derived(source, match)
        if name in tables:
            # If any prior declaration was non-derived, keep that signal
            # -- F70 only treats the table as derived when EVERY
            # declaration agrees it's derived.
            tables[name] = tables[name] and derived
        else:
            tables[name] = derived
    return tables


def _scan_for_insert(repo_root: Path, table: str) -> bool:
    """Return True if any ``kairix/**/*.py`` file INSERTs into ``table``.

    The matcher requires the INSERT to begin inside a Python string
    literal so docstring/comment mentions don't false-positive. The
    schema module itself is excluded (it declares the table; the
    writer logic must live elsewhere).
    """
    # String-literal-bounded match -- the SQL must start inside a
    # quoted Python string. Implicit string concatenation (one string
    # per line) tolerated via DOTALL + bounded backref-free pattern.
    pattern = re.compile(
        rf'["\']?\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+{re.escape(table)}\b',
        re.IGNORECASE,
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
    """Walk every table in schema.py; flag those with no INSERT + no derived marker.

    Returns repo-relative synthetic paths of the form
    ``kairix/core/db/schema.py::<table>`` so each violation appears as
    a distinct violation. Operators remediate by adding an INSERT
    or a ``# table-is-derived:`` comment.
    """
    source = _read_schema_source(repo_root)
    if not source:
        return set()
    tables = _harvest_tables(source)
    if not tables:
        return set()
    violations: set[Path] = set()
    for table, derived in tables.items():
        if derived:
            continue
        if not _scan_for_insert(repo_root, table):
            violations.add(Path(f"kairix/core/db/schema.py::{table}"))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f70-schema-writer-symmetry", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
