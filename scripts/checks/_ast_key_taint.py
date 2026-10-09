"""Shared AST helpers for the F1 / F2 detectors: index a module, resolve constants.

F1 (``sys.modules`` / kairix internals) and F2 (``os.environ`` / ``KAIRIX_*``)
are DEFAULT-DENY: a write is allowed only when its key (or patch target) can
be PROVEN, statically, not to be protected. This module supplies the two
pieces that proof needs:

* :class:`ModuleIndex` — one traversal per file that collects parents,
  imports, every name binding (with its kind: a value, a loop element, a
  helper's return, or unknown), every ``Load`` name reference, the
  attribute references the detectors care about, and the candidate
  statements. No query re-walks the tree.
* :class:`ConstantTable` — the set of string values an expression can take,
  when that set is statically known: literals, ``+`` / f-string / ``%`` /
  ``.format`` folding, names bound only to such values (through any number
  of hops), loop variables over constant containers, constant-container
  indexing (``KEYS[0]`` / ``KEYS[i]``), conditional expressions, and
  helpers that only ``return`` such values. Anything else is UNKNOWN — and
  under default-deny an unknown key is treated as protected.

:class:`ProtectedKeys` decides a known value; for a value whose constant
LEADING prefix is all that is known (``"KAIRIX" + suffix``), it decides
whether that prefix could still complete to a protected key.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# What counts as a protected key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedKeys:
    """A protected key namespace: exact names and/or required prefixes.

    ``keys(value)`` answers for a COMPLETE value. :meth:`could_complete`
    answers for a known leading prefix of a partially constant value: can
    some completion of it be protected?
    """

    prefixes: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()

    def __call__(self, value: str) -> bool:
        return value in self.exact or value.startswith(self.prefixes)

    def could_complete(self, prefix: str) -> bool:
        if not prefix:
            return True
        return any(p.startswith(prefix) or prefix.startswith(p) for p in (*self.prefixes, *self.exact))


# ---------------------------------------------------------------------------
# One-pass module index.
# ---------------------------------------------------------------------------

_CANDIDATE_TYPES = (ast.Call, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)
#: every construct that can bind a name to a value (aliasing sites)
BINDING_SITE_TYPES = (
    ast.Assign,
    ast.AnnAssign,
    ast.NamedExpr,
    ast.Return,
    ast.Yield,
    ast.YieldFrom,
    ast.For,
    ast.AsyncFor,
    ast.comprehension,
    ast.With,
    ast.AsyncWith,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
)
_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
#: attribute names whose references the detectors classify
INDEXED_ATTRIBUTES = frozenset({"environ", "modules", "reload", "__import__"})

#: binding kinds recorded per name
VALUE, ELEMENT, RETURN, PARAM, UNKNOWN = "value", "element", "return", "param", "unknown"
#: stdlib factories whose ``.name`` is their first (``name``) argument
_SPEC_FACTORIES = frozenset({"spec_from_file_location", "spec_from_loader", "ModuleSpec"})


def _target_names(target: ast.expr) -> list[str]:
    """Every plain ``Name`` bound by an assignment / loop target."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


#: AST nodes that open a new lexical scope
_SCOPE_NODES = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

#: one name binding: ("import", "os.path") for ``import os.path`` (binds ``os``),
#: ("import_as", "os") for ``import os as o``, ("from", "os", "environ"), ("other",)
Binding = tuple[str, ...]
_OTHER: Binding = ("other",)


@dataclass
class ScopeInfo:
    """The names one lexical scope binds (Python: bound anywhere ⇒ local throughout)."""

    node: ast.AST
    parent: ScopeInfo | None
    bindings: dict[str, list[Binding]] = field(default_factory=dict)
    #: every binding with the AST node that makes it (for line-accurate reports)
    sites: dict[str, list[tuple[Binding, ast.AST]]] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)

    @property
    def is_class(self) -> bool:
        return isinstance(self.node, ast.ClassDef)

    @property
    def is_module(self) -> bool:
        return self.parent is None

    def bind(self, name: str, binding: Binding, node: ast.AST) -> None:
        self.bindings.setdefault(name, []).append(binding)
        self.sites.setdefault(name, []).append((binding, node))


def _child_scope(node: ast.AST, child: ast.AST, scope: ScopeInfo, new: ScopeInfo | None) -> ScopeInfo:
    """The scope ``child`` (a direct child of ``node``) evaluates in."""
    if new is None:
        return scope
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inner = child is node.args or any(child is stmt for stmt in node.body)
        return new if inner else scope
    if isinstance(node, ast.Lambda):
        return new if (child is node.args or child is node.body) else scope
    if isinstance(node, ast.ClassDef):
        return new if any(child is stmt for stmt in node.body) else scope
    # comprehensions: everything is inside the new scope, except the first
    # generator's iterable (handled at the ``comprehension`` node)
    return new


