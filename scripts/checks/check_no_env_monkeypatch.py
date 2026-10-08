"""F2 detector: no test writes to a KAIRIX_* process-env key.

Walks every test file via AST and reports each statement that mutates a
``KAIRIX_*`` key of the process environment. The write surface is the shared
engine ``_mapping_writes`` (also used by F1 for ``sys.modules``):

* the full ``MutableMapping`` mutation API on ``os.environ`` — subscript
  assign / augassign / del, ``|=``, ``__setitem__`` / ``__delitem__`` /
  ``pop`` / ``setdefault`` / ``update`` / ``__ior__``, and ``clear()`` /
  ``popitem()`` (ALWAYS reported: they remove keys the AST cannot name, so
  they can remove ``KAIRIX_*`` ones);
* replacing ``os.environ`` wholesale — ``os.environ = m``,
  ``del os.environ``, ``setattr / delattr(os, "environ")``;
* pytest ``<monkeypatch>`` (fixture, ``pytest.MonkeyPatch()`` instance,
  ``MonkeyPatch.context()`` target): ``setenv`` / ``delenv`` /
  ``setitem`` / ``delitem`` and both ``setattr`` / ``delattr`` overloads;
* ``unittest.mock``: ``patch.dict(in_dict, values, clear, **kw)``,
  ``patch.object(os, "environ")``, ``patch("os.environ")``.

Every call is bound to the callee's parameter names (positional or keyword).
``os.environ`` resolves through ``import os as o``, ``from os import environ
[as e]`` (import statements only); binding ``os.environ`` / ``os`` / a bound
helper to a local name is itself a violation, never chased;
a copy (``dict(os.environ)``) never does. These direct forms bypass
``monkeypatch``'s auto-undo, so a forgotten restore leaks the value into every
later test in the process (the pytest-bdd ``KAIRIX_DB_PATH`` leak).

A key counts as ``KAIRIX_*`` when it is a literal, an f-string /
concatenation with a ``KAIRIX_`` lead, a name bound (any number of hops) from
such a literal, or a call to a helper returning one (``_ast_key_taint``). An
``update`` / ``|=`` / ``patch.dict`` payload the AST cannot see into (a
variable, a call, a ``**`` spread) also counts.

Two reviewed process-boundary shapes are recognised STRUCTURALLY (no
allow-list, no path list, no pragma):

* **Session baseline** — the write sits inside a ``conftest.py`` fixture
  declared ``@pytest.fixture(scope="session", autouse=True)``: the
  one-per-run hermetic baseline that clears ambient operator variables
  before any test runs and undoes it at session end.
* **Snapshot-restore teardown** — the write sits AFTER the ``yield`` of a
  ``@pytest.fixture`` generator that took a copy of ``os.environ``
  (``dict(os.environ)`` / ``os.environ.copy()`` / ``{**os.environ}``)
  BEFORE the yield, and the write is a genuine restore from it:
  ``os.environ.update(snapshot)`` (optionally preceded by
  ``os.environ.clear()``), ``os.environ[k] = snapshot[k]`` (same key), or a
  ``pop`` / ``del`` of ``k`` guarded by ``k`` being absent from the
  snapshot. It restores state a production seam legitimately wrote; it
  never seeds a value for a production read. Writes BEFORE the yield, and
  post-yield writes that merely mention the snapshot, are still violations.

Output: one ``path:line: shape`` per violation on stdout, sorted.
Pipes into ``arch_gate`` from ``_lib.sh``, which fails on any line.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import re

from _ast_key_taint import ConstantTable, ModuleIndex, ProtectedKeys, immediate_scope, parse_index
from _fitness_rule import FitnessRule
from _mapping_writes import MappingGuard, ProcessMapping
from tc_fitness import gate_keys

# REMEDIATION — the user-facing F21 message the gate prints on failure.
REMEDIATION = """KAIRIX_* process-env write found in a test. Refactor to an explicit
``env=`` mapping / ``paths=FakePaths(...)`` / Deps seam to pass.

The gate is DEFAULT-DENY: os.environ, the os module and bound helpers may
not be aliased (env = os.environ fails on its own); every direct reference
must be an allow-listed read — R[k], R.get / copy / items /
keys / values, k in R, len(R), dict(R), {**R}, iteration, env=R to
subprocess / os.exec* — or a write PROVEN to touch only non-KAIRIX_ keys.
Anything else fails: clear() / popitem(), passing R to any helper, returning
it, replacing it, monkeypatch.setenv / delenv / setitem / patch.dict with a
key that does not resolve statically to a non-KAIRIX_ value.

