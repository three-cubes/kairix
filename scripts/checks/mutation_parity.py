#!/usr/bin/env python3
"""Diff-scoped mutation runner — the mechanical sabotage control (#499 Phase 1).

Motivation (session-escape-6: sabotage proofs were cultural, not mechanical)
----------------------------------------------------------------------------
Surviving mutants slipped into ``done``-derivation and the
``OperatorTokenGuard`` exemption with every suite green, because the
sabotage proofs that should have pinned that logic were *executed once* by
a human (a cultural ritual) rather than enforced by a repeatable gate. A
mutant survives when the tests that cover a line still PASS after the
line's logic is changed — proof the tests assert presence, not behaviour.

This runner makes the sabotage proof mechanical and diff-scoped. It follows
the org's shared mutation contract: a homegrown mutator (no mutmut/stryker
dependency) and NO committed survivor list (PLA-472 retired the ratchet).
The per-commit leg is diff-scoped and hard-capped so it stays bounded; the
nightly ``mutation-suite.yml`` runs the same strict verdict at a wider
(since-last-release) scope.

What it does
------------
1. Derive touched function spans from ``git diff`` — staged by default, or
   against a ``--base`` ref. Only functions whose body lines changed are in
   scope (a docstring-only or signature-only edit yields no mutable span).
2. Generate mutants on the CHANGED lines within those spans:
   ``==``↔``!=``, ``<``↔``<=``, ``>``↔``>=``, ``and``↔``or``,
   ``True``↔``False``, drop-a-conjunct, negate-a-condition.
3. For each mutant, run only the IMPACTED tests — the test files that
   import the mutated module (the same import-graph heuristic
   ``safe-commit.sh --fast`` uses). A mutant whose impacted tests still
   PASS is a SURVIVOR (the gate's signal).

Hard caps (so a ~300-line diff finishes in ~2-3 min):
  * ``MAX_MUTANTS`` (20) total — excess mutants are reported as skipped,
    never silently dropped.
  * ``PER_MUTANT_TIMEOUT_S`` (60) per impacted-test run — a mutant that
    times out is reported as a partial (treated as KILLED: the tests did
    not pass cleanly, which is the conservative call — a survivor is only
    ever a clean pass).

Operator outcome (F21)
----------------------
Each survivor prints::

    mutant survived: <file>:<line> <original> -> <mutation> — the tests
    that cover this line pass with the logic changed.
    fix: add/strengthen an assertion that pins this behaviour.
    next: ...

Exit code is non-zero iff ANY survivor is found. There is no survivor
list that could excuse one — in both the diff-scoped safe-commit leg and
the nightly ``--base`` leg, every survivor fails the gate and is fixed at
source by strengthening the impacted test.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Hard caps — the whole point of diff-scoping. A normal commit must not
# make safe-commit slow or flaky; these bound the work regardless of diff
# size. See module docstring.
#
# Three independent budgets, ANY of which stops the run early:
#   * MAX_MUTANTS — never generate more than this many mutants.
#   * PER_MUTANT_TIMEOUT_S — kill (and treat as killed) a single
#     impacted-test run that exceeds this.
#   * TOTAL_BUDGET_S — once cumulative impacted-test time crosses this, stop
#     launching new mutants and report the remainder as skipped. This is the
#     guard that keeps a broad-footprint diff (e.g. touching factory.py,
#     whose impacted-test set is ~60 files / ~18s a run) inside the ~2-3 min
#     target — without it, 20 x 18s would be ~6 min.
#   * MAX_IMPACTED_TEST_FILES — bound the per-mutant pytest cost by capping
#     how many impacted test files each mutant runs against.
MAX_MUTANTS = 20
PER_MUTANT_TIMEOUT_S = 60
TOTAL_BUDGET_S = 150.0
MAX_IMPACTED_TEST_FILES = 40

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[0;33m"
_RESET = "\033[0m"

# Tests that import the mutated module are selected by these markers only —
# the fast per-commit tiers. (Integration / e2e run in the nightly full
# scope.) Matches safe-commit.sh's --fast marker set.
_IMPACTED_TEST_MARKERS = "unit or bdd or contract"


@dataclass(frozen=True)
class Mutant:
    """One single-token mutation of a source line within a touched span."""

    path: Path  # repo-relative source file
    lineno: int  # 1-based line number in the original source
    col: int  # 0-based column of the mutated token
    original: str  # the original token / fragment (for the report)
    mutation: str  # what it became (for the report)
    mutated_source: str  # full file content with the mutation applied


@dataclass(frozen=True)
class MutantResult:
    """Outcome of running a mutant's impacted tests."""

    mutant: Mutant
    survived: bool  # True iff impacted tests PASSED with the logic changed
    detail: str  # "killed" / "survived" / "timeout (treated as killed)" / "no impacted tests"
    elapsed_s: float


