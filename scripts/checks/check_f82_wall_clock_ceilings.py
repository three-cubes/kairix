"""F82: wall-clock ceiling assertions are banned outside the soak/probe tiers.

Motivation (EPIC #499 Phase 0; the #493 flake family)
-----------------------------------------------------
``assert elapsed < 0.150`` measures the host scheduler, not kairix
behaviour. On a loaded CI runner (or a laptop running two safe-commit
gates concurrently) the same code path legitimately takes 4x longer,
the assertion fires, and a green change burns a full gate cycle on a
re-run. The #493 family burned three gate cycles in one day. Timing
budgets are real requirements — but they belong in the tiers built for
them (``soak`` nightly, ``pvt``/``load`` probes), not in the
per-commit unit/integration path.

What F82 flags
--------------
An ``assert`` whose test is a single comparison of an *elapsed-time
expression* against a *numeric literal ceiling*:

  * ``assert <elapsed> < 2.0`` / ``assert <elapsed> <= 150``
  * ``assert 2.0 > <elapsed>`` / ``assert 150 >= <elapsed>``

where ``<elapsed>`` is one of:

  * a difference of clock calls — ``time.time() - start``,
    ``time.monotonic() - t0``, ``time.perf_counter() - began`` (clock
    call on either side; arithmetic wrappers like ``(... ) * 1000``
    are seen through);
  * a variable assigned (in the same test function) from such a
    difference, or from a difference of variables that were themselves
    assigned from clock calls;
  * any name or attribute matching the elapsed convention —
    ``elapsed``, ``elapsed_ms``, ``elapsed_seconds``, ``.elapsed``.

Exemptions
----------
  * The test function / its class / its module carries a ``slow``,
    ``soak``, ``load``, ``pvt``, or ``benchmark`` marker — resolved
    with the same mechanism F8's marker check uses (function
    decorator, class-level ``pytestmark`` or marker decorator,
    module-level ``pytestmark``).
  * Any line of the assert statement carries ``# F82-allowed: <why>``.

Intentionally NOT caught (precision over recall — a detector agents
distrust is worse than no detector):

  * **Floors** (``assert elapsed >= 0.080``) — "waited at least the
    window" assertions are slow-host-immune; only ceilings flake.
  * Ceilings compared against a *variable* budget
    (``assert elapsed < budget_ms``) — too many legitimate
    deterministic shapes (fake clocks, injected budgets).
  * Elapsed expressions built across helper functions (the helper
    returns the delta, the variable isn't elapsed-named) — only
    same-function assignment tracking plus the naming convention.
  * Asserts inside non-test helpers, ``pytest.approx`` shapes,
    ``unittest`` ``assertLess`` calls, and chained comparisons
    (``assert 0 < elapsed < 2``) — rare in this tree; revisit if one
    of these ships a flake.
  * Fake/frozen clocks: a test driving a ``FakeClock`` is
    deterministic, but the detector cannot see that — use
    ``# F82-allowed: fake clock, deterministic`` on the assert line.

There is no grandfathering: every unmarked wall-clock ceiling blocks at
pre-commit / safe-commit / CI Stage 0.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule

# Markers that move a test into a tier where wall-clock budgets are the
# point (soak nightly, probe/load/pvt harnesses) or are excluded from
# the per-commit path (slow). "benchmark" is reserved defensively for a
# future tier name. Resolution mechanism mirrors F8's check.
EXEMPT_MARKERS = frozenset({"slow", "soak", "load", "pvt", "benchmark"})

# time-module entry points whose call results behave as clock readings.
CLOCK_ATTRS = frozenset({"time", "monotonic", "perf_counter", "monotonic_ns", "perf_counter_ns"})

# Variable / attribute names that carry elapsed-time by convention.
ELAPSED_NAME_RE = re.compile(r"^elapsed(_|$)")

RATIONALE_TAG = "# F82-allowed:"

CEILING_LEFT_OPS = (ast.Lt, ast.LtE)  # elapsed < CONST / elapsed <= CONST
CEILING_RIGHT_OPS = (ast.Gt, ast.GtE)  # CONST > elapsed / CONST >= elapsed

REMEDIATION = """F82: wall-clock ceiling assertion in a per-commit tier — timing
measures host scheduling, not kairix behaviour.

