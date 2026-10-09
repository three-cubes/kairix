"""F2 detector (static half): no test writes a ``KAIRIX_*`` process-env key.

Fast pre-commit feedback for the common spellings, with a string-literal key
starting with ``KAIRIX_``:

* ``monkeypatch.setenv("KAIRIX_X", ...)`` / ``monkeypatch.delenv("KAIRIX_X")``
  (any fixture name);
* ``os.environ["KAIRIX_X"] = ...`` / ``+=`` / ``del os.environ["KAIRIX_X"]``
  (or the name bound by ``from os import environ [as e]``, unless the
  enclosing function rebinds it);
* ``os.environ.pop("KAIRIX_X")`` / ``os.environ.setdefault("KAIRIX_X", ...)``.

Writes inside ``with allow_baseline_writes():`` in ``tests/conftest.py`` (the
session env baseline) are exempt, mirroring the runtime guard.

The exact half is the runtime guard ``tests/fixtures/process_state_guard.py``:
an audit hook sees EVERY env write (any spelling, any computed key) while a
test runs and fails that test. This file deliberately stays a small AST
match — it is the fast loop, not the proof.

Output: ``path:line: shape`` per violation; the gate fails on any.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _fitness_rule import FitnessRule
from tc_fitness import gate_keys

REMEDIATION = """KAIRIX_* process-env write found in a test. Refactor to an explicit
``env=`` mapping / ``paths=FakePaths(...)`` / Deps seam to pass.

fix: pass the value through the production seam instead of the process
env — ``paths=FakePaths(...)`` from tests/fakes.py, an ``env={...}``
mapping on the reader (``read_int_env(..., env=...)``,
``load_secrets(path, env=...)``), or a ``*Deps`` dataclass. If the
production function reads the env var directly, add an
``env: Mapping[str, str] | None = None`` parameter that production leaves
as ``None`` (reads os.environ at the kairix.paths boundary) — the
boundary-only pattern from #139. A subprocess test passes ``env=`` to the
child instead.
next: re-run ``python3 scripts/checks/check_no_env_monkeypatch.py``
(or ``python3 scripts/checks/run_checks.py --gate F2``). The runtime half
(tests/fixtures/process_state_guard.py) fails any spelling this static
check misses when the test runs.
run: bash scripts/safe-commit.sh "test(<area>): inject env via seam instead of mutating os.environ"

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)
  assert resolve_dispatch_concurrency(env={'KAIRIX_MAX_CONCURRENCY': '3'}) == 3

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  os.environ['KAIRIX_DB_PATH'] = str(tmp_path / 'db.sqlite')
  os.environ.pop('KAIRIX_DB_PATH', None)

The one sanctioned writer is the session baseline in tests/conftest.py,
whose writes sit in ``with allow_baseline_writes():`` (both halves exempt
that block; using it anywhere else fails).

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."""

_ENV_HELPERS = {"setenv", "delenv"}
_BASELINE_CONTEXT = "allow_baseline_writes"
_BASELINE_HOME = (Path(__file__).resolve().parents[2] / "tests" / "conftest.py").resolve()
_ENVIRON_METHODS = {"pop", "setdefault"}


def _is_kairix_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("KAIRIX_")


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class _Environ:
    """Decides whether an expression is ``os.environ`` in one parsed file.

    A bare name counts only when the file binds it with ``from os import
    environ [as name]`` and the enclosing function does not rebind it (a
    parameter or an assignment of that name in the function) — a simple
    local check, not a scope engine.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "os"
            for alias in node.names
            if alias.name == "environ"
        }
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def _rebound_locally(self, node: ast.Name) -> bool:
        scope = self.parents.get(node)
        while scope is not None and not isinstance(scope, _FUNCTIONS):
            scope = self.parents.get(scope)
        if scope is None:
            return False
        args = scope.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if a}
        stores = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        return node.id in params | stores

    def __call__(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute):
            return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
        return isinstance(node, ast.Name) and node.id in self.names and not self._rebound_locally(node)


def _shape(node: ast.AST, is_environ: _Environ) -> str | None:
    def subscript_write(target: ast.expr) -> bool:
        return isinstance(target, ast.Subscript) and is_environ(target.value) and _is_kairix_literal(target.slice)

    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(subscript_write(t) for t in targets):
            return "assign os.environ[KAIRIX_*]"
    elif isinstance(node, ast.Delete):
        if any(subscript_write(t) for t in node.targets):
            return "del os.environ[KAIRIX_*]"
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        func, first = node.func, node.args[0]
        if func.attr in _ENV_HELPERS and _is_kairix_literal(first):
            return f"{func.attr}(KAIRIX_*)"
        if func.attr in _ENVIRON_METHODS and is_environ(func.value) and _is_kairix_literal(first):
            return f"os.environ.{func.attr}(KAIRIX_*)"
    return None


def _baseline_blocks(tree: ast.AST) -> list[ast.With]:
    """``with allow_baseline_writes():`` blocks — the session baseline's exemption."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == _BASELINE_CONTEXT
            for item in node.items
        )
    ]


def file_violations(path: Path) -> list[str]:
    """Every ``line: shape`` KAIRIX_* env write in ``path`` (sorted by line).

    Writes inside ``with allow_baseline_writes():`` are the session baseline's
    (the runtime guard exempts the same block); the block itself is a
    violation anywhere but the root ``tests/conftest.py``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return []
    blocks = _baseline_blocks(tree)
    exempt = {line for b in blocks for line in range(b.lineno, (b.end_lineno or b.lineno) + 1)}
    is_environ = _Environ(tree)
    found = {(getattr(node, "lineno", 0), shape) for node in ast.walk(tree) if (shape := _shape(node, is_environ))}
    found = {(line, shape) for line, shape in found if line not in exempt}
    if path.resolve() != _BASELINE_HOME:
        found |= {(b.lineno, f"{_BASELINE_CONTEXT}() outside tests/conftest.py") for b in blocks}
    return [f"{line}: {shape}" for line, shape in sorted(found)]


def file_has_env_monkeypatch(path: Path) -> bool:
    """Return True iff ``path`` writes a KAIRIX_* process-env key in a detected spelling."""
    return bool(file_violations(path))


class F2(FitnessRule):
    """F2 static half as an in-process rule over ``tests/`` (staged runs narrow it)."""

    name = "no-env-monkeypatch"
    remediation = REMEDIATION
    roots = ("tests",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_env_monkeypatch(path)

    def run(self) -> int:
        found: set[str] = set()
        for path in self.enumerate_files():
            rel = str(self._repo_relative(path))
            if self.is_in_scope(rel):
                found.update(f"{rel}:{violation}" for violation in file_violations(path))
        return int(gate_keys(self.name, found, self.remediation))


def main() -> int:
    return F2().run()


if __name__ == "__main__":
    sys.exit(main())
