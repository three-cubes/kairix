"""F6: No ``*_fn=None`` test-only kwargs in production code.

Production functions with parameters named like ``search_fn``, ``chat_fn``,
``embed_fn``, etc. that default to ``None`` are typically test-substitution
seams added "just so tests can swap behaviour." This is the smell that
triggered the #113/#114 reverts: production grew complexity for tests
without operator value.

The legitimate seam pattern is **constructor injection** at a
boundary class (e.g. ``GoldBuilder(llm_judge=, retriever=)``) or
**Protocol injection at a use case** — not a per-helper ``_fn=None``
parameter on free functions.

Detection: any kairix/* module function with a keyword-only or default
parameter whose name ends in ``_fn`` and whose default is ``None``.

Allow-list: parameters declared in ``.architecture/baseline/test-only-kwargs-allow.txt``
(one per line, format ``module.path::function_name::param_name``) — these
are documented as having a real production caller using a non-default
value, OR they're a Protocol/Adapter wiring point at a true boundary.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate, python_files, repo_relative

REMEDIATION = """Refactor:
  - If the function has multiple stateful collaborators, extract a class and
    take them as constructor kwargs (same shape as GoldBuilder's
    llm_judge=, retriever=, db_path=).
  - If the function is a Protocol Adapter, declare the dependency at the
    Protocol level and inject the implementation at the boundary (factory).
  - If the parameter exists ONLY for test substitution, delete it and
    refactor the test to drive through the public surface that constructs
    the right collaborator."""


_ALLOW_FILE = REPO_ROOT / ".architecture" / "baseline" / "test-only-kwargs-allow.txt"


def _read_allow_list() -> set[str]:
    if not _ALLOW_FILE.exists():
        return set()
    return {line.strip() for line in _ALLOW_FILE.read_text().splitlines() if line.strip() and not line.startswith("#")}


def _is_none_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _qualified_param(module_path: str, func_name: str, param_name: str) -> str:
    return f"{module_path}::{func_name}::{param_name}"


def _module_path(path: Path) -> str:
    """Convert tests/integration/foo.py → kairix.integration.foo (best-effort)."""
    rel = path.resolve().relative_to(REPO_ROOT)
    return str(rel.with_suffix("")).replace("/", ".")


def file_has_violation(path: Path, allow: set[str]) -> bool:
    """True if ``path`` declares any function param OR dataclass field
    matching the ``*_fn=None`` shape, not on the allow-list.

    Walks two AST shapes:
      1. ``FunctionDef`` / ``AsyncFunctionDef`` — positional and keyword-only
         args whose default is the ``None`` constant.
      2. ``ClassDef`` body ``AnnAssign`` — annotated dataclass fields whose
         value is the ``None`` constant (e.g.
         ``x_fn: Callable | None = None`` inside a ``@dataclass`` class).
         This catches the F6-pattern that the v1 detector missed because
         dataclasses moved the per-param default off the function signature
         and onto class fields.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    module_path = _module_path(path)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.args
            defaults = args.defaults
            positional_with_default = list(
                zip(positional[len(positional) - len(defaults) :], defaults, strict=True)
            )
            kw_only = list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
            for arg, default in positional_with_default + kw_only:
                param_name = arg.arg
                if not param_name.endswith("_fn"):
                    continue
                if not _is_none_constant(default):
                    continue
                qualified = _qualified_param(module_path, node.name, param_name)
                if qualified in allow:
                    continue
                return True
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, ast.AnnAssign):
                    continue
                if not isinstance(item.target, ast.Name):
                    continue
                field_name = item.target.id
                if not field_name.endswith("_fn"):
                    continue
                if not _is_none_constant(item.value):
                    continue
                qualified = _qualified_param(module_path, node.name, field_name)
                if qualified in allow:
                    continue
                return True

    return False


def main() -> int:
    allow = _read_allow_list()
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p, allow)}
    return gate("no-test-only-kwargs", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