@dataclass
class ModuleIndex:
    """Everything the F1 / F2 queries need, collected in one traversal."""

    tree: ast.AST
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    imports: list[ast.Import] = field(default_factory=list)
    import_froms: list[ast.ImportFrom] = field(default_factory=list)
    #: plain ``name = value`` bindings (Assign / AnnAssign / walrus) for alias resolution
    name_bindings: list[tuple[str, ast.expr]] = field(default_factory=list)
    #: every binding of every name, with its kind (VALUE / ELEMENT / RETURN / UNKNOWN)
    value_bindings: dict[str, list[tuple[str, ast.expr | None]]] = field(default_factory=dict)
    #: every ``Load``-context name reference, by name
    name_loads: dict[str, list[ast.Name]] = field(default_factory=dict)
    #: references ``<x>.environ`` / ``<x>.modules`` / ``<x>.reload``, by attribute
    attributes: dict[str, list[ast.Attribute]] = field(default_factory=dict)
    #: ``with <ctx> as <name>`` targets
    with_targets: list[tuple[str, ast.expr]] = field(default_factory=list)
    #: function definitions by name (for parameter propagation)
    functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = field(default_factory=dict)
    #: parameter name -> (owning function, positional index or None for keyword-only)
    params: dict[str, list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, int | None]]] = field(default_factory=dict)
    #: statements / calls that can write a mapping or patch an attribute
    candidates: list[ast.AST] = field(default_factory=list)
    #: every ``x[key]`` expression (dynamic lookups of guarded names)
    subscripts: list[ast.Subscript] = field(default_factory=list)
    #: every binding site (assignment, walrus, return / yield, loop / with target, defaults)
    binding_sites: list[ast.AST] = field(default_factory=list)
    #: the lexical scope every ``Name`` node is evaluated in (by node id)
    name_scope: dict[int, ScopeInfo] = field(default_factory=dict)
    module_scope: ScopeInfo | None = None
    #: every lexical scope in the file (module first)
    scopes: list[ScopeInfo] = field(default_factory=list)

    @classmethod
    def build(cls, tree: ast.AST) -> ModuleIndex:
        index = cls(tree)
        returns: list[ast.Return] = []
        root = ScopeInfo(tree, None)
        index.module_scope = root
        index.scopes.append(root)
        stack: list[tuple[ast.AST, ScopeInfo]] = [(tree, root)]
        while stack:
            node, scope = stack.pop()
            new = ScopeInfo(node, scope) if isinstance(node, _SCOPE_NODES) and node is not tree else None
            if new is not None:
                index.scopes.append(new)
            for child in ast.iter_child_nodes(node):
                index.parents[child] = node
                stack.append((child, index._scope_for_child(node, child, scope, new)))
            index._record_scope(node, scope)
            index._classify(node, returns)
        for ret in returns:
            fn = enclosing_function(index.parents, ret)
            if fn is not None:
                index._bind(fn.name, RETURN if ret.value is not None else UNKNOWN, ret.value)
        index._hoist_globals()
        return index

    def _hoist_globals(self) -> None:
        """A binding of a name declared ``global`` in its scope rebinds the
        MODULE-level name — move it there (after traversal: the stack walk does
        not visit a ``global`` statement before the stores it governs)."""
        root = self.module_scope
        if root is None:
            return
        for scope in self.scopes[1:]:
            for name in scope.globals:
                for binding, node in scope.sites.pop(name, []):
                    root.bind(name, binding, node)
                scope.bindings.pop(name, None)

    @staticmethod
    def _scope_for_child(node: ast.AST, child: ast.AST, scope: ScopeInfo, new: ScopeInfo | None) -> ScopeInfo:
        if isinstance(node, ast.arguments):
            # parameters belong to the function; their DEFAULTS are evaluated outside it
            outer = scope.parent if scope.parent is not None else scope
            if any(child is d for d in (*node.defaults, *node.kw_defaults)):
                return outer
            return scope
        if isinstance(node, ast.comprehension):
            # the FIRST generator's iterable is evaluated in the enclosing scope
            owner = scope.node
            first = isinstance(owner, _COMPREHENSIONS) and owner.generators and owner.generators[0] is node
            if first and child is node.iter and scope.parent is not None:
                return scope.parent
            return scope
        return _child_scope(node, child, scope, new)

    def _record_scope(self, node: ast.AST, scope: ScopeInfo) -> None:
        """Record which scope binds what, and the scope every Name is read in
        (one dict lookup per node — the index walks every node of every file)."""
        handler = _SCOPE_RECORDERS.get(type(node))
        if handler is not None:
            handler(self, node, scope)

    def _scope_name(self, node: ast.Name, scope: ScopeInfo) -> None:
        self.name_scope[id(node)] = scope
        if isinstance(node.ctx, ast.Load):
            return
        # ``environ |= {...}`` rebinds the name to the SAME mapping (in-place
        # ``__ior__``), so an augmented-assignment target never shadows it
        parent = self.parents.get(node)
        if not (isinstance(parent, ast.AugAssign) and parent.target is node):
            scope.bind(node.id, _OTHER, node)

    @staticmethod
    def _scope_arg(node: ast.arg, scope: ScopeInfo) -> None:
        scope.bind(node.arg, _OTHER, node)

    @staticmethod
    def _scope_import(node: ast.Import, scope: ScopeInfo) -> None:
        for alias in node.names:
            if alias.asname:
                scope.bind(alias.asname, ("import_as", alias.name), node)
            else:
                scope.bind(alias.name.split(".")[0], ("import", alias.name), node)

    @staticmethod
    def _scope_import_from(node: ast.ImportFrom, scope: ScopeInfo) -> None:
        for alias in node.names:
            scope.bind(alias.asname or alias.name, ("from", node.module or "", alias.name), node)

    @staticmethod
    def _scope_named(node: ast.AST, scope: ScopeInfo) -> None:
        """def / class / except-as / match capture: binds ``node.name`` (if any)."""
        name = getattr(node, "name", None)
        if name:
            scope.bind(name, _OTHER, node)

    @staticmethod
    def _scope_match_mapping(node: ast.MatchMapping, scope: ScopeInfo) -> None:
        if node.rest:
            scope.bind(node.rest, _OTHER, node)

    @staticmethod
    def _scope_global(node: ast.Global, scope: ScopeInfo) -> None:
        scope.globals.update(node.names)

    @staticmethod
    def _scope_nonlocal(node: ast.Nonlocal, scope: ScopeInfo) -> None:
        scope.nonlocals.update(node.names)

    # -- scope-aware resolution -------------------------------------------

    def resolve(self, name: ast.Name) -> list[Binding] | None:
        """The bindings ``name`` resolves to under Python's scoping rules, or
        ``None`` when nothing in the file binds it (a builtin / unbound name).

        Innermost scope outward; a class scope applies only to code directly in
        its body; ``global`` jumps to the module scope; ``nonlocal`` skips to the
        enclosing function. A name bound anywhere in a scope is local to all of it.
        """
        scope = self.name_scope.get(id(name))
        first = True
        while scope is not None:
            if name.id in scope.globals and scope.node is not self.tree:
                scope = self.module_scope
                first = False
                continue
            if scope.is_class and not first:
                scope = scope.parent
                continue
            if name.id in scope.bindings and name.id not in scope.nonlocals:
                return scope.bindings[name.id]
            scope = scope.parent
            first = False
        return None

    def resolves_to_module(self, name: ast.expr, module: str) -> bool:
        """``name`` is the stdlib ``module`` object: EVERY binding it resolves to is
        an import of that module (a parameter / assignment / def anywhere in the
        resolving scope — including a later module-level rebind — shadows it)."""
        if not isinstance(name, ast.Name):
            return False
        bindings = self.resolve(name)
        if not bindings:
            return False
        return all(
            (kind[0] == "import" and (kind[1] == module or kind[1].startswith(f"{module}.")))
            or (kind[0] == "import_as" and kind[1] == module)
            for kind in bindings
        )

    def resolves_to_from(self, name: ast.expr, module: str, attr: str) -> bool:
        """``name`` is ``attr`` imported ``from module`` (every binding, unshadowed)."""
        if not isinstance(name, ast.Name):
            return False
        bindings = self.resolve(name)
        if not bindings:
            return False
        return all(kind == ("from", module, attr) for kind in bindings)

    def is_builtin(self, name: ast.expr) -> bool:
        """``name`` is an un-shadowed builtin (nothing in the file binds it)."""
        return isinstance(name, ast.Name) and self.resolve(name) is None

    def is_builtin_ref(self, name: ast.expr) -> bool:
        """``name`` is the builtin of that name: un-shadowed, or bound only by
        ``from builtins import <name>``."""
        if not isinstance(name, ast.Name):
            return False
        return self.is_builtin(name) or self.resolves_to_from(name, "builtins", name.id)

    def _bind(self, name: str, kind: str, source: ast.expr | None) -> None:
        self.value_bindings.setdefault(name, []).append((kind, source))

    def _bind_target(self, target: ast.expr, value: ast.expr | None, kind: str) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, kind, value)
            return
        if (
            kind == VALUE
            and isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
            and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
        ):
            for sub_target, sub_value in zip(target.elts, value.elts, strict=True):
                self._bind_target(sub_target, sub_value, VALUE)
            return
        if (
            kind == ELEMENT
            and isinstance(target, (ast.Tuple, ast.List))
            and not any(isinstance(e, ast.Starred) for e in target.elts)
        ):
            # ``for name, sub in (("HOME", "home"), ...)`` — position i of each element
            for position, sub_target in enumerate(target.elts):
                if isinstance(sub_target, ast.Name):
                    self._bind(sub_target.id, f"{ELEMENT}:{position}", value)
                else:
                    for name in _target_names(sub_target):
                        self._bind(name, UNKNOWN, None)
            return
        for name in _target_names(target):
            self._bind(name, UNKNOWN, None)

    def _classify(self, node: ast.AST, returns: list[ast.Return]) -> None:
        if isinstance(node, _CANDIDATE_TYPES):
            self.candidates.append(node)
        if isinstance(node, BINDING_SITE_TYPES):
            self.binding_sites.append(node)
        handler = _CLASSIFIERS.get(type(node))
        if handler is not None:
            handler(self, node, returns)

    def _on_name(self, node: ast.Name, _returns: list[ast.Return]) -> None:
        if isinstance(node.ctx, ast.Load):
            self.name_loads.setdefault(node.id, []).append(node)

    def _on_attribute(self, node: ast.Attribute, _returns: list[ast.Return]) -> None:
        if node.attr in INDEXED_ATTRIBUTES:
            self.attributes.setdefault(node.attr, []).append(node)

    def _on_subscript(self, node: ast.Subscript, _returns: list[ast.Return]) -> None:
        self.subscripts.append(node)

    def _on_import(self, node: ast.Import, _returns: list[ast.Return]) -> None:
        self.imports.append(node)
        for alias in node.names:
            self._bind(alias.asname or alias.name.split(".")[0], UNKNOWN, None)

    def _on_import_from(self, node: ast.ImportFrom, _returns: list[ast.Return]) -> None:
        self.import_froms.append(node)
        for alias in node.names:
            self._bind(alias.asname or alias.name, UNKNOWN, None)

    def _on_assign(self, node: ast.Assign, _returns: list[ast.Return]) -> None:
        for target in node.targets:
            self._bind_target(target, node.value, VALUE)
        self.name_bindings.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))

    def _on_value_binding(self, node: ast.AnnAssign | ast.NamedExpr, _returns: list[ast.Return]) -> None:
        if node.value is None:
            return
        self._bind_target(node.target, node.value, VALUE)
        if isinstance(node.target, ast.Name):
            self.name_bindings.append((node.target.id, node.value))

    def _on_aug_assign(self, node: ast.AugAssign, _returns: list[ast.Return]) -> None:
        self._bind_target(node.target, None, UNKNOWN)

    def _on_loop(self, node: ast.For | ast.AsyncFor | ast.comprehension, _returns: list[ast.Return]) -> None:
        self._bind_target(node.target, node.iter, ELEMENT)

    def _on_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, _returns: list[ast.Return]) -> None:
        args = node.args
        if isinstance(node, ast.Lambda):
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if arg is not None:
                    self._bind(arg.arg, UNKNOWN, None)
            return
        self.functions.setdefault(node.name, []).append(node)
        for position, arg in enumerate((*args.posonlyargs, *args.args)):
            self._bind(arg.arg, PARAM, None)
            self.params.setdefault(arg.arg, []).append((node, position))
        for arg in args.kwonlyargs:
            self._bind(arg.arg, PARAM, None)
            self.params.setdefault(arg.arg, []).append((node, None))
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._bind(arg.arg, UNKNOWN, None)

    def _on_return(self, node: ast.Return, returns: list[ast.Return]) -> None:
        returns.append(node)

    def _on_with(self, node: ast.With | ast.AsyncWith, _returns: list[ast.Return]) -> None:
        for item in node.items:
            if item.optional_vars is None:
                continue
            self._bind_target(item.optional_vars, None, UNKNOWN)
            if isinstance(item.optional_vars, ast.Name):
                self.with_targets.append((item.optional_vars.id, item.context_expr))

    def _on_except(self, node: ast.ExceptHandler, _returns: list[ast.Return]) -> None:
        if node.name:
            self._bind(node.name, UNKNOWN, None)

    def module_aliases(self, module: str) -> set[str]:
        """Local names bound to stdlib ``module`` (``import os`` / ``import os as _os``)."""
        return {a.asname or a.name for node in self.imports for a in node.names if a.name == module}

    def from_imports(self, module: str, attr: str) -> set[str]:
        """Local names bound by ``from <module> import <attr> [as x]``."""
        return {
            a.asname or a.name
            for node in self.import_froms
            if node.module == module
            for a in node.names
            if a.name == attr
        }

    def statement_of(self, node: ast.AST) -> ast.AST:
        """The innermost statement containing ``node`` (``node`` itself if a statement)."""
        current: ast.AST | None = node
        while current is not None and not isinstance(current, ast.stmt):
            current = self.parents.get(current)
        return current if current is not None else node


