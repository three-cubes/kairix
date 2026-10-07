"""F69: integration tests that walk a table / connector stream need a 10⁴-row variant.

Motivation (ADR-024 Bundle D)
-----------------------------
Bug 3 (``MaintenanceScheduler._prune_orphans``) shipped an unbounded
``LEFT JOIN ... fetchall()`` over ``content_vectors x documents``. At
N=10 fixture scale the join was instantaneous; at production scale
(989k chunks x 2.1M vectors) it saturated disk I/O on every tick. Every
existing integration test for the maintenance scheduler passed because
no test seeded ≥10⁴ rows. The defect class was invisible to the test
pyramid.

F69 is the mechanical rule. For every ``test_*`` function under
``tests/integration/`` whose body contains EITHER:

  * A ``.fetchall()`` call (i.e. eagerly materialises a SELECT over a
    kairix table — the Bug 3 anti-pattern), OR
  * A ``for ... in <expr>.list_changes(...)`` loop (i.e. iterates a
    connector stream — the cursor / per-tick budget anti-pattern that
    motivated F66),

at least ONE variant of the test (the test itself, or a sibling test
in the same module sharing a parametrize family) must drive ≥ 10_000
rows / events through the pattern. The check accepts three shapes:

  1. A ``@pytest.mark.parametrize`` on the test function whose values
     include an integer ≥ 10_000.
  2. A module-level constant assignment named ``_N`` / ``_ORPHAN_COUNT``
     / ``N_ROWS`` / ``SCALE_N`` / etc. with a value ≥ 10_000, referenced
     inside the test body.
  3. A call inside the test body to one of the canonical bulk-seed
     helpers from :mod:`tests.fakes` (``build_bulk_source_connector``,
     ``seed_bulk_entity_signals``, ``seed_bulk_content_rows``) with an
     ``n_events=`` / ``n_rows=`` kwarg whose literal value is ≥ 10_000,
     OR with no override (the defaults are 10_000).

Exemption: tests whose behaviour genuinely does not change with scale
(e.g. a single-row fetchall that's testing a NULL-handling branch) may
carry a ``# F69-small-scale-only: <rationale>`` comment on the def line
OR within the first 5 lines of the function body. Use sparingly — the
Bug 3 anti-pattern is exactly "this test looks like a happy-path read,
no scale needed" and we want pushback at review time.

Detection
---------
1. Walk every ``tests/integration/**/*.py``; collect every ``test_*``
   FunctionDef (top-level and inside test classes).
2. For each test, scan its body AST for ``.fetchall()`` Call nodes
   and ``for ... in <expr>.list_changes(...)`` loops. Skip tests that
   carry the small-scale exemption comment.
3. For qualifying tests, look for a scale variant in three shapes
   (parametrize ≥ 10_000 / module-constant ≥ 10_000 referenced in
   body / bulk-seed helper call). Pass if any shape is satisfied.
4. Violations are reported as
   ``tests/integration/<file>.py::<test_function_name>`` so each
   distinct test surfaces as its own violation.

There is no grandfathering: every violation fails the gate.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

INTEGRATION_ROOT = Path("tests") / "integration"

# Scale threshold from ADR-024 §F69 — Bug 3 surfaced at ~10⁵ rows but
# 10⁴ is enough to expose the unbounded-fetchall family. Higher than
# that is welcome; lower than that is not.
_SCALE_THRESHOLD = 10_000

# Exemption marker. Sits on the def line OR within the first
# _EXEMPT_BODY_LOOKAHEAD_LINES lines of the body. Operators reach for
# this only when behaviour genuinely doesn't change with scale.
_EXEMPT_RE = re.compile(r"#\s*F69-small-scale-only:\s*\S+")
_EXEMPT_BODY_LOOKAHEAD_LINES = 5

# Canonical bulk-seed helpers (tests/fakes.py, Bundle F). When a test
# calls one of these with no override the default n_events / n_rows is
# already 10_000, so the call satisfies F69 without needing an explicit
# kwarg. With an explicit kwarg, we require the value to be ≥ threshold.
_BULK_SEED_HELPERS: frozenset[str] = frozenset(
    {
        "build_bulk_source_connector",
        "seed_bulk_entity_signals",
        "seed_bulk_content_rows",
    }
)
_BULK_SEED_SIZE_KWARGS: frozenset[str] = frozenset({"n_events", "n_rows"})

REMEDIATION = """F69: integration test <file>::<test_name> iterates over a kairix
table (``.fetchall()``) or a connector stream
(``for ... in connector.list_changes(...)``) but no variant of the test
seeds ≥ 10_000 rows. The Bug 3 / GH #335 failure class — an unbounded
read that's instantaneous at N=10 fixture scale and saturates disk I/O
at N=10⁵ production scale — is invisible to fixture-only integration
tests. F69 (ADR-024 Bundle D) requires every such test to include a
production-scale variant.

