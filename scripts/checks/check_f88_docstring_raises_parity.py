"""F88: docstring-``Raises:`` parity at the calling tier.

Motivation (EPIC #499 Phase 1; the session-escape-5 class)
----------------------------------------------------------
``KairixSetupService.save_source`` grew a documented ``Raises:
ValueError`` carrying F21-shaped operator guidance — but the wizard
route that calls it (``_handle_folder_save`` in
``kairix/platform/setup/web/routes.py``) caught only ``OSError``. The
moment a relative-path pick reached the service, that carefully written
guidance surfaced as a raw HTTP 500 mid-wizard. The docstring promised a
recoverable, operator-facing failure; the calling tier had no handler
for it. F88 makes that contract structural: a documented exception is a
promise the caller must keep — either it HANDLES the type, or a test
PROVES the type is exercised through the calling tier.

What F88 harvests (a "documented Raises type")
----------------------------------------------
A method on the setup service contract OR its production backend —
``SetupService`` in ``kairix/platform/setup/service.py`` and
``KairixSetupService`` in ``kairix/platform/setup/backends.py`` — whose
Google-style docstring carries a ``Raises:`` section naming a CONCRETE
exception type. "Concrete" means a resolvable class name: a built-in
(``ValueError`` / ``OSError`` / ...) or an exception class imported /
defined in the setup tree (``SecretsWriteError``). A bare prose
"may raise" with no ``Raises:`` block, or a ``Raises:`` line naming
something that does not resolve to a class, is deliberately skipped —
precision over recall (see "Intentionally NOT caught").

The parity convention (document of record)
------------------------------------------
For each documented type ``T`` on method ``M``, where ``M`` is CALLED in
the declared caller module ``kairix/platform/setup/web/routes.py``, the
contract is satisfied when EITHER:

  1. **Handled** — the caller module has an ``except`` clause naming
     ``T`` (or a superclass of ``T`` on the resolvable class path). A
     ``SecretsWriteError`` documented Raises is satisfied by either an
     ``except SecretsWriteError`` or an ``except OSError`` (its base),
     because the broader handler catches it before it escapes. Built-in
     superclasses resolve through Python's real exception MRO; setup-tree
     subclasses resolve through the ``class X(Base)`` bases parsed from
     the setup ASTs.
  2. **Render-tested** — some test module under ``tests/platform/setup/``
     references ``T`` by name (a raises-injection test that drives the
     service to raise ``T`` and asserts the route renders the rescue
     instead of a 500). Module-level granularity: the test module names
     the exception type somewhere; review catches insincere references.

A documented type that is NEITHER handled NOR render-tested is the
violation — the exact shape of session-escape 5.

Caller resolution
-----------------
A method ``M`` "has a caller in the declared set" when the caller module
contains a ``something.M(...)`` call (an attribute call whose attribute
is the method name — the wizard always calls through the injected
``service`` object, so an attribute-name match is the conservative,
correct signal). Methods with NO such call site are skipped: the rule
only governs the declared Protocol+caller pair, never a method the
wizard does not reach.

Generalisation path (NOT implemented in v1 — documented intent)
---------------------------------------------------------------
The same parity holds for any (Protocol, declared-caller-module) pair:
a connector Protocol's documented ``Raises:`` against the worker that
drives it, a provider Protocol's against the pipeline. v1 keeps the
scope TIGHT to the one service+route pair the session-escape-5 incident
exercised — false positives on a half-mapped caller graph would teach
agents to distrust the gate. Phase 2 lifts the ``(SERVICE_MODULES,
CALLER_MODULE, TEST_DIR)`` triple into a registry of such pairs once the
single-pair shape has soaked.

Intentionally NOT caught (precision over recall)
------------------------------------------------
  * **Prose-only "Raises".** ``save_oauth_source``'s backend docstring
    says "``OSError`` from a read-only config propagates" in prose, with
    NO ``Raises:`` section — not harvested. A typed ``Raises:`` block is
    the contract; the parser keys on it, not on the word "raise".
  * **Non-concrete Raises.** A ``Raises:`` line whose type does not
    resolve to a built-in or a setup-tree class (a typo, a fully-dotted
    path the parser can't bind) is skipped, not flagged.
  * **Methods with no caller in the declared module.** ``tour_prep`` /
    ``status`` etc. are not governed unless ``routes.py`` calls them AND
    they carry a typed ``Raises:``.
  * **Whether the handler is on the SAME function that calls ``M``.**
    Module-level granularity — an ``except T`` anywhere in
    ``routes.py`` satisfies ``T``. The caller graph inside the module is
    small and reviewed; per-function except-scope analysis is data-flow,
    not a conservative detector.
  * **Other Protocol+caller pairs** (connectors, providers) — the
    generalisation above, deferred to Phase 2.
  * **Exception subclasses raised but never documented** — F88 is the
    docstring→handler direction only (a documented promise unkept), not
    the handler→docstring direction (an undocumented raise).

There is no grandfathering: every unhandled, untested documented Raises
blocks at pre-commit / safe-commit / CI Stage 0. The
violating file reported is the SERVICE module that carries the
unfulfilled docstring promise.
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# The declared (service contract + backend, caller, test-dir) triple. v1
# is deliberately single-pair — see the module docstring's generalisation
# note. Paths are repo-relative.
SERVICE_MODULES = (
    Path("kairix/platform/setup/service.py"),
    Path("kairix/platform/setup/backends.py"),
)
CALLER_MODULE = Path("kairix/platform/setup/web/routes.py")
TEST_DIR = Path("tests/platform/setup")

# The Google-style section header the docstring parser keys on.
_RAISES_HEADER = "Raises:"

REMEDIATION = """F88: a setup-service method documents a `Raises: <Type>` that the
calling tier (kairix/platform/setup/web/routes.py) neither handles nor
has a render test for — the session-escape-5 class where save_source
documented `Raises: ValueError` with F21-shaped guidance but the folder
route caught only OSError, so the guidance surfaced as a raw 500
mid-wizard.