def _static_recorder(fn: Callable[[Any, ScopeInfo], None]) -> Callable[[ModuleIndex, Any, ScopeInfo], None]:
    return lambda _index, node, scope: fn(node, scope)


_SCOPE_RECORDERS: dict[type, Callable[[ModuleIndex, Any, ScopeInfo], None]] = {
    ast.Name: ModuleIndex._scope_name,
    ast.arg: _static_recorder(ModuleIndex._scope_arg),
    ast.Import: _static_recorder(ModuleIndex._scope_import),
    ast.ImportFrom: _static_recorder(ModuleIndex._scope_import_from),
    ast.FunctionDef: _static_recorder(ModuleIndex._scope_named),
    ast.AsyncFunctionDef: _static_recorder(ModuleIndex._scope_named),
    ast.ClassDef: _static_recorder(ModuleIndex._scope_named),
    ast.ExceptHandler: _static_recorder(ModuleIndex._scope_named),
    ast.MatchAs: _static_recorder(ModuleIndex._scope_named),
    ast.MatchStar: _static_recorder(ModuleIndex._scope_named),
    ast.MatchMapping: _static_recorder(ModuleIndex._scope_match_mapping),
    ast.Global: _static_recorder(ModuleIndex._scope_global),
    ast.Nonlocal: _static_recorder(ModuleIndex._scope_nonlocal),
}