fix: pass the value through the production seam instead of the process
env — ``paths=FakePaths(...)`` from tests/fakes.py, an ``env={...}``
mapping on the reader (``read_int_env(..., env=...)``, ``get_embed_provider(env=...)``),
or a ``*Deps`` dataclass. If the production function reads the env var
directly, add an ``env: Mapping[str, str] | None = None`` parameter that
production leaves as ``None`` (reads os.environ at the kairix.paths
boundary) — the boundary-only pattern from #139. A non-KAIRIX_ test-only
variable name is fine when the code under test hydrates arbitrary keys.
next: re-run ``python3 scripts/checks/check_no_env_monkeypatch.py``
(or ``python3 scripts/checks/run_checks.py --gate F2``) to confirm the
gate goes green. An unresolved key counts as protected: make a genuinely
non-KAIRIX_ key a literal / module constant the gate can prove, or — for a
KAIRIX_ value — inject it through a seam rather than reshaping the write.
run: bash scripts/safe-commit.sh "test(<area>): inject env via seam instead of mutating os.environ"

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)
  assert resolve_dispatch_concurrency(env={'KAIRIX_MAX_CONCURRENCY': '3'}) == 3

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  os.environ['KAIRIX_DB_PATH'] = str(tmp_path / 'db.sqlite')
  os.environ.pop('KAIRIX_DB_PATH', None)
  with patch.dict(os.environ, {'KAIRIX_MAX_CONCURRENCY': '3'}): ...
  env = os.environ   # aliasing a guarded object
  monkeypatch.setenv(name='KAIRIX_DB_PATH', value='/x')
  monkeypatch.setattr(os, 'environ', {'KAIRIX_DB_PATH': '/x'})

