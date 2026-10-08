"""Shared DEFAULT-DENY engine for F1 / F2: every reference to a process-global mapping.

F1 guards ``sys.modules`` (no swapping kairix modules) and F2 guards
``os.environ`` (no writing ``KAIRIX_*`` keys). Enumerating write forms (a
denylist) let every review round find another spelling, so the model is
inverted: **every reference to the mapping must sit in an allow-listed READ
context, or be a write PROVEN safe — anything else is a violation.**

1. **Receiver resolution** (:class:`ProcessMapping`) — the live mapping,
   through any import alias (``import os as o``), ``from os import environ
   [as e]``, local rebinding followed to a fixpoint (``a = os.environ; b =
   a``), and ``getattr(os, "environ")``. A COPY (``dict(os.environ)``,
   ``os.environ.copy()``, ``{**os.environ}``) is a different object.
2. **Argument binding** (:func:`bind_call`) — every call is matched to the
   callee's real parameter names, positional or keyword
   (``inspect.Signature.bind`` semantics; ``update``'s ``other`` is
   positional-only); a ``*args`` / ``**kw`` spread that could hide an
   argument makes it unknown.
3. **The classifier** (:class:`MappingGuard`) visits EVERY reference once and
   classifies its parent context:

   * **allowed reads** — ``R[k]`` (Load), ``R.get / copy / items / keys /
     values / __contains__ / __getitem__ / __len__ / __iter__``, ``k in R`` /
     ``k not in R``, ``len(R)``, ``dict(R, ...)``, ``{**R}``, ``for x in R``
     and comprehension sources, ``R`` as ``env=`` / ``environ=`` to
     ``subprocess.*`` / ``os.exec*`` / ``os.spawn*``, and binding ``R`` to a
     plain name (whose own uses are classified the same way);
   * **writes allowed only when PROVEN safe** — subscript store / del
     (including inside a tuple / list / starred unpack), ``pop`` /
     ``setdefault`` / ``__setitem__`` / ``__delitem__`` / ``update`` /
     ``__ior__`` / ``|=``, ``<monkeypatch>.setitem / delitem``,
     ``patch.dict``: allowed iff every key resolves statically
     (:class:`~_ast_key_taint.ConstantTable`) to a NON-protected value; an
     unresolved key or payload counts as protected;
   * **everything else is a violation** — ``clear()`` / ``popitem()``, any
     other method or attribute, passing ``R`` to any other callable, calling
     it, comparing it other than ``in``, returning / yielding it, putting it
     in a container, and replacing the mapping (``os.environ = m``,
     ``del os.environ``, ``setattr`` / ``delattr`` / ``patch.object`` /
     ``monkeypatch.setattr`` / ``patch`` on ``os.environ`` /
     ``"os.environ"``, with dotted strings constant-folded).

Helpers that write the mapping WITHOUT naming it are classified too:
``<monkeypatch>.setenv / delenv`` (F2) are allowed only when the variable
name is provably not protected.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from _ast_key_taint import ConstantTable, ModuleIndex, ProtectedKeys

# ---------------------------------------------------------------------------
# Signatures — parameter names in positional order, as the callees declare
# them (pytest.MonkeyPatch, unittest.mock, MutableMapping, builtins).
# ---------------------------------------------------------------------------

MONKEYPATCH_SIGNATURES: dict[str, tuple[str, ...]] = {
    "setenv": ("name", "value", "prepend"),
    "delenv": ("name", "raising"),
    "setitem": ("dic", "name", "value"),
    "delitem": ("dic", "name", "raising"),
    "setattr": ("target", "name", "value", "raising"),
    "delattr": ("target", "name", "raising"),
}
MAPPING_METHOD_SIGNATURES: dict[str, tuple[str, ...]] = {
    "__setitem__": ("key", "value"),
    "__delitem__": ("key",),
    "pop": ("key", "default"),
    "setdefault": ("key", "default"),
    "update": ("other",),
    "__ior__": ("other",),
    "clear": (),
    "popitem": (),
}
PATCH_SIGNATURE: tuple[str, ...] = ("target", "new", "spec", "create", "spec_set", "autospec", "new_callable")
PATCH_DICT_SIGNATURE: tuple[str, ...] = ("in_dict", "values", "clear")
PATCH_OBJECT_SIGNATURE: tuple[str, ...] = ("target", "attribute", "new")
BUILTIN_ATTR_SIGNATURES: dict[str, tuple[str, ...]] = {
    "setattr": ("obj", "name", "value"),
    "delattr": ("obj", "name"),
}
RELOAD_SIGNATURE: tuple[str, ...] = ("module",)
IMPORT_MODULE_SIGNATURE: tuple[str, ...] = ("name", "package")

# ``MutableMapping.update(self, other=(), /, **kwds)`` — ``other`` is positional-only.
_POSITIONAL_ONLY: dict[str, frozenset[str]] = {"update": frozenset({"other"}), "__ior__": frozenset({"other"})}

_KEYED_METHODS = frozenset({"__setitem__", "__delitem__", "pop", "setdefault"})
_BULK_METHODS = frozenset({"update", "__ior__"})


# ---------------------------------------------------------------------------
# Argument binding.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundCall:
    """A call's arguments mapped onto the callee's parameter names.

    ``extra_keywords`` holds keywords outside the signature plus any
    ``**spread`` (``kw.arg is None``); ``opaque`` is True when a ``*args``
    spread hid positional bindings.
    """

    arguments: dict[str, ast.expr]
    extra_keywords: tuple[ast.keyword, ...]
    opaque: bool

    def get(self, name: str) -> ast.expr | None:
        return self.arguments.get(name)

    @property
    def has_spread(self) -> bool:
        return self.opaque or any(kw.arg is None for kw in self.extra_keywords)


def bind_call(call: ast.Call, params: tuple[str, ...], positional_only: frozenset[str] = frozenset()) -> BoundCall:
    """Bind ``call``'s positional + keyword arguments to ``params`` by name.

    ``positional_only`` names (``MutableMapping.update(other, /, **kw)``) are
    never bound from a keyword — ``m.update(other=v)`` writes the KEY
    ``"other"``, so that keyword stays in ``extra_keywords``.
    """
    arguments: dict[str, ast.expr] = {}
    opaque = False
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            opaque = True
            break
        if index < len(params):
            arguments[params[index]] = arg
    extra: list[ast.keyword] = []
    for kw in call.keywords:
        if kw.arg is not None and kw.arg in params and kw.arg not in positional_only and kw.arg not in arguments:
            arguments[kw.arg] = kw.value
        else:
            extra.append(kw)
    return BoundCall(arguments, tuple(extra), opaque)


# ---------------------------------------------------------------------------
# Receiver resolution.
# ---------------------------------------------------------------------------


@dataclass
class ProcessMapping:
    """Resolves expressions that ARE the live ``<module>.<attr>`` mapping."""

    module: str
    attr: str
    module_names: set[str]
    names: set[str]

    @classmethod
    def resolve(cls, index: ModuleIndex, module: str, attr: str) -> ProcessMapping:
        mapping = cls(module, attr, index.module_aliases(module), index.from_imports(module, attr))
        bindings = index.name_bindings
        changed = True
        while changed:  # follow local rebinding (a = os.environ; b = a; o = os) to a fixpoint
            changed = False
            for name, value in bindings:
                if name not in mapping.names and mapping.is_receiver(value):
                    mapping.names.add(name)
                    changed = True
                if name not in mapping.module_names and mapping.is_module(value):
                    mapping.module_names.add(name)
                    changed = True
        return mapping

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.attr}"

    def is_module(self, expr: ast.expr | None) -> bool:
        return isinstance(expr, ast.Name) and expr.id in self.module_names

    def is_receiver(self, expr: ast.expr | None) -> bool:
        if isinstance(expr, ast.Attribute) and expr.attr == self.attr:
            return self.is_module(expr.value)
        return isinstance(expr, ast.Name) and expr.id in self.names


def monkeypatch_names(index: ModuleIndex) -> set[str]:
    """Names bound to a pytest MonkeyPatch: the fixture, ``pytest.MonkeyPatch()``
    instances, and ``with <mp>.context() as m`` / ``MonkeyPatch.context()`` targets."""

    def is_monkeypatch_class(expr: ast.expr) -> bool:
        return (isinstance(expr, ast.Name) and expr.id == "MonkeyPatch") or (
            isinstance(expr, ast.Attribute) and expr.attr == "MonkeyPatch"
        )

    names = {"monkeypatch"}
    bindings = index.name_bindings
    with_targets = index.with_targets
    changed = True
    while changed:
        changed = False
        for name, value in bindings:
            makes_one = isinstance(value, ast.Call) and is_monkeypatch_class(value.func)
            if name not in names and (makes_one or (isinstance(value, ast.Name) and value.id in names)):
                names.add(name)
                changed = True
        for name, ctx_expr in with_targets:
            if name in names or not isinstance(ctx_expr, ast.Call):
                continue
            func = ctx_expr.func
            if isinstance(func, ast.Attribute) and func.attr == "context":
                owner = func.value
                if is_monkeypatch_class(owner) or (isinstance(owner, ast.Name) and owner.id in names):
                    names.add(name)
                    changed = True
    return names


def patch_names(index: ModuleIndex) -> set[str]:
    """Local names bound to ``unittest.mock.patch`` / ``mock.patch``."""
    return {"patch"} | index.from_imports("unittest.mock", "patch") | index.from_imports("mock", "patch")


def mock_module_names(index: ModuleIndex) -> set[str]:
    """Local names bound to the ``unittest.mock`` / ``mock`` module."""
    names = index.from_imports("unittest", "mock") | index.module_aliases("mock")
    names |= {a.asname for node in index.imports for a in node.names if a.name == "unittest.mock" and a.asname}
    return names


def is_patch_ref(
    expr: ast.expr, names: set[str], mock_names: set[str] | None = None, unittest_names: set[str] | None = None
) -> bool:
    """``expr`` is ``unittest.mock.patch`` — a bound name, ``<mock>.patch`` or
    ``unittest.mock.patch``. A bare ``.patch`` attribute on anything else (an
    HTTP client's ``client.patch(url)``) is not."""
    if isinstance(expr, ast.Name):
        return expr.id in names
    if not (isinstance(expr, ast.Attribute) and expr.attr == "patch"):
        return False
    owner = expr.value
    if mock_names is None:  # back-compat: any ``.patch`` attribute
        return True
    if isinstance(owner, ast.Name):
        return owner.id in mock_names
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr == "mock"
        and isinstance(owner.value, ast.Name)
        and owner.value.id in (unittest_names or set())
    )