fix: add a scale variant. Three shapes are accepted:

    # 1. Parametrize over N — fixture variant + scale variant:
    @pytest.mark.integration
    @pytest.mark.parametrize("n_rows", [100, 10_000])
    def test_prune_orphans_bounded(tmp_path: Path, n_rows: int) -> None:
        _seed_orphan_rows(tmp_path, n=n_rows)
        result = scheduler.tick()
        assert result.pruned <= 1000  # F66 budget cap holds at any scale

    # 2. Reuse a canonical bulk-seed helper from tests/fakes.py
    #    (defaults are already 10_000 — no override needed for the
    #    soak variant; pass per_tick_max_items if testing multi-tick):
    from tests.fakes import build_bulk_source_connector
    @pytest.mark.integration
    def test_pipeline_drains_at_scale(tmp_path: Path) -> None:
        connector = build_bulk_source_connector(n_events=10_000)
        pipeline = factory.build_connector_pipeline(db=_open_db(tmp_path), collection="x")
        result = pipeline.run_batch(connector, FakeExtractor())
        assert result.processed == 10_000

    # 3. Module-level constant referenced inside the test body
    #    (existing tests/integration/test_maintenance_scale_bound.py
    #    pattern):
    _ORPHAN_COUNT = 10_000
    @pytest.mark.integration
    def test_prune_orphans_finishes_within_budget(tmp_path: Path) -> None:
        _seed_orphan_db(tmp_path / "k.sqlite", n_orphans=_ORPHAN_COUNT)
        ...

next: re-run python3 scripts/checks/check_f69_scale_bound_tests.py
run: bash scripts/safe-commit.sh "test(integration): add 10k-row variant to <test_name>"

Pass example: tests/integration/test_maintenance_scale_bound.py

    _ORPHAN_COUNT = 10_000

    @pytest.mark.integration
    def test_prune_orphans_finishes_within_time_budget_at_10k_rows(tmp_path: Path) -> None:
        _seed_orphan_db(tmp_path / "kairix.sqlite", n_orphans=_ORPHAN_COUNT)
        # ... scheduler.tick() ... wall-clock assertion ...

    # The module-level _ORPHAN_COUNT = 10_000 referenced inside the
    # test body satisfies F69 — every call site of _ORPHAN_COUNT in
    # the body proves the test runs at production scale.

Forbidden example: tests/integration/test_collections.py (the anti-pattern
F69 stops new tests from copying):

    def test_multi_collection_scans_separately(self, multi_collection_dirs):
        # ... scanner.scan(collections) ...  # only 4 docs seeded
        rows = db.execute("SELECT DISTINCT collection FROM documents").fetchall()
        # ^^^ .fetchall() at fixture scale; would have missed the Bug 3
        # "unbounded fetchall at production scale" failure mode entirely.