fix: assert the deterministic outcome instead (what short-circuited,
what was called), or move the test to the soak tier with
@pytest.mark.slow. If the ceiling is genuinely deterministic (fake
clock, injected budget), add a trailing rationale on the assert line:
# F82-allowed: <why this cannot flake>.
next: see #493 for the flake family this prevents. Re-run
python3 scripts/checks/check_f82_wall_clock_ceilings.py to confirm
the gate goes green.
run: bash scripts/safe-commit.sh "test(<area>): replace wall-clock ceiling with outcome assertion"

Pass example:
  # Outcome-shaped: assert what the budget short-circuit DID, not how
  # long it took. Deterministic on any host.
  def test_budget_exhaustion_skips_rerank():
      pipeline = build_search_pipeline(paths=FakePaths(...), budget=FakeBudget(exhausted=True))
      result = pipeline.search("q")
      assert result.rerank_skipped is True
      assert result.stages_run == ["bm25"]

  # OR tier-shifted: the timing budget is the point — soak owns it.
  @pytest.mark.slow
  def test_dispatch_completes_within_budget():
      ...
      assert elapsed_ms < 150.0

Forbidden example:
  @pytest.mark.unit
  def test_dispatcher_is_fast():
      start = time.monotonic()
      dispatcher.route("q")
      elapsed = time.monotonic() - start
      assert elapsed < 0.150  # flakes on any loaded runner (#493)"""


def _is_clock_call(node: ast.expr) -> bool:
    """True iff ``node`` is a call of a time-module clock.

    Accepted shapes: ``time.monotonic()`` / ``time.time()`` /
    ``time.perf_counter()`` (attribute on any receiver named ``time``),
    and bare ``monotonic()`` / ``perf_counter()`` (the
    ``from time import monotonic`` form). Bare ``time()`` is included —
    in a test file that name is almost always ``from time import time``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in CLOCK_ATTRS and isinstance(func.value, ast.Name) and func.value.id == "time"
    if isinstance(func, ast.Name):
        return func.id in CLOCK_ATTRS
    return False


def _has_elapsed_name(node: ast.expr) -> bool:
    """True iff ``node`` is a Name/Attribute matching the elapsed convention."""
    if isinstance(node, ast.Name):
        return bool(ELAPSED_NAME_RE.match(node.id))
    if isinstance(node, ast.Attribute):
        return bool(ELAPSED_NAME_RE.match(node.attr))
    return False


def _contains_clock_difference(node: ast.expr, clock_vars: frozenset[str]) -> bool:
    """True iff the expression tree contains ``<clockish> - <clockish-or-name>``.

    ``clockish`` = a clock call or a Name previously assigned from one.
    Either side qualifying is enough (``time.monotonic() - start`` and
    ``end - t0`` both count when the named side is a known clock var).
    """

    def _clockish(side: ast.expr) -> bool:
        return _is_clock_call(side) or (isinstance(side, ast.Name) and side.id in clock_vars)

    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub) and (_clockish(sub.left) or _clockish(sub.right)):
            return True
    return False


def _is_elapsed_expression(node: ast.expr, clock_vars: frozenset[str], elapsed_vars: frozenset[str]) -> bool:
    """True iff ``node`` reads as an elapsed-time value (see module docstring)."""
    if _has_elapsed_name(node):
        return True
    if isinstance(node, ast.Name) and node.id in elapsed_vars:
        return True
    return _contains_clock_difference(node, clock_vars)


def _is_numeric_ceiling(node: ast.expr) -> bool:
    """True iff ``node`` is a numeric literal (the only ceiling shape flagged)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _harvest_time_vars(func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[frozenset[str], frozenset[str]]:
    """Single pass over the function body collecting clock-reading and
    elapsed-delta variable names (simple ``name = value`` targets only).
    """
    clock_vars: set[str] = set()
    elapsed_vars: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if _is_clock_call(node.value):
            clock_vars.add(target.id)
        elif _contains_clock_difference(node.value, frozenset(clock_vars)):
            elapsed_vars.add(target.id)
    return frozenset(clock_vars), frozenset(elapsed_vars)


def _assert_is_wall_clock_ceiling(node: ast.Assert, clock_vars: frozenset[str], elapsed_vars: frozenset[str]) -> bool:
    """True iff the assert compares an elapsed expression against a
    numeric ceiling (single comparator only — chained comparisons are
    out of scope by design).
    """
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left, op, right = test.left, test.ops[0], test.comparators[0]
    if isinstance(op, CEILING_LEFT_OPS):
        return _is_elapsed_expression(left, clock_vars, elapsed_vars) and _is_numeric_ceiling(right)
    if isinstance(op, CEILING_RIGHT_OPS):
        return _is_numeric_ceiling(left) and _is_elapsed_expression(right, clock_vars, elapsed_vars)
    return False


# ── F8-style marker resolution (function / class / module) ──────────────────


def _decorator_is_exempt_marker(node: ast.expr) -> bool:
    """``@pytest.mark.slow`` / ``@mark.soak`` / call forms thereof."""
    if isinstance(node, ast.Call):
        return _decorator_is_exempt_marker(node.func)
    if isinstance(node, ast.Attribute) and node.attr in EXEMPT_MARKERS:
        inner = node.value
        if isinstance(inner, ast.Attribute) and inner.attr == "mark":
            return True
        if isinstance(inner, ast.Name) and inner.id == "mark":
            return True
    return False


def _expression_carries_exempt_marker(node: ast.expr) -> bool:
    if _decorator_is_exempt_marker(node):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_expression_carries_exempt_marker(elt) for elt in node.elts)
    return False