_CLASSIFIERS: dict[type, Callable[[ModuleIndex, Any, list[ast.Return]], None]] = {
    ast.Name: ModuleIndex._on_name,
    ast.Attribute: ModuleIndex._on_attribute,
    ast.Import: ModuleIndex._on_import,
    ast.ImportFrom: ModuleIndex._on_import_from,
    ast.Assign: ModuleIndex._on_assign,
    ast.AnnAssign: ModuleIndex._on_value_binding,
    ast.NamedExpr: ModuleIndex._on_value_binding,
    ast.AugAssign: ModuleIndex._on_aug_assign,
    ast.For: ModuleIndex._on_loop,
    ast.AsyncFor: ModuleIndex._on_loop,
    ast.comprehension: ModuleIndex._on_loop,
    ast.FunctionDef: ModuleIndex._on_scope,
    ast.AsyncFunctionDef: ModuleIndex._on_scope,
    ast.Lambda: ModuleIndex._on_scope,
    ast.Return: ModuleIndex._on_return,
    ast.With: ModuleIndex._on_with,
    ast.AsyncWith: ModuleIndex._on_with,
    ast.ExceptHandler: ModuleIndex._on_except,
    ast.Subscript: ModuleIndex._on_subscript,
}

# Constant folding.
# ---------------------------------------------------------------------------