Recognised structurally (not violations): writes inside a conftest.py
``@pytest.fixture(scope="session", autouse=True)`` hermetic baseline, and
the teardown of a fixture that snapshots ``dict(os.environ)`` before its
``yield`` and restores from that snapshot after it.

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."""

_KEYS = ProtectedKeys(prefixes=("KAIRIX_",))
_MARKER = "KAIRIX_*"

#: Cheap pre-parse filter on the tokens every F2 violation needs: the
#: ``environ`` name itself, a ``setenv`` / ``delenv`` helper, or a ``patch`` /
#: ``setattr`` / ``delattr`` / ``getattr`` that could reach ``os.environ``
#: through a constant-folded name (``"os.en" + "viron"``). A file with none of
#: these tokens cannot reference the mapping, so it is never parsed.
_PREFILTER = re.compile(r"environ|setenv|delenv|patch|setattr|delattr|getattr")


class _Ctx:
    """Per-file resolution state: the shared default-deny ``os.environ`` guard."""

    def __init__(self, index: ModuleIndex, path: Path) -> None:
        self.path = path
        self.index = index
        self.environ = ProcessMapping.resolve(index, "os", "environ")
        self.guard = MappingGuard(index, self.environ, _KEYS, ConstantTable(index), _MARKER)
        self.parents = index.parents

    def is_environ(self, expr: ast.expr) -> bool:
        return self.environ.is_receiver(expr)


def _findings(ctx: _Ctx) -> list[tuple[ast.AST, str]]:
    """Every reference to ``os.environ`` outside the read allow-list that is not
    a provably safe write, plus helper-only writes (see ``_mapping_writes``)."""
    return ctx.guard.findings()


# ---------------------------------------------------------------------------
# Structural recognitions (reviewed process-boundary shapes).
# ---------------------------------------------------------------------------


def _fixture_call(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    """The ``pytest.fixture`` / ``fixture`` decorator expression, if any."""
    for deco in fn.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return deco
        if isinstance(target, ast.Name) and target.id == "fixture":
            return deco
    return None


def _kwarg_constant(call: ast.expr, name: str) -> object:
    if not isinstance(call, ast.Call):
        return None
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _is_session_baseline(fn: ast.FunctionDef | ast.AsyncFunctionDef, ctx: _Ctx) -> bool:
    """A ``conftest.py`` fixture declared ``scope="session", autouse=True``."""
    deco = _fixture_call(fn)
    if deco is None or ctx.path.name != "conftest.py":
        return False
    return _kwarg_constant(deco, "scope") == "session" and _kwarg_constant(deco, "autouse") is True


def _is_environ_copy(expr: ast.expr, ctx: _Ctx) -> bool:
    """``dict(os.environ)`` / ``os.environ.copy()`` / ``{**os.environ}``."""
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name) and func.id == "dict" and len(expr.args) == 1:
            return ctx.is_environ(expr.args[0])
        if isinstance(func, ast.Attribute) and func.attr == "copy" and not expr.args:
            return ctx.is_environ(func.value)
    if isinstance(expr, ast.Dict) and len(expr.keys) == 1 and expr.keys[0] is None:
        return ctx.is_environ(expr.values[0])
    return False


def _snapshot_restore_names(fn: ast.FunctionDef | ast.AsyncFunctionDef, ctx: _Ctx) -> tuple[int, set[str]] | None:
    """For a snapshotting fixture: (first yield line, snapshot names bound before it).

    Only statements DIRECTLY in the fixture body count — the ``yield`` and the
    ``snapshot = dict(os.environ)`` assignment — never a nested ``def`` /
    ``lambda`` / ``class`` (consistent with the exemption scope rule)."""
    if _fixture_call(fn) is None:
        return None
    yields = [
        stmt.lineno
        for stmt in fn.body
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Yield, ast.YieldFrom))
    ]
    if not yields:
        return None
    first_yield = min(yields)
    snapshots = {
        t.id
        for stmt in fn.body
        if isinstance(stmt, ast.Assign) and stmt.lineno < first_yield and _is_environ_copy(stmt.value, ctx)
        for t in stmt.targets
        if isinstance(t, ast.Name)
    }
    return (first_yield, snapshots) if snapshots else None


def _is_snapshot_restore(fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST, ctx: _Ctx) -> bool:
    shape = _snapshot_restore_names(fn, ctx)
    if shape is None:
        return False
    first_yield, snapshots = shape
    if getattr(node, "lineno", 0) <= first_yield:
        return False
    return _is_genuine_restore(fn, node, snapshots, ctx)


def _is_snapshot_name(expr: ast.expr | None, snapshots: set[str]) -> bool:
    return isinstance(expr, ast.Name) and expr.id in snapshots


def _same_expr(a: ast.expr, b: ast.expr) -> bool:
    return ast.dump(a) == ast.dump(b)


def _guarded_by_key_absent(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST, key: ast.expr, snapshots: set[str], ctx: _Ctx
) -> bool:
    """``node`` only runs when ``key`` was absent from the snapshot.

    Matches ``if key not in snapshot: <node>`` and ``if key in snapshot: ...
    else: <node>`` — the pop / del half of a per-key restore.
    """
    child: ast.AST = node
    parent = ctx.parents.get(node)
    while parent is not None and parent is not fn:
        if isinstance(parent, ast.If) and isinstance(parent.test, ast.Compare):
            test = parent.test
            if len(test.ops) == 1 and _same_expr(test.left, key) and _is_snapshot_name(test.comparators[0], snapshots):
                in_body = any(child is stmt for stmt in parent.body)
                if isinstance(test.ops[0], ast.NotIn) and in_body:
                    return True
                if isinstance(test.ops[0], ast.In) and not in_body:
                    return True
        child, parent = parent, ctx.parents.get(parent)
    return False


def _straight_line_body(fn: ast.FunctionDef | ast.AsyncFunctionDef, stmt: ast.stmt, ctx: _Ctx) -> list[ast.stmt] | None:
    """The statement list ``stmt`` sits in, when that list runs unconditionally
    on ``fn``'s straight-line path: ``fn``'s own body, or the ``finally`` body
    of a ``try`` that is itself on that path. ``None`` under an ``if`` / loop /
    ``try`` body or handler / ``with`` / nested ``def`` / ``lambda``."""
    parent = ctx.parents.get(stmt)
    if parent is fn:
        return fn.body if any(stmt is s for s in fn.body) else None
    if isinstance(parent, ast.Try) and any(stmt is s for s in parent.finalbody):
        return parent.finalbody if _straight_line_body(fn, parent, ctx) is not None else None
    return None


def _is_snapshot_update_stmt(stmt: ast.stmt, snapshots: set[str], ctx: _Ctx) -> bool:
    call = stmt.value if isinstance(stmt, ast.Expr) else None
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "update"
        and ctx.is_environ(call.func.value)
        and len(call.args) == 1
        and not call.keywords
        and _is_snapshot_name(call.args[0], snapshots)
    )


def _followed_by_full_restore(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST, snapshots: set[str], ctx: _Ctx
) -> bool:
    """A teardown ``os.environ.clear()`` is restoration only when
    ``os.environ.update(snapshot)`` follows it on the SAME unconditional
    straight-line path: both are statements of one list (the fixture body or a
    ``finally`` body on that path), the update a later sibling. A conditional,
    looped, ``try``-body / ``except``, or nested-function restore does not count."""
    stmt = ctx.parents.get(node)
    if not isinstance(stmt, ast.Expr) or stmt.value is not node:
        return False
    body = _straight_line_body(fn, stmt, ctx)
    if body is None:
        return False
    position = next(i for i, s in enumerate(body) if s is stmt)
    for later in body[position + 1 :]:
        if _is_snapshot_update_stmt(later, snapshots, ctx):
            return True
        if _may_exit_early(later):
            return False  # an early ``return`` / ``raise`` before the restore voids it
    return False


def _may_exit_early(stmt: ast.stmt) -> bool:
    """``stmt`` contains a ``return`` / ``raise`` outside any nested function."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Return, ast.Raise)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _is_genuine_restore(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST, snapshots: set[str], ctx: _Ctx
) -> bool:
    """``node`` puts ``os.environ`` back to the snapshot — nothing else.

    Recognised: ``os.environ.update(snapshot)``; ``os.environ.clear()`` when
    the fixture later calls ``os.environ.update(snapshot)``;
    ``os.environ[k] = snapshot[k]`` (same key on both sides); and
    ``os.environ.pop(k, ...)`` /
    ``del os.environ[k]`` guarded by ``k`` being absent from the snapshot.
    Any other post-yield write that merely mentions the snapshot (e.g.
    ``os.environ["KAIRIX_X"] = snapshot.get("PATH")``) is still reported.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method == "update":
            return len(node.args) == 1 and not node.keywords and _is_snapshot_name(node.args[0], snapshots)
        if method == "clear" and not node.args:
            return _followed_by_full_restore(fn, node, snapshots, ctx)
        if method == "pop" and node.args:
            return _guarded_by_key_absent(fn, node, node.args[0], snapshots, ctx)
        return False
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
        return (
            isinstance(target, ast.Subscript)
            and isinstance(value, ast.Subscript)
            and _is_snapshot_name(value.value, snapshots)
            and _same_expr(target.slice, value.slice)
        )
    if isinstance(node, ast.Delete) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
        return _guarded_by_key_absent(fn, node, node.targets[0].slice, snapshots, ctx)
    return False


def _is_recognised_boundary(node: ast.AST, ctx: _Ctx) -> bool:
    """Only a statement DIRECTLY in the fixture's own body is exempt: the
    innermost ``def`` / ``lambda`` around ``node`` must be the fixture itself, so
    a nested function, lambda or yielded / returned callback inherits nothing."""
    scope = immediate_scope(ctx.parents, node)
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return _is_session_baseline(scope, ctx) or _is_snapshot_restore(scope, node, ctx)


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


def file_violations(path: Path) -> list[str]:
    """Every ``line: shape`` KAIRIX_* env write in ``path`` (sorted by line)."""
    index = parse_index(path, _PREFILTER)
    if index is None:
        return []
    ctx = _Ctx(index, path)
    found = {
        (getattr(node, "lineno", 0), label) for node, label in _findings(ctx) if not _is_recognised_boundary(node, ctx)
    }
    return [f"{line}: {label}" for line, label in sorted(found)]


def file_has_env_monkeypatch(path: Path) -> bool:
    """Return True iff ``path`` writes a KAIRIX_* process-env key (any F2 shape)."""
    return bool(file_violations(path))


class F2(FitnessRule):
    """F2 as an in-process :class:`FitnessRule` over ``tests/``.

    In-process (not a shell subprocess) so the shared runner's staged mode
    narrows :meth:`enumerate_files` to the staged test files — ``safe-commit.sh
    --check`` scans only what changed, while ``--all`` / CI scan every file.
    Reports ``path:line: shape`` keys rather than bare paths.
    """

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