# ---------------------------------------------------------------------------
# Layer 3 — the default-deny classifier.
# ---------------------------------------------------------------------------

#: read methods / attributes allowed on the mapping (called or not)
READ_ATTRIBUTES = frozenset(
    {"get", "copy", "items", "keys", "values", "__contains__", "__getitem__", "__len__", "__iter__"}
)
#: builtins that only read the mapping when it is their first argument
READ_BUILTINS = frozenset({"len", "dict"})
#: keyword names under which a process launcher reads the mapping as its environment
ENV_KEYWORDS = frozenset({"env", "environ"})
_SUBPROCESS_FUNCTIONS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)
_OS_LAUNCHER_PREFIXES = ("exec", "spawn", "posix_spawn")

Finding = tuple[ast.AST, str]


class MappingGuard:
    """Classifies every reference to one process-global mapping (default-deny)."""

    def __init__(
        self,
        index: ModuleIndex,
        mapping: ProcessMapping,
        keys: ProtectedKeys,
        constants: ConstantTable,
        marker: str,
    ) -> None:
        self.index = index
        self.mapping = mapping
        self.keys = keys
        self.constants = constants
        self.label = mapping.dotted
        self.marker = marker
        self.monkeypatch = monkeypatch_names(index)
        self.patch = patch_names(index)
        self.mock_modules = mock_module_names(index)
        self.unittest_modules = index.module_aliases("unittest")
        self.subprocess_modules = index.module_aliases("subprocess")
        self.subprocess_functions = {
            name for fn in _SUBPROCESS_FUNCTIONS for name in index.from_imports("subprocess", fn)
        }
        self.os_modules = index.module_aliases("os")

    # -- public -------------------------------------------------------------

    def findings(self) -> list[Finding]:
        """Every violation: unsafe uses of a reference + helper-only writes."""
        out: list[Finding] = []
        for ref in self.references():
            out.extend(self.classify(ref))
        for node in self.index.candidates:
            if isinstance(node, ast.Call):
                out.extend(self._helper_writes(node))
        return out

    def references(self) -> list[ast.expr]:
        """Every expression that evaluates to the live mapping."""
        refs: list[ast.expr] = [
            node for node in self.index.attributes.get(self.mapping.attr, []) if self.mapping.is_module(node.value)
        ]
        for name in self.mapping.names:
            refs.extend(self.index.name_loads.get(name, []))
        for node in self.index.candidates:
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if node.target.id in self.mapping.names:
                    refs.append(node.target)
            elif isinstance(node, ast.Call) and self._is_getattr_of_mapping(node):
                refs.append(node)
        return refs

    def is_patch(self, expr: ast.expr) -> bool:
        return is_patch_ref(expr, self.patch, self.mock_modules, self.unittest_modules)

    # -- the classifier -----------------------------------------------------

    def classify(self, ref: ast.expr) -> list[Finding]:
        """``[]`` when ``ref`` sits in an allowed context, else its violation(s)."""
        parent = self.index.parents.get(ref)
        if isinstance(parent, ast.AugAssign) and parent.target is ref:
            return self._ior_use(parent)
        if isinstance(ref, ast.Attribute) and isinstance(ref.ctx, (ast.Store, ast.Del)):
            return [(self.index.statement_of(ref), f"replace {self.label}")]
        handler = _CONTEXT_HANDLERS.get(type(parent))
        if handler is not None:
            verdict = handler(self, parent, ref)
            if verdict is not None:
                return verdict
        return [(self.index.statement_of(ref), f"{self.label} used outside the read allow-list")]

    def _subscript_use(self, node: ast.Subscript, ref: ast.expr) -> list[Finding] | None:
        if node.value is not ref:
            return None
        if isinstance(node.ctx, ast.Load) or self.key_safe(node.slice):
            return []
        verb = "del" if isinstance(node.ctx, ast.Del) else "assign"
        return [(self.index.statement_of(node), f"{verb} {self.label}[{self.marker}]")]

    def _attribute_use(self, node: ast.Attribute, ref: ast.expr) -> list[Finding] | None:
        if node.value is not ref:
            return None
        if node.attr in READ_ATTRIBUTES and isinstance(node.ctx, ast.Load):
            return []
        call = self.index.parents.get(node)
        if not (isinstance(call, ast.Call) and call.func is node):
            return [(self.index.statement_of(node), f"{self.label}.{node.attr}")]
        return self._method_use(node.attr, call)

    def _method_use(self, method: str, call: ast.Call) -> list[Finding]:
        if method in _KEYED_METHODS:
            bound = bind_call(call, MAPPING_METHOD_SIGNATURES[method])
            if self._bound_key_safe(bound, "key"):
                return []
            return [(call, f"{self.label}.{method}({self.marker})")]
        if method in _BULK_METHODS:
            bound = bind_call(call, MAPPING_METHOD_SIGNATURES[method], _POSITIONAL_ONLY[method])
            if not bound.opaque and self.payload_safe(bound.get("other"), bound.extra_keywords):
                return []
            return [(call, f"{self.label}.{method}(<may carry {self.marker}>)")]
        return [(call, f"{self.label}.{method}()")]

    def _ior_use(self, node: ast.AugAssign) -> list[Finding]:
        if isinstance(node.op, ast.BitOr) and self.payload_safe(node.value, ()):
            return []
        return [(node, f"{self.label} |= <may carry {self.marker}>")]

    def _compare_use(self, node: ast.Compare, ref: ast.expr) -> list[Finding] | None:
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if comparator is ref and isinstance(op, (ast.In, ast.NotIn)):
                return []
        return None

    def _iteration_use(self, node: ast.For | ast.AsyncFor | ast.comprehension, ref: ast.expr) -> list[Finding] | None:
        return [] if node.iter is ref else None

    def _dict_use(self, node: ast.Dict, ref: ast.expr) -> list[Finding] | None:
        spread = any(key is None and value is ref for key, value in zip(node.keys, node.values, strict=True))
        return [] if spread else None

    def _binding_use(self, node: ast.Assign | ast.AnnAssign | ast.NamedExpr, ref: ast.expr) -> list[Finding] | None:
        if node.value is not ref:
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [] if all(isinstance(t, ast.Name) for t in targets) else None

    def _keyword_use(self, node: ast.keyword, ref: ast.expr) -> list[Finding] | None:
        call = self.index.parents.get(node)
        if not isinstance(call, ast.Call):
            return None
        if node.arg in ENV_KEYWORDS and self._is_process_launcher(call.func):
            return []
        return self._argument_use(call, ref)

    def _call_use(self, node: ast.Call, ref: ast.expr) -> list[Finding] | None:
        if node.func is ref:
            return [(node, f"call {self.label}")]
        func = node.func
        if isinstance(func, ast.Name) and func.id in READ_BUILTINS and node.args and node.args[0] is ref:
            return []
        return self._argument_use(node, ref)

    def _argument_use(self, call: ast.Call, ref: ast.expr) -> list[Finding]:
        """``ref`` passed to a callable: only the known safe-write helpers can pass."""
        func = call.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in self.monkeypatch:
                if func.attr in {"setitem", "delitem"}:
                    bound = bind_call(call, MONKEYPATCH_SIGNATURES[func.attr])
                    if bound.get("dic") is ref:
                        if self._bound_key_safe(bound, "name"):
                            return []
                        return [(call, f"monkeypatch.{func.attr}({self.label}, {self.marker})")]
            if func.attr == "dict" and self.is_patch(func.value):
                bound = bind_call(call, PATCH_DICT_SIGNATURE)
                if bound.get("in_dict") is ref:
                    return self._patch_dict_use(call, bound)
        return [(call, f"{self.label} passed to {_callee(func)}")]

    def _patch_dict_use(self, call: ast.Call, bound: BoundCall) -> list[Finding]:
        out: list[Finding] = []
        hidden_values = bound.get("values") is None and bound.has_spread
        if hidden_values or not self.payload_safe(bound.get("values"), bound.extra_keywords):
            out.append((call, f"patch.dict({self.label}, <{self.marker}>)"))
        clear = bound.get("clear")
        clears = clear is not None and not (isinstance(clear, ast.Constant) and not clear.value)
        if clears or (clear is None and bound.has_spread):
            out.append((call, f"patch.dict({self.label}, clear=True)"))
        return out

    # -- writes through helpers that never name the mapping -----------------

    def _helper_writes(self, call: ast.Call) -> list[Finding]:
        func = call.func
        out: list[Finding] = []
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in self.monkeypatch:
            out.extend(self._monkeypatch_helper(func.attr, call))
        if isinstance(func, ast.Attribute) and self.is_patch(func.value):
            out.extend(self._patch_helper(func.attr, call))
        if self.is_patch(func):
            target = bind_call(call, PATCH_SIGNATURE).get("target")
            if self._names_mapping_by_string(target, call):
                out.append((call, f"patch({self.label})"))
        if isinstance(func, ast.Name) and func.id in BUILTIN_ATTR_SIGNATURES:
            bound = bind_call(call, BUILTIN_ATTR_SIGNATURES[func.id])
            if self._names_mapping_by_pair(bound, "obj", "name"):
                out.append((call, f"replace {self.label}"))
        return out

    def _monkeypatch_helper(self, method: str, call: ast.Call) -> list[Finding]:
        if method not in MONKEYPATCH_SIGNATURES:
            return []
        bound = bind_call(call, MONKEYPATCH_SIGNATURES[method])
        if method in {"setenv", "delenv"}:
            if self.mapping.attr == "environ" and not self._bound_key_safe(bound, "name"):
                return [(call, f"monkeypatch.{method}({self.marker})")]
            return []
        if method in {"setitem", "delitem"}:
            dic = bound.get("dic")
            if dic is None and bound.has_spread:  # the mapping may hide behind the spread
                return [(call, f"monkeypatch.{method}({self.label}, {self.marker})")]
            if isinstance(dic, ast.Constant) and self.constants.could_be(dic, self.mapping.dotted):
                return [(call, f"monkeypatch.{method}({self.label}, {self.marker})")]
            return []
        if method in {"setattr", "delattr"}:
            if self._monkeypatch_attr_names_mapping(bound, method):
                return [(call, f"monkeypatch.{method}({self.label})")]
        return []

    def _monkeypatch_attr_names_mapping(self, bound: BoundCall, method: str) -> bool:
        # ``setattr(target, name, value)``: with ``value`` bound it is the object
        # overload; without it ``target`` is a dotted string (``name`` = value).
        object_overload = bound.get("value") is not None if method == "setattr" else bound.get("name") is not None
        if object_overload:
            return self._names_mapping_by_pair(bound, "target", "name")
        target = bound.get("target")
        if target is None:
            return bound.has_spread
        if self.mapping.is_module(target) or self.constants.definitely_object(target):
            return False
        return self._could_be_dotted(target)

    def _patch_helper(self, helper: str, call: ast.Call) -> list[Finding]:
        if helper == "dict":
            bound = bind_call(call, PATCH_DICT_SIGNATURE)
            in_dict = bound.get("in_dict")
            if (in_dict is None and bound.has_spread) or (
                isinstance(in_dict, (ast.Constant, ast.JoinedStr, ast.BinOp)) and self._could_be_dotted(in_dict)
            ):
                if in_dict is None:
                    return [(call, f"patch.dict({self.label}, <{self.marker}>)")]
                return self._patch_dict_use(call, bound)
            return []
        if helper == "object":
            bound = bind_call(call, PATCH_OBJECT_SIGNATURE)
            if self._names_mapping_by_pair(bound, "target", "attribute"):
                return [(call, f"patch({self.label})")]
        return []

    # -- predicates ---------------------------------------------------------

    def key_safe(self, expr: ast.expr | None) -> bool:
        """``expr`` is PROVABLY a non-protected key (unknown → unsafe)."""
        return self.constants.provably_outside(expr, self.keys)

    def _bound_key_safe(self, bound: BoundCall, param: str) -> bool:
        expr = bound.get(param)
        if expr is None:
            return not bound.has_spread
        return self.key_safe(expr)

    def payload_safe(self, payload: ast.expr | None, keywords: tuple[ast.keyword, ...]) -> bool:
        """An ``update`` / ``|=`` / ``patch.dict`` payload PROVABLY free of protected keys."""
        for kw in keywords:
            if kw.arg is None or self.keys(kw.arg):
                return False
        if payload is None:
            return True
        if isinstance(payload, ast.Dict):
            return all(key is not None and self.key_safe(key) for key in payload.keys)
        if isinstance(payload, (ast.List, ast.Tuple)) and not payload.elts:
            return True
        return False

    def _could_be_dotted(self, expr: ast.expr | None) -> bool:
        return self.constants.could_be(expr, self.mapping.dotted)

    def _names_mapping_by_string(self, target: ast.expr | None, call: ast.Call) -> bool:
        if target is None:
            return bind_call(call, PATCH_SIGNATURE).has_spread
        return self._could_be_dotted(target) and not self.mapping.is_module(target)

    def _names_mapping_by_pair(self, bound: BoundCall, obj_param: str, name_param: str) -> bool:
        """``(os, "environ")`` — the module plus a name that could be the attribute."""
        obj = bound.get(obj_param)
        if obj is None:
            return bound.has_spread
        if not self.mapping.is_module(obj):
            return False
        name = bound.get(name_param)
        return bound.has_spread if name is None else self.constants.could_be(name, self.mapping.attr)

    def _is_getattr_of_mapping(self, call: ast.Call) -> bool:
        if not (isinstance(call.func, ast.Name) and call.func.id == "getattr" and len(call.args) >= 2):
            return False
        return self.mapping.is_module(call.args[0]) and self.constants.could_be(call.args[1], self.mapping.attr)

    def _is_process_launcher(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in self.subprocess_functions
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return False
        owner = func.value.id
        if owner in self.subprocess_modules:
            return True
        return owner in self.os_modules and func.attr.startswith(_OS_LAUNCHER_PREFIXES)


_CONTEXT_HANDLERS: dict[type, Callable[[MappingGuard, Any, ast.expr], list[Finding] | None]] = {
    ast.Subscript: MappingGuard._subscript_use,
    ast.Attribute: MappingGuard._attribute_use,
    ast.Compare: MappingGuard._compare_use,
    ast.For: MappingGuard._iteration_use,
    ast.AsyncFor: MappingGuard._iteration_use,
    ast.comprehension: MappingGuard._iteration_use,
    ast.Dict: MappingGuard._dict_use,
    ast.Assign: MappingGuard._binding_use,
    ast.AnnAssign: MappingGuard._binding_use,
    ast.NamedExpr: MappingGuard._binding_use,
    ast.keyword: MappingGuard._keyword_use,
    ast.Call: MappingGuard._call_use,
}


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_callee(func.value)}.{func.attr}"
    return "<callable>"