def _literal(expr: ast.expr) -> object:
    try:
        return ast.literal_eval(expr)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _NOT_LITERAL


_NOT_LITERAL = object()


def fold_string(expr: ast.expr) -> tuple[str, bool] | None:
    """Fold a constant string expression.

    Returns ``(text, complete)``: ``complete`` is True when the whole value is
    known; otherwise ``text`` is the constant LEADING prefix of the value.
    ``None`` when nothing is known (a bare name, a call, ...). Folds string
    literals, ``+`` concatenation, f-strings (constant parts and constant
    ``{...}`` fields), and ``%`` / ``.format`` applied to a constant template
    with literal arguments.
    """
    if isinstance(expr, ast.Constant):
        return (expr.value, True) if isinstance(expr.value, str) else None
    if isinstance(expr, ast.JoinedStr):
        return _fold_joined(expr)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _fold_concat(expr)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        return _fold_template(expr.left, lambda tpl: tpl % _require_literal(expr.right), "%")
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "format":
        return _fold_format(expr)
    return None


def _require_literal(expr: ast.expr) -> object:
    value = _literal(expr)
    if value is _NOT_LITERAL:
        raise ValueError("not a literal")
    return value


def _fold_joined(expr: ast.JoinedStr) -> tuple[str, bool] | None:
    text = ""
    for part in expr.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            text += part.value
            continue
        if isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion in (-1, ord("s")):
            inner = fold_string(part.value)
            if inner is not None and inner[1]:
                text += inner[0]
                continue
            return (text + inner[0], False) if inner is not None else ((text, False) if text else None)
        return (text, False) if text else None
    return (text, True)