fix: for each unfulfilled documented type printed above, do ONE of —
  (a) HANDLE it in the route that calls the method: add an
      `except <Type>:` (or a superclass) in
      kairix/platform/setup/web/routes.py that renders the operator
      rescue banner instead of letting the exception escape as a 500;
  (b) RENDER-TEST it: add a raises-injection test under
      tests/platform/setup/ that makes the service raise <Type> and
      asserts the route renders the rescue (reference <Type> by name in
      the test module);
  (c) if the docstring promise is wrong, correct the `Raises:` section
      so it names only types the method actually raises.
next: re-run python3 scripts/checks/check_f88_docstring_raises_parity.py
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "fix(setup): handle documented Raises at the wizard route (session-escape-5 class)"

Pass example: kairix/platform/setup/backends.py + routes.py
  # backends.py
  def save_source(self, path: str) -> None:
      \"\"\"Persist the chosen folder.

      Raises:
          ValueError: when the path is relative or missing.
      \"\"\"
      ...
  # routes.py — the caller HANDLES the documented type:
  try:
      service.save_source(path)
  except ValueError as exc:           # documented Raises is caught —
      return render(_TPL_FOLDER, ...)  # operator sees the rescue, not a 500
  except OSError as exc:
      return render(_TPL_FOLDER, ...)

Forbidden example:
  # backends.py grew a documented Raises:
  def save_source(self, path: str) -> None:
      \"\"\"... Raises: ValueError: when the path is relative. \"\"\"
      if not Path(path).is_absolute():
          raise ValueError("enter the full path ...")  # F21-shaped guidance
  # routes.py catches only OSError — ValueError escapes:
  try:
      service.save_source(path)
  except OSError as exc:              # ValueError is NOT caught here —
      return render(_TPL_FOLDER, ...)  # the guidance becomes a raw 500."""


