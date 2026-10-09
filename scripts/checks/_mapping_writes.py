"""Shared DEFAULT-DENY engine for F1 / F2: every reference to a process-global mapping.

F1 guards ``sys.modules`` (no swapping kairix modules) and F2 guards
``os.environ`` (no writing ``KAIRIX_*`` keys). Enumerating write forms (a
denylist) let every review round find another spelling, so the model is
inverted: **every reference to the mapping must sit in an allow-listed READ
context, or be a write PROVEN safe — anything else is a violation.**

1. **Receiver resolution** (:class:`ProcessMapping`) — the live mapping,
   through any import alias (``import os as o``) or ``from os import environ
   [as e]`` — import statements only; binding a guarded object to a local
   name is itself a violation (never chased). A COPY (``dict(os.environ)``,
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

Two receiver-side rules close the remaining spellings by principle:

* **Guarded module objects** — every reference to the owning module (``os`` /
  ``sys``, by import alias) is classified too: a static attribute access passes (its
  name is provably not ``environ`` / ``modules``); passing the module to ANY
  callable passes only with no spread and an attribute-name argument provably
  naming something else; anything else fails.
* **Receiver-agnostic helper methods** — ``.setenv / .delenv / .setitem /
  .delitem / .setattr / .delattr`` are classified by METHOD NAME plus bound
  arguments on any receiver; an unresolved key / target fails.
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
    index: ModuleIndex | None = None

    @classmethod
    def resolve(cls, index: ModuleIndex, module: str, attr: str) -> ProcessMapping:
        module_names = index.module_aliases(module) | {
            a.name.split(".")[0]
            for node in index.imports
            for a in node.names
            if a.asname is None and a.name.startswith(f"{module}.")
        }
        # Import statements only — local rebinding of a guarded object is a
        # violation in its own right (``alias_findings``), never chased.
        return cls(module, attr, module_names, index.from_imports(module, attr), index)

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.attr}"

    def is_module(self, expr: ast.expr | None) -> bool:
        """``expr`` is the ``os`` / ``sys`` module — a name whose binding, under
        Python's scoping rules, is the import (a parameter / local / later
        rebind of the same name shadows it)."""
        if not (isinstance(expr, ast.Name) and expr.id in self.module_names):
            return False
        return self.index is None or self.index.resolves_to_module(expr, self.module)

    def is_receiver(self, expr: ast.expr | None) -> bool:
        if isinstance(expr, ast.Attribute) and expr.attr == self.attr:
            return self.is_module(expr.value)
        if not (isinstance(expr, ast.Name) and expr.id in self.names):
            return False
        return self.index is None or self.index.resolves_to_from(expr, self.module, self.attr)


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
#: builtins that may receive a guarded module directly with a static attribute name
_ATTRIBUTE_READERS = frozenset({"getattr", "hasattr"})
#: keyword names under which a process launcher reads the mapping as its environment
ENV_KEYWORDS = frozenset({"env", "environ"})
_SUBPROCESS_FUNCTIONS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
)
_OS_LAUNCHER_PREFIXES = ("exec", "spawn", "posix_spawn")

Finding = tuple[ast.AST, str]


# ---------------------------------------------------------------------------
# Rebinding a guarded import name (same principle as the alias ban).
# ---------------------------------------------------------------------------

#: A guarded NAME and the one binding that makes it the guarded object:
#: ``("module", "os")`` — ``import os`` / ``import os.path`` / ``import os as os``;
#: ``("from", "os", "environ")`` — ``from os import environ``. Builtins
#: (``getattr`` / ``__import__`` ...) are canonical as ``from builtins import x``
#: or when nothing binds them at all.
GuardedName = tuple[str, ...]

BUILTIN_GUARDED_NAMES: dict[str, GuardedName] = {
    name: ("from", "builtins", name) for name in ("getattr", "setattr", "delattr", "__import__")
}


def _is_canonical(binding: tuple[str, ...], canonical: GuardedName) -> bool:
    if canonical[0] == "module":
        module = canonical[1]
        if binding[0] == "import":
            return binding[1] == module or binding[1].startswith(f"{module}.")
        return binding[0] == "import_as" and binding[1] == module
    return binding == canonical


def guarded_rebinds(index: ModuleIndex, guarded: dict[str, GuardedName]) -> list[Finding]:
    """Every binding that makes a guarded name something OTHER than its guarded
    import, where that could change what an earlier / later reference means:

    * at module level or directly in a class body — ANY such binding
      (``os = object()``, ``import json as os``, ``def getattr(...)``,
      ``environ = {}``, ``for sys in ...``, a ``global os`` store in a function);
    * in a function / lambda / comprehension scope — only when the SAME scope
      also imports the guarded object (``import os`` ... ``os = x``), since the
      reference then means different things before and after the rebind.

    A plain function-local shadow (a parameter named ``os``, a local
    ``environ = {}`` with no import of it in that function) is not reported:
    Python makes the name local for the whole body, so no reference in that
    body can reach the guarded import.
    """
    out: list[Finding] = []
    for scope in index.scopes:
        module_like = scope.is_module or scope.is_class
        for name, canonical in guarded.items():
            sites = scope.sites.get(name, [])
            rebinds = [node for binding, node in sites if not _is_canonical(binding, canonical)]
            if not rebinds:
                continue
            imported_here = any(_is_canonical(binding, canonical) for binding, _ in sites)
            if module_like or imported_here:
                where = "module level" if scope.is_module else ("class body" if scope.is_class else "function")
                out.extend((node, f"rebinds guarded name {name} ({where})") for node in rebinds)
    return out


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
        self.patch = patch_names(index)
        self.mock_modules = mock_module_names(index)
        self.unittest_modules = index.module_aliases("unittest")
        self.subprocess_modules = index.module_aliases("subprocess")
        self.subprocess_functions = {
            name for fn in _SUBPROCESS_FUNCTIONS for name in index.from_imports("subprocess", fn)
        }
        self.os_modules = index.module_aliases("os")
        self.builtin_attr_functions = {"getattr", "setattr", "delattr"} | {
            name for fn in ("getattr", "setattr", "delattr") for name in index.from_imports("builtins", fn)
        }
        self.builtins_modules = index.module_aliases("builtins")
        #: extra guarded objects a detector adds (F1: ``importlib`` / ``reload``)
        self.extra_guarded: Callable[[ast.expr], str | None] = lambda _expr: None

    # -- public -------------------------------------------------------------

    def findings(self) -> list[Finding]:
        """Every violation: unsafe uses of the mapping, unsafe uses of its owning
        MODULE object, and helper-method writes that never name either."""
        out: list[Finding] = self.alias_findings()  # first: it records the alias-site refs
        for ref in self.references():
            if id(ref) not in self._alias_refs:
                out.extend(self.classify(ref))
        for ref in self.module_references():
            if id(ref) not in self._alias_refs:
                out.extend(self.classify_module(ref))
        for node in self.index.candidates:
            if isinstance(node, ast.Call):
                out.extend(self._helper_writes(node))
        return out

    def module_references(self) -> list[ast.Name]:
        """Every load of the owning module object (``os`` / ``sys``, by import alias)."""
        refs: list[ast.Name] = []
        for name in self.mapping.module_names:
            refs.extend(ref for ref in self.index.name_loads.get(name, []) if self.mapping.is_module(ref))
        return refs

    def classify_module(self, ref: ast.Name) -> list[Finding]:
        """Default-deny for the MODULE object (``os`` / ``sys``): a static attribute
        access is allowed (``os.environ`` itself is then classified as a mapping
        reference). As an argument it is allowed ONLY to builtin ``getattr`` /
        ``hasattr`` called directly, or to ``<x>.setattr`` / ``<x>.delattr`` /
        ``patch.object``, each with a statically named attribute that is not
        ``environ`` / ``modules`` and no spread. Everything else — any other
        callable, ``vars``, an aliased ``getattr``, binding it to a name — fails."""
        parent = self.index.parents.get(ref)
        module = self.mapping.module
        if isinstance(parent, ast.Attribute) and parent.value is ref:
            return []
        call = parent
        if isinstance(parent, (ast.keyword, ast.Starred)):
            call = self.index.parents.get(parent)
        if isinstance(call, ast.Call) and call.func is not ref:
            if self._module_argument_harmless(call, ref):
                return []
            return [(call, f"{module} passed to {_callee(call.func)} (could reach {self.label})")]
        return [(self.index.statement_of(ref), f"{module} used outside the allow-list")]

    def _module_argument_harmless(self, call: ast.Call, ref: ast.expr) -> bool:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
            return False  # a spread may hide the attribute name or the value
        func = call.func
        if isinstance(func, ast.Name) and func.id in _ATTRIBUTE_READERS and self.index.is_builtin_ref(func):
            # builtin getattr / hasattr, called directly: (obj, name[, default])
            return len(call.args) >= 2 and call.args[0] is ref and self._statically_other_attr(call.args[1])
        if isinstance(func, ast.Attribute) and func.attr in {"setattr", "delattr"}:
            bound = bind_call(call, MONKEYPATCH_SIGNATURES[func.attr])
            return bound.get("target") is ref and self._statically_other_attr(bound.get("name"))
        if isinstance(func, ast.Attribute) and func.attr == "object" and self.is_patch(func.value):
            bound = bind_call(call, PATCH_OBJECT_SIGNATURE)
            return bound.get("target") is ref and self._statically_other_attr(bound.get("attribute"))
        return False

    def _statically_other_attr(self, name: ast.expr | None) -> bool:
        """``name`` resolves statically to attribute names, none of them guarded."""
        values = self.constants.strings(name)
        return values is not None and self.mapping.attr not in values

    # -- aliasing is itself a violation -------------------------------------

    def alias_findings(self) -> list[Finding]:
        """Binding a guarded object to a name — assignment, walrus, default
        argument, ``for`` / ``with`` target, tuple unpacking, ``return`` /
        ``yield``, lambda body — fails. Guarded objects are never chased through
        aliases, so they must only ever be used in the direct form."""
        self._alias_refs: set[int] = set()
        out: list[Finding] = []
        for site in self.index.binding_sites:
            for value, form in _bound_values(site):
                for candidate in _evaluated(value):
                    description = self.guarded(candidate)
                    if description is not None:
                        self._alias_refs.add(id(candidate))
                        out.append((site, f"aliases {description} ({form})"))
        return out

    def guarded(self, expr: ast.expr) -> str | None:
        """What guarded object ``expr`` IS (direct form), or ``None``."""
        if self.mapping.is_receiver(expr):
            return self.label
        if self.mapping.is_module(expr):
            return f"the {self.mapping.module} module"
        if isinstance(expr, ast.Attribute):
            if self.mapping.is_receiver(expr.value):
                return f"{self.label}.{expr.attr}"
            if expr.attr in MONKEYPATCH_SIGNATURES:
                return f"the bound .{expr.attr} helper"
            if expr.attr in {"getattr", "setattr", "delattr"} and isinstance(expr.value, ast.Name):
                if expr.value.id in self.builtins_modules and self.index.resolves_to_module(expr.value, "builtins"):
                    return f"builtins.{expr.attr}"
        if isinstance(expr, ast.Name) and expr.id in self.builtin_attr_functions:
            if self.index.is_builtin_ref(expr):
                return f"builtin {expr.id}"
        return self.extra_guarded(expr)

    def references(self) -> list[ast.expr]:
        """Every expression that evaluates to the live mapping."""
        refs: list[ast.expr] = [
            node for node in self.index.attributes.get(self.mapping.attr, []) if self.mapping.is_module(node.value)
        ]
        for name in self.mapping.names:
            refs.extend(ref for ref in self.index.name_loads.get(name, []) if self.mapping.is_receiver(ref))
        for node in self.index.candidates:
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if self.mapping.is_receiver(node.target):
                    refs.append(node.target)
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
        builtin_reader = isinstance(func, ast.Name) and func.id in READ_BUILTINS and self.index.is_builtin_ref(func)
        if builtin_reader and node.args and node.args[0] is ref:
            return []
        return self._argument_use(node, ref)

    def _argument_use(self, call: ast.Call, ref: ast.expr) -> list[Finding]:
        """``ref`` passed to a callable: only the known safe-write helpers can pass."""
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"setitem", "delitem"}:  # any receiver — the method name is enough
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
        if isinstance(func, ast.Attribute):  # any receiver: setenv / delenv / setitem / ... by method name
            out.extend(self._monkeypatch_helper(func.attr, call))
        if isinstance(func, ast.Attribute) and self.is_patch(func.value):
            out.extend(self._patch_helper(func.attr, call))
        if self.is_patch(func):
            target = bind_call(call, PATCH_SIGNATURE).get("target")
            if self._names_mapping_by_string(target, call):
                out.append((call, f"patch({self.label})"))
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
            return False  # ``(os, "environ", v)`` is the module-object rule's call
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
    ast.keyword: MappingGuard._keyword_use,
    ast.Call: MappingGuard._call_use,
}


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_callee(func.value)}.{func.attr}"
    return "<callable>"


def _bound_values(site: ast.AST) -> list[tuple[ast.expr, str]]:
    """The value expressions a binding site binds to a name, with the form."""
    if isinstance(site, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return [(site.value, "assignment")] if site.value is not None else []
    if isinstance(site, ast.Return):
        return [(site.value, "return")] if site.value is not None else []
    if isinstance(site, (ast.Yield, ast.YieldFrom)):
        return [(site.value, "yield")] if site.value is not None else []
    if isinstance(site, (ast.For, ast.AsyncFor, ast.comprehension)):
        # ``for x in (os.environ,)`` binds the element; iterating the mapping itself
        # (``for k in os.environ``) only yields keys and is an allowed read.
        if isinstance(site.iter, (ast.Tuple, ast.List, ast.Set)):
            return [(elt, "loop target") for elt in site.iter.elts]
        return []
    if isinstance(site, (ast.With, ast.AsyncWith)):
        return [(item.context_expr, "with target") for item in site.items if item.optional_vars is not None]
    if isinstance(site, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = site.args
        values = [(d, "default argument") for d in (*args.defaults, *args.kw_defaults) if d is not None]
        if isinstance(site, ast.Lambda):
            values.append((site.body, "lambda return"))
        return values
    return []


def _evaluated(expr: ast.expr) -> list[ast.expr]:
    """``expr`` and every sub-expression it may EVALUATE TO (tuple / list / set
    elements for unpacking, starred values, conditional branches, ``or`` /
    ``and`` operands, walrus values)."""
    out: list[ast.expr] = [expr]
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        for elt in expr.elts:
            out.extend(_evaluated(elt))
    elif isinstance(expr, ast.Starred):
        out.extend(_evaluated(expr.value))
    elif isinstance(expr, ast.IfExp):
        out.extend(_evaluated(expr.body) + _evaluated(expr.orelse))
    elif isinstance(expr, ast.BoolOp):
        for value in expr.values:
            out.extend(_evaluated(value))
    elif isinstance(expr, ast.NamedExpr):
        out.extend(_evaluated(expr.value))
    return out
