"""F71: every preflight check that reports a count has a paired truthfulness test.

Motivation (GH #334, ADR-024 Bundle C)
--------------------------------------
The historical ``_check_entity_signals_staging_not_stuck`` ran a
``SELECT ... LIMIT 1000`` and reported ``count = len(rows)``. A 2.3M-row
backlog read out as ``count = 1000`` and operators saw "small enough to
ignore". The fix (commit landed earlier this session) switched to
``COUNT(*)``, but no mechanical rule prevented the same pattern from
recurring in any other preflight.

F71 is the mechanical rule. For every function in
``kairix/core/db/integrity.py`` whose name matches ``_check_*`` AND
whose return type annotation is ``IntegrityGap | None`` AND whose body
constructs an ``IntegrityGap(... count=<expression> ...)``, require a
paired contract test in ``tests/contracts/test_integrity_truthfulness.py``
named ``test_<check_function_name>_count_equals_ground_truth``.

The paired test must seed N rows matching the predicate the preflight
uses internally (N >= 1500 — generously larger than any historical
LIMIT cap so under-reporting bugs surface as concrete count mismatches),
then assert ``gap.count == db.execute("SELECT COUNT(*) FROM <table> "
"WHERE <same predicate>").fetchone()[0]``.

Exemption: a preflight that legitimately cannot derive a SQL ground
truth (e.g. counts external state like a Neo4j node count or a usearch
index length) may carry a ``# F71-truthfulness-exempt: <rationale>``
comment on the function definition line. Use rarely — only when the
"count" field genuinely cannot be reconciled with a SQL aggregate.

Detection
---------
1. Parse ``kairix/core/db/integrity.py`` with ``ast``.
2. For every top-level ``FunctionDef`` whose name starts with ``_check_``
   and whose ``returns`` annotation textually contains ``IntegrityGap``,
   look at the function body for an ``IntegrityGap(...)`` constructor
   that includes a ``count=`` keyword argument.
3. Skip any function whose source text (on the def line, in the
   contiguous comment block immediately above the def, or anywhere
   inside the function body) carries ``# F71-truthfulness-exempt:``.
4. Require ``tests/contracts/test_integrity_truthfulness.py`` to define
   a function named ``test_<check_function_name>_count_equals_ground_truth``.

Violations are reported with a synthetic path key
``kairix/core/db/integrity.py::<check_function_name>`` so the failure
output lists "what's broken" in human-readable form.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

INTEGRITY_PATH = Path("kairix") / "core" / "db" / "integrity.py"
TRUTHFULNESS_TEST_PATH = Path("tests") / "contracts" / "test_integrity_truthfulness.py"

_EXEMPT_RE = re.compile(r"#\s*F71-truthfulness-exempt:\s*\S+")

REMEDIATION = """F71: preflight check <function_name> reports a count without a
truthfulness contract test — the GH #334 LIMIT-1000 masking failure mode
could silently recur.

fix: add the paired test in tests/contracts/test_integrity_truthfulness.py:

    def test_<function_name>_count_equals_ground_truth() -> None:
        db = _make_db()
        try:
            # Seed >= 1500 rows matching the predicate the preflight reads
            # (bigger than any historical LIMIT cap to catch masking bugs).
            _seed_n_rows_matching(db, n=1500)
            gap = <function_name>(db)
            ground_truth = db.execute(
                "SELECT COUNT(*) FROM <table> WHERE <same predicate>"
            ).fetchone()[0]
        finally:
            db.close()
        assert gap is not None, "preflight must surface a gap when predicate matches"
        assert gap.count == ground_truth, (
            f"F71: preflight reported {gap.count}; "
            f"SELECT COUNT(*) reports {ground_truth} for the same predicate. "
            f"The preflight is hiding the true scale — see GH #334."
        )

next: re-run python3 scripts/checks/check_f71_preflight_truthfulness.py
run: bash scripts/safe-commit.sh "test(contracts): add truthfulness contract for <function_name>"

Pass example: kairix/core/db/integrity.py

    def _check_entity_signals_staging_not_stuck(db) -> IntegrityGap | None:
        # COUNT(*) reads the true backlog; LIMIT only bounds the sample.
        total = db.execute(
            "SELECT COUNT(*) FROM entity_signals "
            "WHERE pushed_to_neo4j = 0 AND modified_at < ?",
            (cutoff,),
        ).fetchone()[0]
        sample = db.execute(
            "SELECT id, kind, value FROM entity_signals "
            "WHERE pushed_to_neo4j = 0 AND modified_at < ? LIMIT ?",
            (cutoff, _MAX_SAMPLE),
        ).fetchall()
        return IntegrityGap(invariant=..., count=int(total), sample=..., remediation=...)

    # Paired truthfulness test in tests/contracts/test_integrity_truthfulness.py:
    def test__check_entity_signals_staging_not_stuck_count_equals_ground_truth():
        # seeds 1500 stuck rows; asserts gap.count == 1500 (matches COUNT(*)).