def _parse(rel: Path, repo_root: Path) -> ast.Module | None:
    """AST-parse a repo-relative path; ``None`` if unreadable/unparseable."""
    path = repo_root / rel
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _builtin_exception(name: str) -> type | None:
    """The built-in exception class named ``name``, or ``None``.

    Only types that are actually ``BaseException`` subclasses qualify, so
    a ``Raises: Mapping`` typo (a real builtin name, not an exception)
    does not resolve as a concrete exception type.
    """
    obj = getattr(builtins, name, None)
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    return None


def _exception_class_bases(modules: list[ast.Module]) -> dict[str, set[str]]:
    """Map ``class X(Base, ...)`` → its direct base NAMES, across the setup
    ASTs. Feeds setup-tree superclass resolution (``SecretsWriteError`` →
    ``{"OSError"}``). Only simple ``Name`` bases are recorded — a dotted
    base (``module.Error``) is conservatively dropped.
    """
    bases: dict[str, set[str]] = {}
    for tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases[node.name] = {b.id for b in node.bases if isinstance(b, ast.Name)}
    return bases


def _superclass_names(name: str, class_bases: dict[str, set[str]]) -> frozenset[str]:
    """All names of ``name`` and its ancestors — built-in MRO plus the
    setup-tree ``class X(Base)`` graph.

    A setup-tree subclass (``SecretsWriteError``) walks its parsed bases
    transitively; once a base is a built-in exception, its full Python
    MRO names are folded in. Cycles (malformed source) are bounded by the
    visited set.
    """
    out: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in out:
            continue
        out.add(current)
        builtin_cls = _builtin_exception(current)
        if builtin_cls is not None:
            out.update(base.__name__ for base in builtin_cls.__mro__)
        stack.extend(class_bases.get(current, set()))
    return frozenset(out)


def _raises_types(docstring: str) -> list[str]:
    """The exception type names in a Google-style ``Raises:`` section.

    Parses the indented block under the ``Raises:`` header: each entry
    starts with ``<TypeName>:`` (the description follows the colon). The
    block ends at the next dedented non-blank line or another section
    header. Continuation lines (more-indented description text) are
    skipped. Only the leading identifier before the first ``:`` is taken
    as the type name.
    """
    lines = docstring.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _RAISES_HEADER), None)
    if start is None:
        return []
    header_indent = len(lines[start]) - len(lines[start].lstrip())
    types: list[str] = []
    entry_indent: int | None = None
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            break  # dedented to (or past) the header — section over.
        if entry_indent is None:
            entry_indent = indent
        if indent > entry_indent:
            continue  # continuation of the previous entry's description.
        head = line.strip().split(":", 1)[0].strip()
        # Take the bare leading identifier (``ValueError``); ignore
        # parameter-style or multi-token heads.
        token = head.split()[0] if head else ""
        if token.isidentifier():
            types.append(token)
    return types


def _documented_raises(
    modules: list[ast.Module],
    target_classes: frozenset[str],
    known_classes: frozenset[str],
) -> dict[str, set[str]]:
    """Map method name → set of concrete documented ``Raises:`` types.

    Walks methods of the target service classes across the setup ASTs.
    A type is "concrete" — and kept — when it resolves to a built-in
    exception OR a class defined in the setup tree (``known_classes``,
    which includes setup-tree exceptions like ``SecretsWriteError``). A
    type that resolves to neither (a typo, an unbound dotted path) is
    dropped (precision over recall).
    """
    documented: dict[str, set[str]] = {}
    for tree in modules:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in target_classes:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                doc = ast.get_docstring(item)
                if not doc:
                    continue
                for type_name in _raises_types(doc):
                    if _builtin_exception(type_name) is None and type_name not in known_classes:
                        continue  # non-concrete — skip per precision rule.
                    documented.setdefault(item.name, set()).add(type_name)
    return documented