# ── diff → touched function spans ───────────────────────────────────────


def _git(args: list[str]) -> str:
    """Run ``git <args>`` at REPO_ROOT; return stdout (empty on failure)."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")


def changed_lines(base: str | None) -> dict[Path, set[int]]:
    """Map each changed ``kairix/**.py`` file to the set of NEW line numbers
    its diff added or modified.

    ``base is None`` → staged diff (``--cached``). Otherwise diff against
    ``base`` (e.g. ``origin/main``). Only ``kairix/`` python files are
    considered — tests and scripts are out of scope for mutation (we mutate
    production code and ask whether tests catch it).
    """
    diff_args = ["diff", "--unified=0"]
    if base is None:
        diff_args.append("--cached")
    else:
        diff_args.append(base)
    return _changed_lines_from_diff(_git(diff_args))


def _changed_lines_from_diff(diff: str) -> dict[Path, set[int]]:
    """Parse a ``git diff --unified=0`` text into ``{kairix-py-path: {lines}}``.

    Split from :func:`changed_lines` so the (pure) parse is unit-testable
    without a git subprocess. Only ``kairix/**.py`` files contribute; a hunk
    that only deletes lines (``+count`` == 0) adds nothing.
    """
    out: dict[Path, set[int]] = {}
    current: Path | None = None
    for line in diff.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            rel = file_match.group("path")
            current = Path(rel) if rel.startswith("kairix/") and rel.endswith(".py") else None
            continue
        if current is None:
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            start = int(hunk.group("start"))
            count = int(hunk.group("count")) if hunk.group("count") is not None else 1
            if count > 0:
                out.setdefault(current, set()).update(range(start, start + count))
    return out


def _enclosing_function_lines(source: str, changed: set[int]) -> set[int]:
    """Narrow ``changed`` to lines that fall inside a function/method body.

    A changed line outside any ``def`` (module-level constant, class body,
    import) is not a behavioural span we mutate — mutation testing pins
    *logic*, and logic lives in function bodies. Returns the subset of
    ``changed`` that lies within some FunctionDef/AsyncFunctionDef span.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno if node.end_lineno is not None else node.lineno
            spans.append((node.lineno, end))
    return {ln for ln in changed if any(lo <= ln <= hi for lo, hi in spans)}


# ── mutation operators ──────────────────────────────────────────────────
#
# Each operator inspects an AST node and yields zero or more (original,
# mutation) token rewrites anchored at a (lineno, col_offset). We rewrite
# the SOURCE TEXT at that anchor rather than unparsing the AST, so the
# mutated file is a minimal one-token delta a human could read in a diff —
# and so formatting / comments survive untouched.

_COMPARE_SWAPS = {
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", "<="),
    ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="),
    ast.GtE: (">=", ">"),
}
_BOOL_SWAP = {ast.And: ("and", "or"), ast.Or: ("or", "and")}


def _rewrite_token(lines: list[str], lineno: int, original: str, mutation: str, near_col: int) -> str | None:
    """Replace the FIRST occurrence of ``original`` at or after ``near_col``
    on 1-based ``lineno`` with ``mutation``. Returns the full mutated source,
    or ``None`` if the token isn't found where expected (defensive: skip
    rather than corrupt)."""
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        return None
    line = lines[idx]
    pos = line.find(original, max(0, near_col))
    if pos == -1:
        pos = line.find(original)
    if pos == -1:
        return None
    mutated_line = line[:pos] + mutation + line[pos + len(original) :]
    new_lines = list(lines)
    new_lines[idx] = mutated_line
    return "\n".join(new_lines) + ("\n" if lines and not lines[-1].endswith("\n") else "")


def _comparison_mutants(node: ast.Compare, lines: list[str], path: Path, scope: set[int]) -> list[Mutant]:
    """Swap each comparison operator (``==``↔``!=`` etc.) on an in-scope line."""
    out: list[Mutant] = []
    # ast does not give per-operator line/col, so anchor on the line of the
    # left operand; for the common single-op comparison this is the op line.
    for op in node.ops:
        swap = _COMPARE_SWAPS.get(type(op))
        if swap is None:
            continue
        lineno = node.left.end_lineno or node.lineno
        if lineno not in scope:
            continue
        original, mutation = swap
        near_col = node.left.end_col_offset or 0
        mutated = _rewrite_token(lines, lineno, original, mutation, near_col)
        if mutated is not None:
            out.append(Mutant(path, lineno, near_col, original, mutation, mutated))
    return out


def _boolop_mutants(node: ast.BoolOp, lines: list[str], path: Path, scope: set[int]) -> list[Mutant]:
    """``and``↔``or`` swap, plus drop-a-conjunct on ``and`` (the most common
    correctness-load-bearing boolean shape)."""
    out: list[Mutant] = []
    keyword, replacement = _BOOL_SWAP[type(node.op)]
    lineno = node.values[0].end_lineno or node.lineno
    if lineno in scope:
        near_col = node.values[0].end_col_offset or 0
        mutated = _rewrite_token(lines, lineno, keyword, replacement, near_col)
        if mutated is not None:
            out.append(Mutant(path, lineno, near_col, keyword, replacement, mutated))
    return out


def _constant_mutants(node: ast.Constant, lines: list[str], path: Path, scope: set[int]) -> list[Mutant]:
    """``True``↔``False`` literal flip on an in-scope line."""
    if not isinstance(node.value, bool):
        return []
    if node.lineno not in scope:
        return []
    original = "True" if node.value else "False"
    mutation = "False" if node.value else "True"
    mutated = _rewrite_token(lines, node.lineno, original, mutation, node.col_offset)
    if mutated is None:
        return []
    return [Mutant(path, node.lineno, node.col_offset, original, mutation, mutated)]


def generate_mutants(source: str, path: Path, scope: set[int]) -> list[Mutant]:
    """All single-token mutants on in-``scope`` lines of ``source``.

    ``scope`` is the set of changed line numbers already narrowed to
    function bodies. Mutants are produced deterministically in source order
    so the per-commit cap selects the same first-N every run.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[Mutant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            out.extend(_comparison_mutants(node, lines, path, scope))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            out.extend(_boolop_mutants(node, lines, path, scope))
        elif isinstance(node, ast.Constant):
            out.extend(_constant_mutants(node, lines, path, scope))
    # Deterministic order: by line then column.
    out.sort(key=lambda m: (m.lineno, m.col))
    return out


# ── impacted-test selection ─────────────────────────────────────────────


def _module_path(rel: Path) -> str:
    """``kairix/foo/bar.py`` → ``kairix.foo.bar`` for import-grep."""
    return str(rel.with_suffix("")).replace("/", ".")


def _same_module_tests(paths: set[Path], found: set[str]) -> list[str]:
    """The mutated modules' OWN test files within ``found``.

    A mutation to ``kairix/**/<mod>.py`` is most reliably killed by that
    module's own tests, which live in either ``test_<mod>.py`` OR the common
    aspect-suffixed form ``test_<mod>_<aspect>.py`` (e.g. a mutation to
    ``topology.py`` is killed by ``test_topology_config_parser.py``; one
    to ``topology_applier.py`` by ``test_topology_applier_unit.py``).
    Both shapes count as same-module so they always survive the impacted-test
    cap: otherwise a co-mutated widely-imported file (e.g. ``factory.py``,
    imported by 100+ test files) evicts a small module's own tests via the
    alphabetical truncation, leaving the module's mutants un-covered. Without
    the suffix match, an aspect-named unit test that genuinely kills a mutant
    is silently dropped from the window and the mutant reads as a survivor.
    Returns a deterministic (sorted) list."""
    out: set[str] = set()
    for p in paths:
        exact = f"test_{p.stem}.py"
        prefix = f"test_{p.stem}_"
        for tf in found:
            name = Path(tf).name
            if name == exact or name.startswith(prefix):
                out.add(tf)
    return sorted(out)


def _prioritise(found: set[str], paths: set[Path]) -> list[str]:
    """Order impacted tests so a mutated module's own tests survive the cap.

    Same-module tests (``test_<mod>.py``) come first, then the remaining
    importers fill the budget up to ``MAX_IMPACTED_TEST_FILES``. Without this,
    the alphabetical truncation could evict a small module's direct tests when
    a widely-imported file is co-mutated in the same diff."""
    same_module = _same_module_tests(paths, found)
    rest = sorted(found - set(same_module))
    budget = max(0, MAX_IMPACTED_TEST_FILES - len(same_module))
    return same_module + rest[:budget]


def impacted_tests(paths: set[Path], root: Path = REPO_ROOT) -> list[str]:
    """Test files that import any mutated module — the import-graph heuristic
    ``safe-commit.sh --fast`` uses. Returns test-file paths relative to ``root``
    (the repository)."""
    tests_dir = root / "tests"
    if not tests_dir.exists():
        return []
    found: set[str] = set()
    needles = {_module_path(p) for p in paths}
    needles.update(str(p) for p in paths)  # also match path-string references
    # A module's own test file may import it through its package's re-export
    # (``from kairix.memory_stores import KairixNativeStore``), which names no
    # module path: count a test named after the module, or after its package
    # (``test_secrets.py`` for ``kairix/secrets/_legacy.py``), that imports
    # that package.
    reexports = {(_module_path(p.parent), name) for p in paths for name in (f"test_{p.stem}", f"test_{p.parent.name}")}
    # A module under a hyphenated directory (``openclaw/memory-prompt/plugin.py``)
    # cannot be imported by name; its tests load it by path, so they name the
    # directory (``memory-prompt``) or its importable spelling (``memory_prompt``).
    needles.update(
        spelling for p in paths for part in p.parent.parts if "-" in part for spelling in (part, part.replace("-", "_"))
    )
    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(needle in text for needle in needles) or any(
            test_file.stem.startswith(name) and f"{package} import" in text for package, name in reexports
        ):
            found.add(str(test_file.relative_to(root)))
    # Bound the per-mutant pytest cost: a module imported by 60+ test files
    # would make each mutant run a multi-minute suite. The first N (sorted,
    # deterministic) are a representative cover — a mutant that survives all
    # of them is a survivor; one killed by any of them is killed.
    #
    # A mutated module's OWN tests (test_<mod>.py) are always included first,
    # then the rest fill the remaining budget. This keeps a module's direct
    # tests in-window even when a widely-imported file is co-mutated in the
    # same diff — without it the alphabetical cap silently drops them.
    return _prioritise(found, paths)


def tests_for_mutant(
    path: Path,
    cache: dict[Path, list[str]],
    find: Callable[[set[Path]], list[str]] = impacted_tests,
) -> list[str]:
    """The fast-tier tests that import ``path`` — the module ONE mutant changed.

    Each mutant runs against its own module's importers, never one set for the
    whole diff: a diff-wide set is capped across every touched file, so on a
    wide diff a module's own tests fall out of the window and its killed
    mutants read as survivors (the 2026-10-08 nightly reported 3 such false
    survivors over 90 touched files). ``cache`` holds each module's list, so
    a module is searched once however many mutants it has."""
    if path not in cache:
        cache[path] = find({path})
    return cache[path]


def _run_impacted_tests(test_files: list[str], timeout_s: int) -> tuple[bool, str, float]:
    """Run ``pytest`` over ``test_files`` (fast markers, no coverage).

    Returns ``(passed, detail, elapsed_s)``. ``passed`` is True ONLY on a
    clean pytest exit 0 — a timeout, collection error, or any failure is
    a non-survivor (the mutant was caught, conservatively)."""
    if not test_files:
        return False, "no impacted tests", 0.0
    start = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *test_files,
                "-x",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
                "--no-cov",
                "-m",
                _IMPACTED_TEST_MARKERS,
                "--timeout=30",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout >{timeout_s}s (treated as killed)", float(timeout_s)
    elapsed = time.monotonic() - start
    passed = result.returncode == 0
    detail = "survived" if passed else "killed"
    return passed, detail, elapsed


# ── orchestration ───────────────────────────────────────────────────────


def _apply_and_test(mutant: Mutant, test_files: list[str]) -> MutantResult:
    """Write the mutated source to the real file path (backed up first), run
    impacted tests, restore. The file IS the production file — pytest imports
    it — so we mutate in place and always restore in ``finally``.

    ``test_files`` is guaranteed non-empty by the caller (``run`` short-
    circuits when no fast-tier test imports the changed module). A clean
    pytest pass = SURVIVED; any non-zero exit (failure, timeout, collection
    error) = killed (conservative: a survivor is only ever a clean pass)."""
    target = REPO_ROOT / mutant.path
    original_bytes = target.read_bytes()
    try:
        target.write_text(mutant.mutated_source, encoding="utf-8")
        passed, detail, elapsed = _run_impacted_tests(test_files, PER_MUTANT_TIMEOUT_S)
    finally:
        # Restore byte-for-byte — never leave a mutated file behind, even on
        # KeyboardInterrupt / exception.
        target.write_bytes(original_bytes)
    return MutantResult(mutant=mutant, survived=passed, detail=detail, elapsed_s=elapsed)


def _survivor_report(result: MutantResult) -> str:
    """F21 operator-outcome block for one survivor."""
    m = result.mutant
    return (
        f"{_RED}mutant survived{_RESET}: {m.path}:{m.lineno} "
        f"`{m.original}` -> `{m.mutation}` ({result.detail}) — "
        "the tests that cover this line pass with the logic changed.\n"
        f"  fix: add/strengthen an assertion in the impacted test(s) that pins this "
        f"behaviour (assert the OUTCOME the `{m.original}` produces, not just that the "
        "function ran).\n"
        f"  next: re-run `python3 scripts/checks/mutation_parity.py` after strengthening "
        "the test — the mutant must then be KILLED.\n"
        '  run: bash scripts/safe-commit.sh "test(mutation): pin '
        f'{m.path.stem} behaviour at line {m.lineno}"'
    )


def limits(*, full_scope: bool, max_mutants: int = MAX_MUTANTS) -> tuple[int | None, float | None]:
    """``(mutant cap, total-time budget)`` for a run; ``None`` means unlimited.

    The commit-time run is capped and budgeted to stay inside the local loop,
    and defers the rest to the nightly full-scope run. That run therefore has
    neither cap nor budget: otherwise the deferred mutants never run anywhere."""
    if full_scope:
        return None, None
    return max_mutants, TOTAL_BUDGET_S


def shard(mutants: list[Mutant], index: int, count: int) -> list[Mutant]:
    """The ``index``-th of ``count`` disjoint shards of ``mutants``.

    Mutants are generated in a deterministic order (sorted file, then source
    order), so taking every ``count``-th one from ``index`` partitions them:
    each mutant lands in exactly one shard, and the shards together cover all.
    The nightly suite runs the shards as parallel jobs so each stays well under
    the job timeout however large the since-release diff is."""
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"shard index {index} is not in range for {count} shard(s)")
    return mutants[index::count]


def run(
    *,
    base: str | None,
    max_mutants: int = MAX_MUTANTS,
    full_scope: bool = False,
    shard_index: int = 0,
    shard_count: int = 1,
) -> int:
    """Diff-scoped mutation run. Returns process exit code.

    * ``base`` — diff ref, or ``None`` for the staged diff.
    * ``full_scope`` — every mutant on the diff, with no cap or time budget
      (the nightly mutation suite). A mutant with no selected test fails a
      full-scope run: it was never applied, so it cannot count as killed.
    * ``shard_index`` / ``shard_count`` — run only that shard of the mutants.

    Any survivor fails — there is no survivor list to excuse one.
    """
    touched = changed_lines(base)
    if not touched:
        print(f"{_GREEN}PASS mutation_parity{_RESET} — no mutable production-code diff (0 mutants).")
        return 0

    # Build the in-scope mutant set across all touched files.
    all_mutants: list[Mutant] = []
    for rel, lines_changed in sorted(touched.items()):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        scope = _enclosing_function_lines(source, lines_changed)
        all_mutants.extend(generate_mutants(source, rel, scope))

    if not all_mutants:
        print(
            f"{_GREEN}PASS mutation_parity{_RESET} — "
            "diff touched no mutable logic (0 mutants in changed function bodies)."
        )
        return 0

    cap, budget = limits(full_scope=full_scope, max_mutants=max_mutants)
    pool = all_mutants if cap is None else all_mutants[:cap]
    skipped = len(all_mutants) - len(pool)
    capped = shard(pool, shard_index, shard_count)
    test_files = impacted_tests(set(touched))
    per_module: dict[Path, list[str]] = {}

    if not test_files and not full_scope:
        # No fast-tier test imports the changed module(s). The diff-scoped
        # gate's job is to catch WEAK tests that exist — it does not mandate
        # a unit test for every line (F7 per-file coverage owns "untested
        # line", and the nightly full-scope leg widens the marker set). Pass
        # with a notice rather than fail a legitimate integration-only change.
        print(
            f"{_YELLOW}PASS mutation_parity{_RESET} — "
            f"{len(capped)} mutant(s) across {len(touched)} file(s), but no "
            "fast-tier (unit/bdd/contract) test imports the changed module(s); "
            "nothing to mutate against here. (F7 coverage owns 'untested line'; "
            "the nightly full-scope leg covers wider tiers.)"
        )
        return 0

    print(
        f"=== mutation_parity: {len(capped)} mutant(s) "
        f"({skipped} over the {max_mutants}-cap skipped) "
        f"across {len(touched)} file(s); {len(test_files)} impacted test file(s) ==="
    )

    survivors: list[MutantResult] = []
    uncovered: list[Mutant] = []
    total_elapsed = 0.0
    budget_skipped = 0
    for i, mutant in enumerate(capped, start=1):
        if budget is not None and total_elapsed >= budget:
            # Total-time budget hit: stop launching new mutants. The
            # remainder are reported as skipped, never silently dropped —
            # the nightly full-scope run (no budget) covers them.
            budget_skipped = len(capped) - (i - 1)
            print(
                f"  [budget] {total_elapsed:.0f}s >= {budget:.0f}s cap reached — "
                f"{budget_skipped} remaining mutant(s) deferred to nightly full-scope."
            )
            break
        own_tests = tests_for_mutant(mutant.path, per_module)
        if not own_tests:
            print(f"  [{i}/{len(capped)}] {mutant.path}:{mutant.lineno} — no fast-tier test imports it; skipped")
            uncovered.append(mutant)
            continue
        result = _apply_and_test(mutant, own_tests)
        total_elapsed += result.elapsed_s
        marker = f"{_RED}SURVIVED{_RESET}" if result.survived else f"{_GREEN}killed{_RESET}"
        print(
            f"  [{i}/{len(capped)}] {mutant.path}:{mutant.lineno} "
            f"`{mutant.original}`->`{mutant.mutation}` — {marker} ({result.elapsed_s:.1f}s)"
        )
        if result.survived:
            survivors.append(result)

    ran = len(capped) - budget_skipped - len(uncovered)
    print(
        f"--- {len(survivors)} survivor(s) of {ran} mutant(s) run; "
        f"{len(uncovered)} with no selected test; {total_elapsed:.1f}s total ---"
    )

    return _verdict(survivors, uncovered if full_scope else [])


def _uncovered_report(mutant: Mutant) -> str:
    """F21 operator-outcome block for a mutant a full-scope run could not apply."""
    return (
        f"{_RED}mutant not tested{_RESET}: {mutant.path}:{mutant.lineno} "
        f"`{mutant.original}` -> `{mutant.mutation}` — no fast-tier test was selected for this module, "
        "so the mutant was never applied.\n"
        "  fix: add a unit/contract test that imports the module (or name the module's path in an "
        "existing test that exercises it), so the runner selects it.\n"
        "  next: re-run `python3 scripts/checks/mutation_parity.py --base <ref> --full-scope`.\n"
        f'  run: bash scripts/safe-commit.sh "test(mutation): cover {mutant.path.stem}"'
    )


def _verdict(survivors: list[MutantResult], uncovered: list[Mutant] | None = None) -> int:
    """Translate the run into an exit code: any survivor fails, and so does any
    mutant a full-scope run could not apply (``uncovered``)."""
    uncovered = uncovered or []
    if not survivors and not uncovered:
        print(f"{_GREEN}PASS mutation_parity{_RESET} — 0 survivors (every mutant on the diff was killed).")
        return 0

    print(
        f"{_RED}FAIL mutation_parity{_RESET} — {len(survivors)} survivor(s), "
        f"{len(uncovered)} mutant(s) with no selected test:",
        file=sys.stderr,
    )
    for result in survivors:
        print(_survivor_report(result), file=sys.stderr)
    for mutant in uncovered:
        print(_uncovered_report(mutant), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help="diff against this ref (e.g. origin/main) instead of the staged diff",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        default=MAX_MUTANTS,
        help=f"hard cap on mutants generated (default {MAX_MUTANTS})",
    )
    parser.add_argument(
        "--full-scope",
        action="store_true",
        help="run every mutant on the diff, with no cap or time budget (the nightly suite)",
    )
    parser.add_argument("--shard-index", type=int, default=0, help="run only this shard (0-based)")
    parser.add_argument("--shard-count", type=int, default=1, help="split the mutants into this many shards")
    args = parser.parse_args(argv)
    return run(
        base=args.base,
        max_mutants=args.max_mutants,
        full_scope=args.full_scope,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )


if __name__ == "__main__":
    sys.exit(main())