def _fold_concat(expr: ast.BinOp) -> tuple[str, bool] | None:
    left = fold_string(expr.left)
    if left is None or not left[1]:
        return left
    right = fold_string(expr.right)
    if right is None:
        return (left[0], False)
    return (left[0] + right[0], right[1])


def _fold_template(template_expr: ast.expr, apply: Callable[[str], object], marker: str) -> tuple[str, bool] | None:
    template = fold_string(template_expr)
    if template is None or not template[1]:
        return template
    try:
        result = apply(template[0])
    except (ValueError, TypeError, KeyError, IndexError):
        return (template[0].split(marker, 1)[0], False)
    return (result, True) if isinstance(result, str) else None


def _fold_format(expr: ast.Call) -> tuple[str, bool] | None:
    def apply(tpl: str) -> str:
        args = [_require_literal(a) for a in expr.args]
        kwargs = {kw.arg: _require_literal(kw.value) for kw in expr.keywords if kw.arg is not None}
        if any(kw.arg is None for kw in expr.keywords):
            raise ValueError("** spread")
        return tpl.format(*args, **kwargs)

    assert isinstance(expr.func, ast.Attribute)
    return _fold_template(expr.func.value, apply, "{")


# ---------------------------------------------------------------------------
# Constant table — the statically known string values of an expression.
# ---------------------------------------------------------------------------

#: cap on a value set (a product of two large unions is "unknown", not a hang)
_MAX_VALUES = 256


