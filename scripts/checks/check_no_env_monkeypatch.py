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
[as e]`` and local rebinding followed to a fixpoint (``a = os.environ; b = a``);
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
from _ast_key_taint import enclosing_function, parent_map, tainted_names
from _mapping_writes import ProcessMapping, WriteSurface

# REMEDIATION text — the shell wrapper ``check-no-env-monkeypatch.sh``
# owns the user-facing message that prints when the gate fails. This
# constant exists for F21 (actionable-feedback) compliance and is
# semantically equivalent to the shell wrapper's REMEDIATION.
REMEDIATION = """KAIRIX_* process-env write found in a test. Refactor to an explicit
``env=`` mapping / ``paths=FakePaths(...)`` / Deps seam to pass.

Covers every MutableMapping write on os.environ (subscript assign / del,
|=, __setitem__ / __delitem__ / pop / setdefault / update, clear / popitem),
replacing os.environ wholesale, monkeypatch setenv / delenv / setitem /
delitem / setattr / delattr (any MonkeyPatch instance, positional or keyword
arguments), and patch.dict / patch.object / patch on os.environ — through
any alias (import os as o, from os import environ, env = os.environ). A key
held in a variable or returned by a helper counts too.

fix: pass the value through the production seam instead of the process
env — ``paths=FakePaths(...)`` from tests/fakes.py, an ``env={...}``
mapping on the reader (``read_int_env(..., env=...)``, ``get_embed_provider(env=...)``),
or a ``*Deps`` dataclass. If the production function reads the env var
directly, add an ``env: Mapping[str, str] | None = None`` parameter that
production leaves as ``None`` (reads os.environ at the kairix.paths
boundary) — the boundary-only pattern from #139. A non-KAIRIX_ test-only
variable name is fine when the code under test hydrates arbitrary keys.
next: re-run ``python3 scripts/checks/check_no_env_monkeypatch.py``
(or ``bash scripts/checks/check-no-env-monkeypatch.sh``) to confirm
the gate goes green.
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
  env = os.environ; env |= {'KAIRIX_DB_PATH': '/x'}
  monkeypatch.setenv(name='KAIRIX_DB_PATH', value='/x')
  monkeypatch.setattr(os, 'environ', {'KAIRIX_DB_PATH': '/x'})

Recognised structurally (not violations): writes inside a conftest.py
``@pytest.fixture(scope="session", autouse=True)`` hermetic baseline, and
the teardown of a fixture that snapshots ``dict(os.environ)`` before its
``yield`` and restores from that snapshot after it.

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."""

_PREFIX = "KAIRIX_"
_MARKER = "KAIRIX_*"


def _is_protected(value: str) -> bool:
    return value.startswith(_PREFIX)


class _Ctx:
    """Per-file resolution state: the shared ``os.environ`` write surface."""

    def __init__(self, tree: ast.AST, path: Path) -> None:
        self.path = path
        self.tainted = tainted_names(tree, _is_protected)
        self.environ = ProcessMapping.resolve(tree, "os", "environ")
        self.surface = WriteSurface(tree, self.environ, _is_protected, self.tainted, _MARKER)
        self.parents = parent_map(tree)

    def is_environ(self, expr: ast.expr) -> bool:
        return self.environ.is_receiver(expr)


def _statement_shapes(node: ast.AST, ctx: _Ctx) -> list[str]:
    """Every KAIRIX_* env write ``node`` performs (see ``_mapping_writes``)."""
    return ctx.surface.writes(node)


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
    """For a snapshotting fixture: (first yield line, snapshot names bound before it)."""
    if _fixture_call(fn) is None:
        return None
    yields = [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))]
    if not yields:
        return None
    first_yield = min(yields)
    snapshots = {
        t.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and n.lineno < first_yield and _is_environ_copy(n.value, ctx)
        for t in n.targets
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


def _followed_by_full_restore(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST, snapshots: set[str], ctx: _Ctx
) -> bool:
    """A teardown ``os.environ.clear()`` is restoration only when the same
    fixture then puts the whole snapshot back with ``os.environ.update(snapshot)``."""
    line = getattr(node, "lineno", 0)
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "update"
        and ctx.is_environ(n.func.value)
        and n.lineno > line
        and len(n.args) == 1
        and _is_snapshot_name(n.args[0], snapshots)
        for n in ast.walk(fn)
    )


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
    fn = enclosing_function(ctx.parents, node)
    while fn is not None:
        if _is_session_baseline(fn, ctx) or _is_snapshot_restore(fn, node, ctx):
            return True
        fn = enclosing_function(ctx.parents, fn)
    return False


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


def file_violations(path: Path) -> list[str]:
    """Every ``line: shape`` KAIRIX_* env write in ``path`` (sorted by line)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return []
    ctx = _Ctx(tree, path)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        shapes = _statement_shapes(node, ctx)
        if shapes and not _is_recognised_boundary(node, ctx):
            found.extend((getattr(node, "lineno", 0), s) for s in shapes)
    return [f"{line}: {shape}" for line, shape in sorted(found)]


def file_has_env_monkeypatch(path: Path) -> bool:
    """Return True iff ``path`` writes a KAIRIX_* process-env key (any F2 shape)."""
    return bool(file_violations(path))


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    for path in sorted(root.rglob("*.py")):
        for violation in file_violations(path):
            print(f"{path}:{violation}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