def _scope_has_exempt_pytestmark(body: list[ast.stmt]) -> bool:
    """Module-or-class body assigns an exempt marker to ``pytestmark``."""
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if _expression_carries_exempt_marker(stmt.value):
                        return True
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "pytestmark":
                if stmt.value is not None and _expression_carries_exempt_marker(stmt.value):
                    return True
    return False


def _line_range_has_rationale(node: ast.stmt, source_lines: list[str]) -> bool:
    """True iff any physical line spanned by ``node`` carries the
    ``# F82-allowed:`` rationale tag.
    """
    start = node.lineno
    end = getattr(node, "end_lineno", None) or start
    for lineno in range(start, end + 1):
        if 0 < lineno <= len(source_lines) and RATIONALE_TAG in source_lines[lineno - 1]:
            return True
    return False


def _function_violates(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> bool:
    """True iff an un-exempt test function contains a flagged assert."""
    if not func.name.startswith("test_"):
        return False
    if any(_decorator_is_exempt_marker(d) for d in func.decorator_list):
        return False
    clock_vars, elapsed_vars = _harvest_time_vars(func)
    for node in ast.walk(func):
        if not isinstance(node, ast.Assert):
            continue
        if _line_range_has_rationale(node, source_lines):
            continue
        if _assert_is_wall_clock_ceiling(node, clock_vars, elapsed_vars):
            return True
    return False


def file_has_violation(path: Path) -> bool:
    """True iff any test function in ``path`` asserts a wall-clock
    ceiling without a tier marker or rationale.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False

    if _scope_has_exempt_pytestmark(tree.body):
        return False
    source_lines = source.splitlines()

    for top_level in tree.body:
        if isinstance(top_level, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _function_violates(top_level, source_lines):
                return True
        elif isinstance(top_level, ast.ClassDef):
            if _scope_has_exempt_pytestmark(top_level.body) or any(
                _decorator_is_exempt_marker(d) for d in top_level.decorator_list
            ):
                continue
            for member in top_level.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _function_violates(member, source_lines):
                        return True
    return False


class F82(FitnessRule):
    """F82 as a FitnessRule subclass — see module docstring."""

    name = "f82"
    remediation = REMEDIATION
    roots = ("tests",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_violation(path)


def main() -> int:
    return F82().run()


if __name__ == "__main__":
    sys.exit(main())
