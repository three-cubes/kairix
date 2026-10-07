"""F57: cc_pair lifecycle state-machine integrity.

ADR v2 §3 (``docs/architecture/connector-scope-topology/ADR.md``) defines
``topology_cc_pairs.status`` as a state machine
(``SCHEDULED → INITIAL_INDEXING → ACTIVE ↔ PAUSED / DELETING / INVALID``).
F57 prevents ad-hoc ``UPDATE topology_cc_pairs SET status = ?`` writes
scattered across the codebase that bypass the transition matrix. Every
status mutation must go through a centralised validator that consults a
declared transition table.

Acceptable shape:

  _ALLOWED_TRANSITIONS: dict[CCPairStatus, frozenset[CCPairStatus]] = {
      CCPairStatus.SCHEDULED: frozenset({CCPairStatus.INITIAL_INDEXING, ...}),
      ...
  }

  def transition_cc_pair(db, cc_pair_id: int, new_status: CCPairStatus) -> None:
      current = _load_current_status(db, cc_pair_id)
      if new_status not in _ALLOWED_TRANSITIONS[current]:
          raise IllegalTransition(current, new_status)
      db.execute("UPDATE topology_cc_pairs SET status = ? WHERE id = ?", ...)

Detection (AST + string scan):

  1. Walk every ``kairix/**/*.py`` file.
  2. Find any string literal containing the substring
     ``UPDATE topology_cc_pairs`` followed by ``SET status``.
  3. For each match, walk the enclosing module: a file passes when the
     same module defines ``_ALLOWED_TRANSITIONS`` (the transition matrix
     dispatch) as a module-level attribute. Otherwise the file is
     flagged — the UPDATE is bypassing the validator.

  Files that lack any matching SQL string literal are out-of-scope for
  F57. The check is vacuous-green pre-Wave-C because no production code
  references ``topology_cc_pairs`` yet (confirmed by grep at landing).

No grandfathering — every violation fails. Wave C cc_pair
lifecycle code WILL trip F57 if it doesn't centralise transitions.

Per F21, ``REMEDIATION`` carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import REPO_ROOT

# Regex matches "UPDATE topology_cc_pairs ... SET status" in a string
# literal — order-tolerant on whitespace and case. The cc_pair table is
# the table whose lifecycle ADR v2 defines as a state machine; other
# topology_* tables (groups, hierarchy, etc.) are out of scope.
_UPDATE_STATUS_RE = re.compile(
    r"UPDATE\s+topology_cc_pairs\b[\s\S]*?SET\s+[\s\S]*?\bstatus\b",
    re.IGNORECASE,
)

# Name of the module-level dispatch attribute that signals a file
# routes UPDATEs through a centralised validator.
_TRANSITION_MATRIX_NAME = "_ALLOWED_TRANSITIONS"

REMEDIATION = """F57: a SQL UPDATE on topology_cc_pairs.status was found in a module
that does not declare an _ALLOWED_TRANSITIONS dispatch matrix.

ADR v2 §3 defines cc_pair.status as a state machine
(SCHEDULED → INITIAL_INDEXING → ACTIVE ↔ PAUSED / DELETING / INVALID).
Ad-hoc UPDATE ... SET status = ? bypasses the transition matrix and lets
illegal jumps land (e.g. SCHEDULED → DELETING straight, skipping
INITIAL_INDEXING). The data drifts silently.

fix: extract the UPDATE into a centralised transition_cc_pair() helper
     that consults a module-level _ALLOWED_TRANSITIONS dispatch dict.
     Every status mutation goes through that helper. Helper raises
     IllegalTransition when a jump isn't in the matrix.
next: see docs/architecture/connector-scope-topology/ADR.md §3 (cc_pair
     lifecycle) + 10-test-architecture.md §"New F-rules required" (F57).
run: python3 scripts/checks/check_f57_ccpair_lifecycle_integrity.py

Pass example:
  # kairix/core/connectors/cc_pair.py
  from kairix.core.protocols import CCPairStatus

  _ALLOWED_TRANSITIONS: dict[CCPairStatus, frozenset[CCPairStatus]] = {
      CCPairStatus.SCHEDULED: frozenset({CCPairStatus.INITIAL_INDEXING, CCPairStatus.DELETING}),
      CCPairStatus.INITIAL_INDEXING: frozenset({CCPairStatus.ACTIVE, CCPairStatus.INVALID}),
      CCPairStatus.ACTIVE: frozenset({CCPairStatus.PAUSED, CCPairStatus.DELETING, CCPairStatus.INVALID}),
      CCPairStatus.PAUSED: frozenset({CCPairStatus.ACTIVE, CCPairStatus.DELETING}),
      CCPairStatus.DELETING: frozenset(),
      CCPairStatus.INVALID: frozenset({CCPairStatus.DELETING}),
  }

  def transition_cc_pair(db, cc_pair_id, new_status):
      current = _load_current_status(db, cc_pair_id)
      if new_status not in _ALLOWED_TRANSITIONS[current]:
          raise IllegalTransition(current, new_status)
      db.execute("UPDATE topology_cc_pairs SET status = ? WHERE id = ?",
                 (new_status.value, cc_pair_id))

Forbidden example:
  # kairix/worker.py — F57 fires
  db.execute("UPDATE topology_cc_pairs SET status = 'ACTIVE' WHERE id = ?", (pair_id,))
  # No _ALLOWED_TRANSITIONS dispatch in this module; jump is unvalidated."""


def _module_declares_transition_matrix(tree: ast.Module) -> bool:
    """True if the module defines a top-level ``_ALLOWED_TRANSITIONS``
    assignment (annotated or bare).

    Bare ``ast.Assign``: ``_ALLOWED_TRANSITIONS = {...}``.
    Annotated ``ast.AnnAssign``: ``_ALLOWED_TRANSITIONS: dict[...] = {...}``.
    """
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == _TRANSITION_MATRIX_NAME:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == _TRANSITION_MATRIX_NAME:
                    return True
    return False


def _file_has_unguarded_status_update(path: Path) -> bool:
    """True if ``path`` contains a SQL UPDATE on topology_cc_pairs.status
    but does NOT declare ``_ALLOWED_TRANSITIONS``.

    Scans every string-literal constant in the AST for the SQL pattern
    (per-source-line regex, fragments allowed). Counts a hit only when
    the same module lacks the dispatch matrix.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    # Cheap pre-filter: if the regex doesn't match the raw source, skip
    # the AST parse cost.
    if not _UPDATE_STATUS_RE.search(source):
        return False

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False

    # Confirm the match is inside a string literal (not a comment).
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _UPDATE_STATUS_RE.search(node.value):
                # Found a real string literal containing the UPDATE.
                # Module-level dispatch matrix exempts it.
                return not _module_declares_transition_matrix(tree)
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every .py file under ``repo_root/kairix/`` and return
    repo-relative paths that issue a status UPDATE without a declared
    transition matrix.
    """
    kairix_dir = repo_root / "kairix"
    if not kairix_dir.exists():
        return set()

    violations: set[Path] = set()
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if _file_has_unguarded_status_update(path):
            try:
                violations.add(path.resolve().relative_to(repo_root))
            except ValueError:
                continue
    return violations


class F57(FitnessRule):
    """F57 as a FitnessRule subclass — see module docstring."""

    name = "f57"
    remediation = REMEDIATION
    roots = ("kairix",)

    def file_has_violation(self, path: Path) -> bool:
        return _file_has_unguarded_status_update(path)


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 on net-new violations."""
    return F57().run()


if __name__ == "__main__":
    sys.exit(main())