class ConstantTable:
    """The set of string values an expression can take, when statically known."""

    def __init__(self, index: ModuleIndex) -> None:
        self.index = index
        self._names: dict[str, frozenset[str] | None] = {}
        self._in_progress: set[str] = set()

    # -- strings ------------------------------------------------------------

    def strings(self, expr: ast.expr | None) -> frozenset[str] | None:
        """Every string ``expr`` can evaluate to, or ``None`` when unknown."""
        if expr is None:
            return None
        handler = _STRING_HANDLERS.get(type(expr))
        if handler is not None:
            return handler(self, expr)
        folded = fold_string(expr)
        return frozenset({folded[0]}) if folded is not None and folded[1] else None

    def _constant(self, expr: ast.Constant) -> frozenset[str] | None:
        return frozenset({expr.value}) if isinstance(expr.value, str) else None

    def _name(self, expr: ast.Name) -> frozenset[str] | None:
        return self.name_strings(expr.id)

    def _concat(self, expr: ast.BinOp) -> frozenset[str] | None:
        if isinstance(expr.op, ast.Add):
            return _product(self.strings(expr.left), self.strings(expr.right))
        folded = fold_string(expr)
        return frozenset({folded[0]}) if folded is not None and folded[1] else None

    def _joined(self, expr: ast.JoinedStr) -> frozenset[str] | None:
        values: frozenset[str] | None = frozenset({""})
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                values = _product(values, frozenset({part.value}))
            elif isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion in (-1, 115):
                values = _product(values, self.strings(part.value))
            else:
                return None
        return values

    def _if_exp(self, expr: ast.IfExp) -> frozenset[str] | None:
        return _union(self.strings(expr.body), self.strings(expr.orelse))

    def _subscript(self, expr: ast.Subscript) -> frozenset[str] | None:
        elements = self.elements(expr.value)
        if elements is None:
            return None
        index = expr.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int) and not isinstance(index.value, bool):
            try:
                return self.strings(elements[index.value])
            except IndexError:
                return None
        return self._union_all(elements)

    def _call(self, expr: ast.Call) -> frozenset[str] | None:
        if isinstance(expr.func, ast.Name) and not expr.args and not expr.keywords:
            bindings = self.index.value_bindings.get(expr.func.id, [])
            if bindings and all(kind == RETURN for kind, _ in bindings):
                return self._union_all([source for _, source in bindings])
        folded = fold_string(expr)
        return frozenset({folded[0]}) if folded is not None and folded[1] else None

    # -- names / containers -------------------------------------------------

    def name_strings(self, name: str) -> frozenset[str] | None:
        if name in self._names:
            return self._names[name]
        if name in self._in_progress:
            return None
        self._in_progress.add(name)
        result = self._resolve_name(name)
        self._in_progress.discard(name)
        self._names[name] = result
        return result

    def _resolve_name(self, name: str) -> frozenset[str] | None:
        bindings = self.index.value_bindings.get(name)
        if not bindings:
            return None
        values: frozenset[str] | None = frozenset()
        if any(kind == PARAM for kind, _ in bindings):
            values = self._param_values(name)
            if values is None:
                return None
        for kind, source in bindings:
            if kind == PARAM:
                continue
            if kind == VALUE:
                values = _union(values, self.strings(source))
            elif kind == ELEMENT:
                elements = self.elements(source)
                values = _union(values, None if elements is None else self._union_all(elements))
            elif kind.startswith(f"{ELEMENT}:"):
                values = _union(values, self._positional_elements(source, int(kind.split(":", 1)[1])))
            else:
                return None
            if values is None:
                return None
        return values

    def _param_values(self, name: str) -> frozenset[str] | None:
        """Union of the arguments every call site in this module passes for
        parameter ``name`` (``None`` if the function escapes, is a method, is
        defined twice, has no call site, or any argument is unknown)."""
        values: frozenset[str] | None = frozenset()
        for function, position in self.index.params.get(name, []):
            values = _union(values, self._call_site_values(function, position, name))
            if values is None:
                return None
        return values

    def _call_site_values(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, position: int | None, name: str
    ) -> frozenset[str] | None:
        if isinstance(self.index.parents.get(function), ast.ClassDef):
            return None
        if len(self.index.functions.get(function.name, [])) != 1:
            return None
        loads = self.index.name_loads.get(function.name, [])
        if not loads:
            return None
        default = _parameter_default(function, position, name)
        values: frozenset[str] | None = frozenset()
        for load in loads:
            call = self.index.parents.get(load)
            if not (isinstance(call, ast.Call) and call.func is load):
                return None  # the function escapes (callback, alias) — its args are unknowable
            if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
                return None
            argument: ast.expr | None = None
            if position is not None and position < len(call.args):
                argument = call.args[position]
            else:
                argument = next((kw.value for kw in call.keywords if kw.arg == name), default)
            values = _union(values, self.strings(argument))
            if values is None:
                return None
        return values

    def _attribute(self, expr: ast.Attribute) -> frozenset[str] | None:
        """``spec.name`` where ``spec = importlib.util.spec_from_file_location("x", ...)``."""
        if expr.attr != "name" or not isinstance(expr.value, ast.Name):
            return None
        bindings = self.index.value_bindings.get(expr.value.id, [])
        if not bindings or any(kind != VALUE for kind, _ in bindings):
            return None
        names: list[ast.expr | None] = []
        for _, source in bindings:
            if not (isinstance(source, ast.Call) and _callee_name(source.func) in _SPEC_FACTORIES):
                return None
            first = source.args[0] if source.args else next((k.value for k in source.keywords if k.arg == "name"), None)
            names.append(first)
        return self._union_all(names)

    def _positional_elements(self, container: ast.expr | None, position: int) -> frozenset[str] | None:
        """Strings at ``position`` of every (literal tuple / list) element of ``container``."""
        elements = self.elements(container)
        if elements is None:
            return None
        picked: list[ast.expr | None] = []
        for element in elements:
            inner = self.elements(element) if isinstance(element, (ast.Tuple, ast.List)) else None
            if inner is None or position >= len(inner):
                return None
            picked.append(inner[position])
        return self._union_all(picked)

    def elements(self, expr: ast.expr | None) -> list[ast.expr] | None:
        """The element expressions of a constant container (literal, or a name
        bound only to literal containers), else ``None``."""
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            if any(isinstance(e, ast.Starred) for e in expr.elts):
                return None
            return list(expr.elts)
        if isinstance(expr, ast.Name) and expr.id not in self._in_progress:
            bindings = self.index.value_bindings.get(expr.id)
            if not bindings or any(kind != VALUE for kind, _ in bindings):
                return None
            self._in_progress.add(expr.id)
            try:
                out: list[ast.expr] = []
                for _, source in bindings:
                    inner = self.elements(source)
                    if inner is None:
                        return None
                    out.extend(inner)
                return out
            finally:
                self._in_progress.discard(expr.id)
        return None

    def _union_all(self, exprs: Sequence[ast.expr | None]) -> frozenset[str] | None:
        values: frozenset[str] | None = frozenset()
        for item in exprs:
            values = _union(values, self.strings(item))
            if values is None:
                return None
        return values

    # -- verdicts -----------------------------------------------------------

    def provably_outside(self, expr: ast.expr | None, keys: ProtectedKeys) -> bool:
        """``expr`` is PROVABLY not a protected key (default-deny: unknown → False).

        A fully known value is tested directly; a value with a known constant
        leading prefix (``f"tests.fake.{name}"``) is outside when no completion
        of the prefix can be protected; a name is outside when every one of its
        bindings is.
        """
        values = self.strings(expr)
        if values is not None:
            return not any(keys(v) for v in values)
        folded = fold_string(expr) if expr is not None else None
        if folded is not None and folded[0]:
            return not keys.could_complete(folded[0])
        if isinstance(expr, ast.Name) and expr.id not in self._in_progress:
            bindings = self.index.value_bindings.get(expr.id, [])
            if not bindings or any(kind != VALUE for kind, _ in bindings):
                return False
            self._in_progress.add(expr.id)
            try:
                return all(self.provably_outside(source, keys) for _, source in bindings)
            finally:
                self._in_progress.discard(expr.id)
        return False

    def definitely_object(self, expr: ast.expr | None) -> bool:
        """``expr`` is PROVABLY not a string: an attribute / call / container
        expression, or a name bound only to such (so a ``setattr`` target is an
        object, not a dotted path)."""
        if isinstance(expr, (ast.Attribute, ast.Call, ast.List, ast.Dict, ast.Set, ast.Lambda)):
            return not (isinstance(expr, ast.Call) and fold_string(expr) is not None)
        if not isinstance(expr, ast.Name):
            return False
        bindings = self.index.value_bindings.get(expr.id, [])
        return bool(bindings) and all(
            kind == VALUE and source is not None and self.definitely_object(source) for kind, source in bindings
        )

    def could_be(self, expr: ast.expr | None, target: str) -> bool:
        """``expr`` might evaluate to ``target`` (unknown → True)."""
        values = self.strings(expr)
        if values is not None:
            return target in values
        folded = fold_string(expr) if expr is not None else None
        if folded is not None and folded[0]:
            return target.startswith(folded[0])
        return True