Allowed exemption (rare — behaviour genuinely doesn't change with scale):

    # F69-small-scale-only: NULL-handling branch fires on row 1 regardless of N
    def test_null_collection_is_normalised(db):
        rows = db.execute("SELECT collection FROM documents WHERE id = 1").fetchall()
        assert rows[0][0] == ""
"""


# ---------------------------------------------------------------------------
# AST harvesting helpers
# ---------------------------------------------------------------------------


def _iter_test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """Return every ``test_*`` FunctionDef in the module (top-level + class-nested).

    Walks the full AST so test methods inside a ``TestX`` class are
    included alongside top-level test functions.
    """
    out: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            out.append(node)
    return out


def _harvest_module_constants(tree: ast.Module) -> dict[str, int]:
    """Return ``{name: int_value}`` for every module-level ``X = <int>`` assignment.

    Only constants whose value is a plain ``int`` literal (or a unary
    minus over an int) are included; expressions like ``10 * 1000`` are
    skipped to keep the check predictable. Underscored-numeric literals
    (``10_000``) are normal Python ints so they're picked up.
    """
    out: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, int):
            # Allow ``-N`` unary form for completeness; F69 cares about
            # positive thresholds though, so a negative constant won't
            # match the >= 10_000 check downstream.
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = int(node.value.value)
    return out


# ---------------------------------------------------------------------------
# Pattern detection — does this test trigger F69?
# ---------------------------------------------------------------------------


def _body_contains_fetchall(fn: ast.FunctionDef) -> bool:
    """Return True if the function body contains a ``.fetchall()`` call.

    Matches the Call node ``<anything>.fetchall()`` — the receiver can
    be a sqlite cursor, a SQLAlchemy result, etc. The semantic F69
    cares about is "this test eagerly materialises a SELECT result";
    that's the Bug 3 anti-pattern shape regardless of the receiver.
    """
    for child in ast.walk(fn):
        if not isinstance(child, ast.Call):
            continue
        called = child.func
        if isinstance(called, ast.Attribute) and called.attr == "fetchall":
            return True
    return False


def _body_contains_list_changes_iteration(fn: ast.FunctionDef) -> bool:
    """Return True if the function body iterates over ``<expr>.list_changes(...)``.

    Matches ``for ... in <expr>.list_changes(...)`` and also the
    equivalent comprehension form. The receiver expression is
    intentionally not constrained — connectors are constructed via the
    factory in F47-compliant tests and the resulting bound name varies.
    """
    for child in ast.walk(fn):
        iter_target: ast.expr | None = None
        if isinstance(child, ast.For):
            iter_target = child.iter
        elif isinstance(child, ast.AsyncFor):
            iter_target = child.iter
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for gen in child.generators:
                if _is_list_changes_call(gen.iter):
                    return True
            continue
        if iter_target is not None and _is_list_changes_call(iter_target):
            return True
    return False


def _is_list_changes_call(node: ast.expr) -> bool:
    """Return True if ``node`` is a call to a method named ``list_changes``."""
    if not isinstance(node, ast.Call):
        return False
    called = node.func
    return isinstance(called, ast.Attribute) and called.attr == "list_changes"


def _triggers_f69(fn: ast.FunctionDef) -> bool:
    """Return True if the test's body matches an F69-relevant iteration pattern."""
    return _body_contains_fetchall(fn) or _body_contains_list_changes_iteration(fn)


# ---------------------------------------------------------------------------
# Pass-checks — does this test (or a sibling) include a 10⁴-row variant?
# ---------------------------------------------------------------------------


def _parametrize_has_scale_value(fn: ast.FunctionDef) -> bool:
    """Return True if any ``@pytest.mark.parametrize`` decorator carries an int ≥ threshold.

    Walks every literal in the second argument of every parametrize
    decorator on the function. The first arg is the parameter-name
    string; the second is the list/tuple of values. Tuples of params
    (multi-param parametrize) are walked too so ``[("x", 100), ("x",
    10_000)]`` is detected the same as ``[100, 10_000]``.
    """
    for deco in fn.decorator_list:
        call = deco if isinstance(deco, ast.Call) else None
        if call is None:
            continue
        called = call.func
        # Match ``pytest.mark.parametrize`` and ``mark.parametrize`` /
        # any other Attribute chain ending in ``parametrize``.
        is_parametrize = isinstance(called, ast.Attribute) and called.attr == "parametrize"
        if not is_parametrize:
            continue
        if len(call.args) < 2:
            continue
        if _any_int_at_threshold(call.args[1]):
            return True
    return False


def _any_int_at_threshold(node: ast.AST) -> bool:
    """Return True if ``node`` (or any nested literal) is an int ≥ threshold."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, int):
            if child.value >= _SCALE_THRESHOLD:
                return True
    return False


def _calls_bulk_seed_helper_at_scale(fn: ast.FunctionDef) -> bool:
    """Return True if the test calls a bulk-seed helper with N ≥ threshold (or default).

    A helper call with no ``n_events`` / ``n_rows`` kwarg passes — the
    canonical helpers default to 10_000. A helper call with an
    explicit kwarg passes only when the literal value is ≥ threshold.
    A kwarg with a non-literal value (variable reference, expression)
    is treated conservatively as "satisfies F69" — these are most
    commonly module-level constants or parametrize indirections that
    the body / decorator scan already accounts for, and false-negatives
    are safer than false-positives here.
    """
    for child in ast.walk(fn):
        if not isinstance(child, ast.Call):
            continue
        called = child.func
        helper_name: str | None = None
        if isinstance(called, ast.Name):
            helper_name = called.id
        elif isinstance(called, ast.Attribute):
            helper_name = called.attr
        if helper_name not in _BULK_SEED_HELPERS:
            continue
        if _bulk_call_passes_threshold(child):
            return True
    return False


def _bulk_call_passes_threshold(call: ast.Call) -> bool:
    """Per-call check for a bulk-seed helper invocation.

    Defaults pass (no kwarg provided → helper uses its built-in
    10_000 default). Explicit literal kwargs must be ≥ threshold.
    Explicit non-literal kwargs (variable / expression) are treated
    as satisfying — see the call-site rationale.
    """
    found_size_kwarg = False
    for kw in call.keywords:
        if kw.arg not in _BULK_SEED_SIZE_KWARGS:
            continue
        found_size_kwarg = True
        value = kw.value
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            if value.value >= _SCALE_THRESHOLD:
                return True
            continue
        # Non-literal kwarg value — treat as satisfying. The detector
        # can't statically know the value; the alternative is
        # cascading false-positives on every indirect reference.
        return True
    # No size kwarg → helper default (10_000) is used → F69-satisfied.
    if not found_size_kwarg:
        return True
    return False


def _references_scale_constant(
    fn: ast.FunctionDef,
    module_constants: dict[str, int],
) -> bool:
    """Return True if the test body references a module-level constant ≥ threshold.

    Catches the canonical ``_ORPHAN_COUNT = 10_000`` /
    ``test_prune_orphans_at_10k_rows(_seed(..., n_orphans=_ORPHAN_COUNT))``
    shape already present in tests/integration/test_maintenance_scale_bound.py.
    """
    scale_names = {name for name, value in module_constants.items() if value >= _SCALE_THRESHOLD}
    if not scale_names:
        return False
    for child in ast.walk(fn):
        if isinstance(child, ast.Name) and child.id in scale_names:
            return True
    return False


def _has_scale_variant(
    fn: ast.FunctionDef,
    module_constants: dict[str, int],
) -> bool:
    """Return True if any of the three accepted scale shapes is satisfied."""
    if _parametrize_has_scale_value(fn):
        return True
    if _calls_bulk_seed_helper_at_scale(fn):
        return True
    if _references_scale_constant(fn, module_constants):
        return True
    return False


# ---------------------------------------------------------------------------
# Exemption handling
# ---------------------------------------------------------------------------


def _has_small_scale_exempt(fn: ast.FunctionDef, source_lines: list[str]) -> bool:
    """Return True if the def line OR the first ``_EXEMPT_BODY_LOOKAHEAD_LINES``
    body lines carry the ``# F69-small-scale-only:`` rationale.

    The def line is the ``lineno`` line (1-indexed); body lines start
    at ``lineno + 1``. We don't walk the contiguous comment block above
    the def (unlike F71's approach) because pytest discovers tests by
    name from the module — a comment above the def is too far from the
    discovered test, and reviewers might miss it during paydown.
    """
    if fn.lineno < 1 or fn.lineno > len(source_lines):
        return False
    if _EXEMPT_RE.search(source_lines[fn.lineno - 1]):
        return True
    body_start = fn.lineno  # zero-indexed start of body in source_lines
    body_end = min(len(source_lines), body_start + _EXEMPT_BODY_LOOKAHEAD_LINES)
    for line in source_lines[body_start:body_end]:
        if _EXEMPT_RE.search(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-file harvest + collect
# ---------------------------------------------------------------------------


def _harvest_file_violations(path: Path, repo_root: Path) -> set[Path]:
    """Return the set of F69 violation keys for one test file."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    source_lines = source.splitlines()
    module_constants = _harvest_module_constants(tree)
    rel = path.resolve().relative_to(repo_root)
    violations: set[Path] = set()
    for fn in _iter_test_functions(tree):
        if not _triggers_f69(fn):
            continue
        if _has_small_scale_exempt(fn, source_lines):
            continue
        if _has_scale_variant(fn, module_constants):
            continue
        violations.add(Path(f"{rel}::{fn.name}"))
    return violations


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every integration test; flag any triggering F69 with no scale variant.

    Returns repo-relative synthetic paths of the form
    ``tests/integration/<file>.py::<test_function_name>`` so each
    distinct test surfaces as its own violation.
    """
    integration_dir = repo_root / INTEGRATION_ROOT
    if not integration_dir.exists():
        return set()
    violations: set[Path] = set()
    for path in sorted(integration_dir.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        violations |= _harvest_file_violations(path, repo_root)
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f69-scale-bound-tests", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
