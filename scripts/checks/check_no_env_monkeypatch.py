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

ONE reviewed process-boundary shape is recognised STRUCTURALLY (no
allow-list, no path list, no pragma): the **session baseline** — a statement
directly in the body of a ``conftest.py`` fixture declared
``@pytest.fixture(scope="session", autouse=True)``: the one-per-run hermetic
baseline that clears ambient operator variables before any test runs and
undoes it at session end. There is no snapshot / restore exemption: a test
whose code under test writes the process env injects the env mapping through
the production seam (``env=`` / ``environ=``) instead of restoring
``os.environ`` afterwards; a subprocess test passes ``env=`` to the child.

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
from _mapping_writes import BUILTIN_GUARDED_NAMES, GuardedName, MappingGuard, ProcessMapping, guarded_rebinds
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
  snapshot = dict(os.environ); yield; os.environ.clear(); os.environ.update(snapshot)
  os = FakeOs()   # module-level rebind of a guarded name (os / environ / getattr ...)
  os.putenv('KAIRIX_DB_PATH', '/x')   # or os.unsetenv / posix.putenv / from os import putenv
  os.__dict__['environ']   # any dunder on os / os.environ (vars(os.environ) too)

Recognised structurally (not a violation): a statement directly in the
conftest.py ``@pytest.fixture(scope="session", autouse=True)`` hermetic
baseline. Nothing else — snapshot / clear() / update(snapshot) restores
fail too: inject the env mapping through the seam (env= / environ=)
instead of restoring the process env, or pass env= to a subprocess.

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
_PREFILTER = re.compile(r"environ|setenv|delenv|putenv|unsetenv|patch|setattr|delattr|getattr|vars|\.__\w+__")

#: Names F2 guards against rebinding at module / class level (or alongside
#: their own import in a function) — see ``_mapping_writes.guarded_rebinds``.
_F2_GUARDED_NAMES: dict[str, GuardedName] = {
    "os": ("module", "os"),
    "environ": ("from", "os", "environ"),
    **{name: BUILTIN_GUARDED_NAMES[name] for name in ("getattr", "setattr", "delattr")},
}


class _Ctx:
    """Per-file resolution state: the shared default-deny ``os.environ`` guard."""

    def __init__(self, index: ModuleIndex, path: Path) -> None:
        self.path = path
        self.index = index
        self.environ = ProcessMapping.resolve(index, "os", "environ")
        self.guard = MappingGuard(index, self.environ, _KEYS, ConstantTable(index), _MARKER)
        # os.putenv / os.unsetenv write the process env by key (posix / nt
        # export the same functions)
        self.guard.process_writers = {"putenv": ("key", "value"), "unsetenv": ("key",)}
        self.guard.process_writer_modules = ("os", "posix", "nt")
        self.parents = index.parents

    def is_environ(self, expr: ast.expr) -> bool:
        return self.environ.is_receiver(expr)


def _findings(ctx: _Ctx) -> list[tuple[ast.AST, str]]:
    """Every reference to ``os.environ`` outside the read allow-list that is not
    a provably safe write, plus helper-only writes (see ``_mapping_writes``),
    plus every rebinding of a guarded name (``os = ...`` at module level)."""
    return ctx.guard.findings() + guarded_rebinds(ctx.index, _F2_GUARDED_NAMES)


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


def _is_recognised_boundary(node: ast.AST, ctx: _Ctx) -> bool:
    """The ONE recognised shape: a statement DIRECTLY in the body of the
    canonical session-baseline fixture. The innermost ``def`` / ``lambda``
    around ``node`` must be the fixture itself, so a nested function, lambda
    or yielded / returned callback inherits nothing."""
    scope = immediate_scope(ctx.parents, node)
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return _is_session_baseline(scope, ctx)


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