_STRING_HANDLERS: dict[type, Callable[[ConstantTable, Any], frozenset[str] | None]] = {
    ast.Constant: ConstantTable._constant,
    ast.Name: ConstantTable._name,
    ast.BinOp: ConstantTable._concat,
    ast.JoinedStr: ConstantTable._joined,
    ast.IfExp: ConstantTable._if_exp,
    ast.Subscript: ConstantTable._subscript,
    ast.Call: ConstantTable._call,
    ast.Attribute: ConstantTable._attribute,
}


def _parameter_default(
    function: ast.FunctionDef | ast.AsyncFunctionDef, position: int | None, name: str
) -> ast.expr | None:
    args = function.args
    if position is None:
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if arg.arg == name:
                return default
        return None
    positional = (*args.posonlyargs, *args.args)
    offset = len(positional) - len(args.defaults)
    return args.defaults[position - offset] if position >= offset else None


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _union(a: frozenset[str] | None, b: frozenset[str] | None) -> frozenset[str] | None:
    if a is None or b is None:
        return None
    out = a | b
    return out if len(out) <= _MAX_VALUES else None


def _product(a: frozenset[str] | None, b: frozenset[str] | None) -> frozenset[str] | None:
    if a is None or b is None or len(a) * len(b) > _MAX_VALUES:
        return None
    return frozenset(x + y for x in a for y in b)


# ---------------------------------------------------------------------------
# Scope helpers.
# ---------------------------------------------------------------------------


def is_module_attr(expr: ast.expr, module_names: set[str], attr: str, direct_names: set[str]) -> bool:
    """``expr`` is ``<module alias>.<attr>`` or a name bound by ``from <module> import <attr>``."""
    if isinstance(expr, ast.Attribute) and expr.attr == attr:
        return isinstance(expr.value, ast.Name) and expr.value.id in module_names
    return isinstance(expr, ast.Name) and expr.id in direct_names


def enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function definition lexically containing ``node``."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _FUNCTION_TYPES):
            return current
        current = parents.get(current)
    return None


def immediate_scope(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.AST | None:
    """Innermost ``def`` / ``async def`` / ``lambda`` lexically containing ``node``."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _SCOPE_TYPES):
            return current
        current = parents.get(current)
    return None


def parse_index(path: Path, prefilter: re.Pattern[str] | None = None) -> ModuleIndex | None:
    """Read + parse ``path`` once and index it; ``None`` when unreadable, a
    syntax error, or the cheap ``prefilter`` regex finds no relevant token."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if prefilter is not None and not prefilter.search(text):
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None
    return ModuleIndex.build(tree)
