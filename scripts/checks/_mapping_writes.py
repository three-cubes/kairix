"""Shared AST engine for F1 / F2: every write to a process-global mapping.

F1 guards ``sys.modules`` (no swapping kairix modules) and F2 guards
``os.environ`` (no writing KAIRIX_* keys). Both are the same question asked
of a different process-global ``MutableMapping``: *does this statement
mutate the mapping in a way that can touch a protected key?* Answering it
piecemeal let each review round find another spelling, so this module
answers it once, for both detectors, in three layers:

1. **Receiver resolution** (:class:`ProcessMapping`) — is this expression
   the live mapping? Covers ``os.environ`` through any import alias
   (``import os as o``), ``from os import environ [as e]``, and local
   rebinding followed to a fixpoint (``a = os.environ; b = a``). A COPY
   (``dict(os.environ)``, ``os.environ.copy()``, ``{**os.environ}``) is a
   different object and never resolves.
2. **Argument binding** (:func:`bind_call`) — every call is matched to the
   callee's real parameter names, positional or keyword, the way
   ``inspect.Signature.bind`` would, instead of reading ``args[0]``.
3. **The mutation surface** (:class:`WriteSurface`) — the full
   ``MutableMapping`` write API plus the test-framework helpers that write
   through it:

   * subscript ``m[k] = v``, ``m[k] op= v``, ``del m[k]``;
   * ``m |= other`` and ``m.__ior__(other)``;
   * ``m.__setitem__(key, value)``, ``m.__delitem__(key)``,
     ``m.pop(key, default)``, ``m.setdefault(key, default)``,
     ``m.update(other, **kw)``;
   * ``m.clear()`` and ``m.popitem()`` — ALWAYS reported: they remove keys
     the AST cannot name, so they can remove protected ones (hiding a
     ``KAIRIX_*`` variable or evicting a kairix module is the same
     influence as deleting it by name);
   * replacing the mapping wholesale — ``os.environ = {...}``,
     ``del os.environ``, ``setattr(os, "environ", m)``,
     ``delattr(os, "environ")``;
   * pytest ``monkeypatch`` (the fixture, any ``pytest.MonkeyPatch()``
     instance, or a ``MonkeyPatch.context()`` target):
     ``setenv(name, value, prepend)``, ``delenv(name, raising)`` (env
     only), ``setitem(dic, name, value)``, ``delitem(dic, name, raising)``,
     and both ``setattr`` / ``delattr`` overloads —
     ``("os.environ", value)`` and ``(os, "environ", value)``;
   * ``unittest.mock``: ``patch.dict(in_dict, values, clear, **kw)`` (the
     mapping as an object or as the ``"os.environ"`` string),
     ``patch.object(os, "environ", ...)`` and ``patch("os.environ", ...)``.

A key / mapping payload is "protected" per the detector's predicate, resolved
through ``_ast_key_taint`` (literals, f-strings, variables, helper returns).
A payload the AST cannot see into (a variable, a call, a ``**`` spread, a
``*args`` spread hiding the key) is treated as possibly protected.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from _ast_key_taint import ModuleIndex, ProtectedKeys, key_is_protected

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
_CLEARING_METHODS = frozenset({"clear", "popitem"})


# ---------------------------------------------------------------------------
# Layer 2 — argument binding.
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
# Layer 1 — receiver resolution.
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

    def is_dotted_string(self, expr: ast.expr | None) -> bool:
        return isinstance(expr, ast.Constant) and expr.value == self.dotted

    def is_module_attr_pair(self, obj: ast.expr | None, name: ast.expr | None) -> bool:
        """``(os, "environ")`` — the module plus the attribute name as a string."""
        return self.is_module(obj) and isinstance(name, ast.Constant) and name.value == self.attr


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


def is_patch_ref(expr: ast.expr, names: set[str]) -> bool:
    return (isinstance(expr, ast.Name) and expr.id in names) or (
        isinstance(expr, ast.Attribute) and expr.attr == "patch"
    )


# ---------------------------------------------------------------------------
# Layer 3 — the mutation surface.
# ---------------------------------------------------------------------------


class WriteSurface:
    """Reports every statement that writes ``mapping`` in a protected way."""

    def __init__(
        self,
        index: ModuleIndex,
        mapping: ProcessMapping,
        keys: ProtectedKeys,
        tainted: set[str],
        marker: str,
    ) -> None:
        self.mapping = mapping
        self.keys = keys
        self.tainted = tainted
        self.label = mapping.dotted
        self.marker = marker
        self.monkeypatch = monkeypatch_names(index)
        self.patch = patch_names(index)

    # -- predicates -------------------------------------------------------

    def key_may_be_protected(self, bound: BoundCall, param: str) -> bool:
        expr = bound.get(param)
        if expr is None:
            return bound.has_spread
        return key_is_protected(expr, self.keys, self.tainted)

    @staticmethod
    def _hidden(bound: BoundCall, param: str) -> bool:
        """``param`` is unbound and a ``*args`` / ``**kw`` spread may supply it."""
        return bound.get(param) is None and bound.has_spread

    def _is_receiver_param(self, bound: BoundCall, param: str) -> bool:
        """The bound argument is the mapping — or a spread may be hiding it."""
        return self.mapping.is_receiver(bound.get(param)) or self._hidden(bound, param)

    def _replaces_mapping(self, bound: BoundCall, target: str, name: str) -> bool:
        """``(target, name)`` names the mapping: ``("os.environ", ...)``,
        ``(os, "environ")``, or a spread hiding either half."""
        obj = bound.get(target)
        if self.mapping.is_dotted_string(obj) or self.mapping.is_module_attr_pair(obj, bound.get(name)):
            return True
        if self._hidden(bound, target):
            return True
        return self.mapping.is_module(obj) and self._hidden(bound, name)

    def mapping_may_carry(self, payload: ast.expr | None, keywords: tuple[ast.keyword, ...]) -> bool:
        """An ``update`` / ``|=`` / ``patch.dict`` payload that can write a protected key."""
        if any(kw.arg is None or self.keys(kw.arg) for kw in keywords):
            return True
        if payload is None:
            return False
        if isinstance(payload, ast.Dict):
            return any(k is None or key_is_protected(k, self.keys, self.tainted) for k in payload.keys)
        if isinstance(payload, (ast.List, ast.Tuple)) and not payload.elts:
            return False
        return True  # a variable / call / comprehension: the AST cannot rule it out

    def _subscript_key(self, target: ast.expr) -> bool:
        return (
            isinstance(target, ast.Subscript)
            and self.mapping.is_receiver(target.value)
            and key_is_protected(target.slice, self.keys, self.tainted)
        )

    # -- statement shapes -------------------------------------------------

    def writes(self, node: ast.AST) -> list[str]:
        """Shape labels for every protected write ``node`` performs."""
        if isinstance(node, ast.Call):
            return self._call_writes(node)
        if isinstance(node, ast.Assign):
            return [s for t in node.targets if (s := self._target_write(t, "assign"))]
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            label = self._target_write(node.target, "assign")
            return [label] if label else []
        if isinstance(node, ast.AugAssign):
            return self._augassign_writes(node)
        if isinstance(node, ast.Delete):
            return [s for t in node.targets if (s := self._target_write(t, "del"))]
        return []

    def _target_write(self, target: ast.expr, verb: str) -> str | None:
        if self._subscript_key(target):
            return f"{verb} {self.label}[{self.marker}]"
        if isinstance(target, ast.Attribute) and self.mapping.is_receiver(target):
            return f"replace {self.label}"
        return None

    def _augassign_writes(self, node: ast.AugAssign) -> list[str]:
        if self._subscript_key(node.target):
            return [f"assign {self.label}[{self.marker}]"]
        if (
            isinstance(node.op, ast.BitOr)
            and self.mapping.is_receiver(node.target)
            and self.mapping_may_carry(node.value, ())
        ):
            return [f"{self.label} |= <may carry {self.marker}>"]
        return []

    def _call_writes(self, call: ast.Call) -> list[str]:
        func = call.func
        labels: list[str] = []
        if isinstance(func, ast.Attribute):
            if self.mapping.is_receiver(func.value):
                labels.extend(self._method_writes(func.attr, call))
            if isinstance(func.value, ast.Name) and func.value.id in self.monkeypatch:
                labels.extend(self._monkeypatch_writes(func.attr, call))
            if is_patch_ref(func.value, self.patch):
                labels.extend(self._patch_helper_writes(func.attr, call))
        if is_patch_ref(func, self.patch):
            bound = bind_call(call, PATCH_SIGNATURE)
            if self.mapping.is_dotted_string(bound.get("target")) or self._hidden(bound, "target"):
                labels.append(f"patch({self.label})")
        if isinstance(func, ast.Name) and func.id in BUILTIN_ATTR_SIGNATURES:
            bound = bind_call(call, BUILTIN_ATTR_SIGNATURES[func.id])
            if self._replaces_mapping(bound, "obj", "name"):
                labels.append(f"replace {self.label}")
        return labels

    def _method_writes(self, method: str, call: ast.Call) -> list[str]:
        if method not in MAPPING_METHOD_SIGNATURES:
            return []
        bound = bind_call(call, MAPPING_METHOD_SIGNATURES[method], _POSITIONAL_ONLY.get(method, frozenset()))
        if method in _KEYED_METHODS and self.key_may_be_protected(bound, "key"):
            return [f"{self.label}.{method}({self.marker})"]
        if method in _BULK_METHODS and (
            self._hidden(bound, "other") or self.mapping_may_carry(bound.get("other"), bound.extra_keywords)
        ):
            return [f"{self.label}.{method}(<may carry {self.marker}>)"]
        if method in _CLEARING_METHODS:
            return [f"{self.label}.{method}()"]
        return []

    def _monkeypatch_writes(self, method: str, call: ast.Call) -> list[str]:
        if method not in MONKEYPATCH_SIGNATURES:
            return []
        bound = bind_call(call, MONKEYPATCH_SIGNATURES[method])
        if method in {"setenv", "delenv"}:
            if self.mapping.attr == "environ" and self.key_may_be_protected(bound, "name"):
                return [f"monkeypatch.{method}({self.marker})"]
            return []
        if method in {"setitem", "delitem"}:
            if self._is_receiver_param(bound, "dic") and self.key_may_be_protected(bound, "name"):
                return [f"monkeypatch.{method}({self.label}, {self.marker})"]
            return []
        # setattr / delattr — both overloads: ("os.environ", value) and (os, "environ", value).
        if self._replaces_mapping(bound, "target", "name"):
            return [f"monkeypatch.{method}({self.label})"]
        return []

    def _patch_helper_writes(self, helper: str, call: ast.Call) -> list[str]:
        if helper == "dict":
            bound = bind_call(call, PATCH_DICT_SIGNATURE)
            if not (self._is_receiver_param(bound, "in_dict") or self.mapping.is_dotted_string(bound.get("in_dict"))):
                return []
            labels = []
            if self._hidden(bound, "values") or self.mapping_may_carry(bound.get("values"), bound.extra_keywords):
                labels.append(f"patch.dict({self.label}, <{self.marker}>)")
            clear = bound.get("clear")
            truthy = clear is not None and not (isinstance(clear, ast.Constant) and not clear.value)
            if truthy or self._hidden(bound, "clear"):
                labels.append(f"patch.dict({self.label}, clear=True)")
            return labels
        if helper == "object":
            bound = bind_call(call, PATCH_OBJECT_SIGNATURE)
            if self._replaces_mapping(bound, "target", "attribute"):
                return [f"patch({self.label})"]
        return []