def _called_method_names(tree: ast.Module) -> frozenset[str]:
    """Attribute-call names in the caller module (``service.save_source(...)``
    → ``save_source``) — every method the wizard route reaches."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return frozenset(out)


def _handled_exception_names(tree: ast.Module) -> frozenset[str]:
    """Exception NAMES the caller module catches in ``except`` clauses.

    Both ``except OSError:`` and ``except (A, B):`` tuple forms are
    harvested by terminal name (``Name`` → id, ``Attribute`` → attr).
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        for handler in node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]:
            if isinstance(handler, ast.Name):
                out.add(handler.id)
            elif isinstance(handler, ast.Attribute):
                out.add(handler.attr)
    return frozenset(out)


def _test_referenced_names(repo_root: Path) -> frozenset[str]:
    """Every name referenced (import / Name / Attribute) anywhere under the
    declared test dir — the render-test coverage surface."""
    out: set[str] = set()
    test_root = repo_root / TEST_DIR
    if not test_root.exists():
        return frozenset()
    for path in sorted(test_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path.relative_to(repo_root), repo_root)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                out.add(node.id)
            elif isinstance(node, ast.Attribute):
                out.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                out.update(alias.name for alias in node.names)
    return frozenset(out)


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Harvest documented Raises on the service contract+backend, resolve
    each against the caller's handlers (with superclass widening) and the
    test dir, print per-finding detail, and return the violating service
    files (repo-relative).
    """
    parsed = [(rel, tree) for rel in SERVICE_MODULES if (tree := _parse(rel, repo_root)) is not None]
    caller_tree = _parse(CALLER_MODULE, repo_root)
    if not parsed or caller_tree is None:
        return set()

    service_modules = [tree for _, tree in parsed]
    class_bases = _exception_class_bases(service_modules)
    # A setup-tree-defined class name counts as a concrete exception type
    # (``SecretsWriteError``); built-ins resolve independently. ``class_bases``
    # holds every ``class X(...)`` defined across the setup ASTs.
    known_classes = frozenset(class_bases)
    target_classes = frozenset({"SetupService", "KairixSetupService"})
    documented = _documented_raises(service_modules, target_classes, known_classes)

    called = _called_method_names(caller_tree)
    handled = _handled_exception_names(caller_tree)
    tested = _test_referenced_names(repo_root)

    # Resolve, per method, which SERVICE file declared each documented
    # type so the violation report points at the file carrying the
    # unfulfilled promise.
    declaring_files = _declaring_files(parsed, target_classes, documented)

    violations: set[Path] = set()
    for method in sorted(documented):
        if method not in called:
            continue  # no caller in the declared module — not governed.
        for type_name in sorted(documented[method]):
            ancestry = _superclass_names(type_name, class_bases)
            if handled & ancestry:
                continue  # caught by the type or a superclass on its path.
            if type_name in tested:
                continue  # render-tested through the declared test dir.
            rel = declaring_files.get((method, type_name), SERVICE_MODULES[0])
            violations.add(rel)
            print(
                f"  [f88] {rel}: method '{method}' documents 'Raises: {type_name}' but "
                f"{CALLER_MODULE} neither catches it (nor a superclass) nor has a "
                f"render test under {TEST_DIR}"
            )
    return violations


def _declaring_files(
    parsed: list[tuple[Path, ast.Module]],
    target_classes: frozenset[str],
    documented: dict[str, set[str]],
) -> dict[tuple[str, str], Path]:
    """Map ``(method, type)`` → the repo-relative SERVICE file whose
    docstring declared that ``Raises:`` type.

    A method documented in both ``service.py`` and ``backends.py`` records
    the first-seen declaring file per type; the report keys on it so the
    operator opens the file carrying the unfulfilled promise.
    """
    out: dict[tuple[str, str], Path] = {}
    for rel, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in target_classes:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name not in documented:
                    continue
                doc = ast.get_docstring(item) or ""
                for type_name in _raises_types(doc):
                    key = (item.name, type_name)
                    if key not in out and type_name in documented[item.name]:
                        out[key] = rel
    return out


def main() -> int:
    return gate("f88", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
