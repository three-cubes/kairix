"""F6: No ``*_fn=None`` / ``*_loader=None`` / ``*_factory=None`` etc. test-only
kwargs on production free functions.

Production functions with parameters named like ``search_fn``, ``chat_fn``,
``embed_fn``, ``store_loader``, ``backend_loader``, ``factory_loader``,
``builder_loader``, ``provider_factory``, ``creds_resolver`` etc. that
default to ``None`` are typically test-substitution seams added "just so
tests can swap behaviour." This is the smell that triggered the
``#113/#114`` reverts and the more recent
``kairix.use_cases.eval_suite._resolve_production_fact_store::store_loader``
revert: production grew complexity for tests without operator value.

The legitimate seam pattern is **constructor injection** at a boundary
class (e.g. ``GoldBuilder(llm_judge=, retriever=)``) or **Protocol
injection at a use case via a ``Deps`` dataclass with
``field(default_factory=...)``** — not a per-helper ``*_fn=None`` /
``*_loader=None`` parameter on a free function (especially not on a
private ``_`` -prefixed helper).

Detection (free functions only — methods on a ``ClassDef`` are exempt
because they ARE the canonical Deps-constructor shape):

  Any positional-with-default or keyword-only parameter whose name ends
  in one of ``_TEST_SEAM_SUFFIXES`` AND whose default is the ``None``
  constant. Plus the same shape on ``@dataclass`` field annotations
  (``AnnAssign`` inside a ``ClassDef`` body, target = ``None``).

Severity:

- **Free function (public or ``_``-prefixed private)** — every matching
  parameter is flagged. There is no allow-list and no grandfathering
  (PLA-472): a seam a real production caller needs moves onto a Deps
  class; a seam only tests use is deleted.
- **Dataclass field** — every ``ClassDef``-level ``AnnAssign`` with a
  test-seam suffix and a ``None`` default is flagged (qualified
  ``module.path::ClassName::field_name``); use
  ``field(default_factory=...)`` instead.
- **Methods on a ``ClassDef``** — NEVER flagged. Constructor / method
  injection on a class is the canonical Deps shape; if the smell is
  "test-only kwargs on a Deps class" the right gate is "is the class
  shape itself a Deps dataclass" — out of scope for F6.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate, python_files, repo_relative

# Suffix set extended beyond ``_fn`` to catch the broader class of
# test-injection seams: ``_loader`` / ``_factory`` / ``_builder`` /
# ``_provider`` / ``_resolver`` — every shape the revert audit found
# being used to slip past the original ``_fn``-only check.
_TEST_SEAM_SUFFIXES: tuple[str, ...] = (
    "_fn",
    "_loader",
    "_factory",
    "_builder",
    "_provider",
    "_resolver",
)


REMEDIATION = """Refactor to a dataclass with ``field(default_factory=...)``
on a Deps class — the canonical shape is ``kairix/worker.py::WorkerDeps``
— to pass. The smell is *test-only kwargs on a free function*, especially
on a private ``_``-prefixed helper.

fix: delete the ``*_fn`` / ``*_loader`` / ``*_factory`` / ``*_builder`` /
``*_provider`` / ``*_resolver`` parameter and move the collaborator onto
a ``@dataclass`` Deps class with ``field(default_factory=...)``; tests
construct an overridden Deps and pass it as a single argument. See
``kairix/worker.py::WorkerDeps`` for the canonical shape.
next: re-run ``python3 scripts/checks/check_no_test_only_kwargs.py``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(<area>): replace test-only kwargs with Deps class"

The legitimate seam is **constructor injection on a Deps class**, NOT a
per-helper ``_fn=None`` / ``_loader=None`` / ``_factory=None`` parameter
on a free function. Tests pass an overridden Deps; production gets the
default factory. Private (``_``-prefixed) free functions carry the worst
shape of the smell. There is no allow-list: every flagged parameter is
refactored at source.

Pass example:
  # kairix/worker.py
  @dataclass
  class WorkerDeps:
      embed: Callable[[], Any] = field(default_factory=lambda: _default_embed)
      sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)

  def run_worker(deps: WorkerDeps | None = None) -> None:
      deps = deps or WorkerDeps()
      deps.embed()

  # in a test
  fake = WorkerDeps(embed=lambda: 'fake-embed', sleep=lambda _: None)
  run_worker(deps=fake)

Forbidden example:
  def _resolve_production_thing(*, thing_loader=None) -> Thing:
      loader = thing_loader if thing_loader is not None else _default_loader
      return loader()()

  # public callers always pass None; the only non-default caller is a test.