Forbidden example: the GH #334 anti-pattern

    def _check_entity_signals_staging_not_stuck(db) -> IntegrityGap | None:
        # WRONG: count comes from len(rows) where SELECT has LIMIT 1000.
        # A 2.3M-row backlog reads out as count=1000; operators ignore it.
        rows = db.execute(
            "SELECT id, kind, value FROM entity_signals "
            "WHERE pushed_to_neo4j = 0 AND modified_at < ? LIMIT 1000",
            (cutoff,),
        ).fetchall()
        return IntegrityGap(invariant=..., count=len(rows), ...)

Allowed exemption (rare — count cannot be reconciled with SQL):

    # F71-truthfulness-exempt: counts usearch index length, not a SQL aggregate
    def _check_vector_store_vs_content_vectors(db, vector_store_loader) -> IntegrityGap | None:
        ...
"""


def _has_truthfulness_exempt(node: ast.FunctionDef, source: str) -> bool:
    """Return True if the function's source span carries F71-truthfulness-exempt.

    Inspects (in order): the contiguous comment block directly above the
    ``def`` line (walking upward until a non-comment / blank-line break),
    the ``def`` line itself, and any line inside the function body. The
    historical convention from F62/F66/F67 is "comment on or directly
    above the def"; we also accept inside-body so a long preflight can
    document the exemption next to the specific construct that drove it.
    """
    lines = source.splitlines()
    # Walk upward from the line above the def, accumulating contiguous
    # comment-only lines (a blank line or a non-comment line breaks the
    # block). This lets a multi-line rationale ``# F71-... : ...`` /
    # ``# continuation`` sit immediately above the def and still count.
    idx = node.lineno - 2  # zero-based index of the line directly above def
    while idx >= 0:
        line = lines[idx].strip()
        if not line:
            break
        if not line.startswith("#"):
            break
        if _EXEMPT_RE.search(line):
            return True
        idx -= 1
    def_line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
    if _EXEMPT_RE.search(def_line):
        return True
    end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
    for line in lines[node.lineno : end_line]:
        if _EXEMPT_RE.search(line):
            return True
    return False


def _returns_integrity_gap_or_none(node: ast.FunctionDef) -> bool:
    """Return True if the return annotation textually contains IntegrityGap.

    Accepts ``IntegrityGap | None``, ``Optional[IntegrityGap]``, and the
    bare ``IntegrityGap`` shape — all three carry the same semantic
    "this preflight returns a gap or signals no-gap".
    """
    if node.returns is None:
        return False
    try:
        text = ast.unparse(node.returns)
    except Exception:
        return False
    return "IntegrityGap" in text


def _constructs_gap_with_count(node: ast.FunctionDef) -> bool:
    """Return True if any IntegrityGap(...) call in the body sets count=."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        called = child.func
        called_name = None
        if isinstance(called, ast.Name):
            called_name = called.id
        elif isinstance(called, ast.Attribute):
            called_name = called.attr
        if called_name != "IntegrityGap":
            continue
        for kw in child.keywords:
            if kw.arg == "count":
                return True
    return False


def _harvest_preflight_check_functions(source: str) -> list[str]:
    """Return names of every ``_check_*`` preflight that should pair with F71.

    A function qualifies when it (a) is module-level, (b) starts with
    ``_check_``, (c) declares a return type containing ``IntegrityGap``,
    (d) constructs ``IntegrityGap(... count=...)`` somewhere in its body,
    and (e) does NOT carry an F71-truthfulness-exempt rationale.
    """
    tree = ast.parse(source)
    out: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("_check_"):
            continue
        if not _returns_integrity_gap_or_none(node):
            continue
        if not _constructs_gap_with_count(node):
            continue
        if _has_truthfulness_exempt(node, source):
            continue
        out.append(node.name)
    return out


def _harvest_truthfulness_test_names(source: str) -> set[str]:
    """Return the set of test function names defined in the truthfulness file.

    We only need names; signature / body / decorators are irrelevant for
    the F71 mechanical check.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every preflight check; flag any with no matching truthfulness test."""
    integrity_file = repo_root / INTEGRITY_PATH
    if not integrity_file.exists():
        return set()
    integrity_source = integrity_file.read_text(encoding="utf-8")
    check_names = _harvest_preflight_check_functions(integrity_source)
    if not check_names:
        return set()

    test_file = repo_root / TRUTHFULNESS_TEST_PATH
    if not test_file.exists():
        # Every preflight is a violation when the truthfulness file is missing.
        return {Path(f"{INTEGRITY_PATH}::{name}") for name in check_names}
    test_source = test_file.read_text(encoding="utf-8")
    test_names = _harvest_truthfulness_test_names(test_source)

    violations: set[Path] = set()
    for check_name in check_names:
        expected = f"test_{check_name}_count_equals_ground_truth"
        if expected not in test_names:
            violations.add(Path(f"{INTEGRITY_PATH}::{check_name}"))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f71-preflight-truthfulness", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