If the parameter exists ONLY for test substitution, delete it and
refactor the test to drive through the public surface that constructs
the right collaborator (or use ``pragma: no cover`` if the branch is
genuinely defensive-and-unreachable, like a Cap-subpackage ImportError
rewrap on a sibling-subpackage that ships with every kairix install)."""


def _is_none_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _qualified_param(module_path: str, func_name: str, param_name: str) -> str:
    return f"{module_path}::{func_name}::{param_name}"


def _module_path(path: Path) -> str:
    """Convert tests/integration/foo.py → kairix.integration.foo (best-effort).

    Falls back to the literal stem when ``path`` lives outside ``REPO_ROOT``
    — happens in unit tests that write sample modules to ``tmp_path``.
    The qualified-param string is still well-formed; only the leading
    module-path prefix differs from the production-walk emission shape.
    """
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(REPO_ROOT)
    except ValueError:
        # ``tmp_path`` source — emit a synthetic dotted path so the
        # detector still produces a qualified ``...::func::param`` form
        # the tests can assert on.
        return str(resolved.with_suffix("")).replace("/", ".").lstrip(".")
    return str(rel.with_suffix("")).replace("/", ".")


def _has_test_seam_suffix(name: str) -> bool:
    """True iff ``name`` ends in one of the test-seam suffixes."""
    return any(name.endswith(suffix) for suffix in _TEST_SEAM_SUFFIXES)


def _free_function_violations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module_path: str,
) -> list[str]:
    """Return the list of qualified param names violating F6 on this free function.

    Every matching param is flagged — public and private hosts alike; there
    is no allow-list.
    """
    args = node.args
    positional = args.args
    defaults = args.defaults
    positional_with_default = list(zip(positional[len(positional) - len(defaults) :], defaults, strict=True))
    kw_only = list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    return [
        _qualified_param(module_path, node.name, arg.arg)
        for arg, default in positional_with_default + kw_only
        if _has_test_seam_suffix(arg.arg) and _is_none_constant(default)
    ]


def _class_field_violations(node: ast.ClassDef, module_path: str) -> list[str]:
    """Return the list of qualified field names violating F6 on this class.

    Targets the dataclass shape:
      ``thing_loader: Callable | None = None``
    inside a class body. ``default_factory=...`` cases are safe because
    their value is a Call node, not the ``None`` constant. This is the
    canonical Deps shape and continues to pass.
    """
    return [
        _qualified_param(module_path, node.name, item.target.id)
        for item in node.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and _has_test_seam_suffix(item.target.id)
        and _is_none_constant(item.value)
    ]


def _iter_free_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return all free functions in ``tree`` — methods inside ``ClassDef`` excluded.

    A free function is a ``FunctionDef`` / ``AsyncFunctionDef`` whose
    enclosing scope is the module, another function, or a comprehension —
    NOT a class. Methods (including nested classes' methods) are exempt
    from F6 because constructor / method injection on a class IS the
    canonical Deps shape.

    Implementation: walk the AST and record parent relationships; emit
    only function nodes whose ancestor chain has no ``ClassDef``.
    """
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _has_class_ancestor(node, parents):
            continue
        out.append(node)
    return out


def _has_class_ancestor(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """True iff ``node`` is nested (at any depth) inside a ``ClassDef``."""
    current: ast.AST | None = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.ClassDef):
            return True
        current = parents.get(id(current))
    return False


def _iter_classdefs(tree: ast.AST) -> list[ast.ClassDef]:
    """Return all ``ClassDef`` nodes anywhere in ``tree`` (including nested)."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def file_violations(path: Path) -> list[str]:
    """Return every F6 violation in ``path`` as a list of qualified names.

    Walks two AST shapes:
      1. ``FunctionDef`` / ``AsyncFunctionDef`` AT MODULE / NESTED-FUNCTION
         SCOPE (i.e. *free functions*) — positional and keyword-only args
         whose default is the ``None`` constant. Methods inside a
         ``ClassDef`` are exempt: constructor / method injection on a
         class IS the canonical Deps shape (F6 is about test-only kwargs
         on free helpers, not the Deps pattern itself).
      2. ``ClassDef`` body ``AnnAssign`` — annotated dataclass fields
         whose value is the ``None`` constant (e.g.
         ``x_loader: Callable | None = None`` inside a ``@dataclass``
         class). ``default_factory=...`` cases stay safe because their
         value is a Call node, not None.

    Severity rule:
      Every matching free-function param / class field is flagged. There
      is no allow-list and no grandfathering.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    module_path = _module_path(path)
    violations: list[str] = []
    for fn in _iter_free_functions(tree):
        violations.extend(_free_function_violations(fn, module_path))
    for cls in _iter_classdefs(tree):
        violations.extend(_class_field_violations(cls, module_path))
    return violations


def file_has_violation(path: Path) -> bool:
    """True iff ``path`` has at least one F6 violation. Kept as a thin
    boolean wrapper around :func:`file_violations` for callers that only
    need a yes/no answer; the detailed-list form drives the test suite
    and the per-param ``main()`` reporter.
    """
    return bool(file_violations(path))


def main() -> int:
    violations_by_file: dict[Path, list[str]] = {}
    for p in python_files("kairix"):
        v = file_violations(p)
        if v:
            violations_by_file[repo_relative(p)] = v

    # gate() reports per file; emit the per-param detail first so the
    # operator reading the failure sees exactly which params triggered it.
    if violations_by_file:
        print("F6 — test-only kwargs detected (per-param detail):")
        for path in sorted(violations_by_file):
            for qualified in violations_by_file[path]:
                print(f"  {path}: {qualified}")
        print()
    return gate("no-test-only-kwargs", set(violations_by_file), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
