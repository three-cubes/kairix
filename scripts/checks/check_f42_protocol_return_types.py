"""F42: connector/extractor-surface Protocol methods return frozen
dataclasses or other allowed shapes — never ``dict[str, Any]``,
``list[dict]``, bare ``Any``, or ``Mapping[..., Any]``.

The connector/ingestion architecture (see
``docs/architecture/connector-ingestion-architecture.md`` §3 + §5.3)
introduces a family of Protocols that move data across the
domain ↔ plugin boundary: ``SourceConnector``, ``Extractor``,
``BronzeStore``, ``SilverProcessor``, ``EntityGraphSink``. Returns
from these Protocol methods MUST be frozen dataclasses (or tuples of
them, or simple typed shapes) so the boundary stays
self-describing and refactor-safe. Untyped dict returns reintroduce
the dict-as-record drift the architecture exists to prevent.

The rule fires on AST-level inspection of
``kairix/core/protocols.py``. For each Protocol class whose name is
in the connector-surface set, every method's return annotation is
checked against the allow-list:

  * a class name (resolved later to a frozen dataclass — the AST
    layer can't see the @dataclass decorator on the referenced
    class, but it CAN see whether the annotation is a typed name
    versus a ``dict``/``Any``/``Mapping`` rejection),
  * ``str``, ``int``, ``bool``, ``float``, ``bytes``, ``None``,
  * ``tuple[<X>, ...]`` where ``<X>`` is an allowed shape,
  * ``Iterator[<X>]`` / ``Iterable[<X>]`` / ``Sequence[<X>]`` /
    ``list[<X>]`` where ``<X>`` is an allowed shape,
  * ``<X> | None`` or ``Optional[<X>]`` where ``<X>`` is an allowed
    shape.

Rejected:

  * ``dict[K, V]`` / ``Dict[K, V]`` / ``dict`` as a return type,
  * ``Mapping[K, Any]``,
  * bare ``Any``,
  * ``list[dict]`` or any container of ``dict``.

The rule is **forward-looking**: at the moment the rule lands, none
of the connector surfaces yet exist in ``kairix/core/protocols.py``,
so the violation set is empty. The gate fires when Wave 1 adds the
``SourceConnector`` / ``Extractor`` / ... Protocol classes. Existing
Protocols (``DocumentRepository``, ``GraphRepository``, etc.) are
NOT scanned — the typed-boundary discipline is brand-new for the
connector surface and would generate enormous false-positive noise
against the existing dict-shaped repositories. If/when retroactive
typing of older Protocols is in scope, the rule scope widens and the
legacy Protocols are retyped in the same change (there is no
grandfathering).

If ``kairix/core/protocols.py`` does not exist, the check passes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

_PROTOCOLS_FILE_REL = Path("kairix") / "core" / "protocols.py"

# Protocol class names that fall under F42's typed-boundary
# discipline. Match by class name. New connector-surface Protocols
# added in Wave 1 onward must be appended here.
_SURFACE_PROTOCOLS: frozenset[str] = frozenset(
    {
        "SourceConnector",
        "Extractor",
        "BronzeStore",
        "SilverProcessor",
        "EntityGraphSink",
    }
)

# Simple identifier names that are always acceptable as a Protocol
# return annotation — primitive scalars and ``None``.
_ALLOWED_SCALAR_NAMES: frozenset[str] = frozenset({"str", "int", "bool", "float", "bytes", "None"})

# Container generics whose first type-argument is the element type
# the rule must recurse into.
_ELEMENT_CONTAINERS: frozenset[str] = frozenset({"Iterator", "Iterable", "Sequence", "list", "List", "tuple", "Tuple"})

# Names whose use as a return type (directly or as an element type)
# is rejected by F42.
_FORBIDDEN_NAMES: frozenset[str] = frozenset({"dict", "Dict", "Any", "Mapping", "MutableMapping"})

REMEDIATION = """F42: connector-surface Protocol method returns an
untyped shape (``dict[str, Any]`` / ``list[dict]`` / bare ``Any`` /
``Mapping[..., Any]``).

The connector ↔ domain seam must stay self-describing. Untyped dict
returns reintroduce the dict-as-record drift the framework exists
to prevent — refactors silently lose fields, IDEs can't see the
schema, and consumers reach into magic keys.

fix: define a ``@dataclass(frozen=True)`` value object that names
the fields you'd otherwise stuff into a dict, and return THAT.
``tuple[<frozen-dc>, ...]`` is fine for collections;
``Iterator[<frozen-dc>]`` is fine for streams; ``<frozen-dc> | None``
is fine for optional returns. Scalar returns (``str``, ``int``,
``bool``, ``float``, ``bytes``) and ``None`` are also accepted.
next: re-run python3 scripts/checks/check_f42_protocol_return_types.py
to confirm the gate goes green.
run: bash scripts/safe-commit.sh \"feat(protocols): add <ValueObject> dataclass + retype <Surface>.<method>\"

Pass example:
  @dataclass(frozen=True)
  class ChangeEvent:
      op: Literal[\"created\", \"modified\", \"deleted\"]
      item_id: str
      modified_at: str

  class SourceConnector(Protocol):
      def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]: ...

Forbidden example:
  class SourceConnector(Protocol):
      def list_changes(self, cursor: Cursor | None) -> Iterator[dict[str, Any]]: ...  # F42

Why: see docs/architecture/connector-ingestion-architecture.md §3 +
§5.3 — every value object that crosses the boundary is a frozen
dataclass, Pydantic stays at the JSON edge (HTTP/MCP/config), and
the Protocol surface is what the type-checker enforces between
producers and consumers."""


def _annotation_is_allowed(node: ast.expr | None) -> bool:
    """Return True if the annotation node is one of the allowed
    return-type shapes.

    Walks subscripted generics recursively. A bare unknown identifier
    (e.g. ``ChangeEvent``) is treated as allowed — the AST layer
    cannot prove a referenced class is a frozen dataclass, but it
    CAN reject the explicit ``dict`` / ``Any`` / ``Mapping`` names.
    The F41 mypy-strict-clean leg of the contract catches the
    remaining cases (e.g. a class that exists but is not frozen).
    """
    if node is None:
        # No annotation at all is treated as missing-type — fail.
        return False

    # ``-> None`` / ``-> str`` / etc.
    if isinstance(node, ast.Name):
        if node.id in _FORBIDDEN_NAMES:
            return False
        return True

    # ``-> "Some.Name"`` string forward-ref. Treat as allowed unless
    # the inner string is exactly a forbidden token.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip() not in _FORBIDDEN_NAMES

    # ``-> None`` via ast.Constant(None).
    if isinstance(node, ast.Constant) and node.value is None:
        return True

    # ``-> Foo | None`` (PEP 604 union) or ``-> Foo | Bar``.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_is_allowed(node.left) and _annotation_is_allowed(node.right)

    # ``-> Optional[X]`` / ``-> Union[X, Y]``.
    if isinstance(node, ast.Subscript):
        return _subscript_is_allowed(node)

    # ``-> some.qualified.Name``.
    if isinstance(node, ast.Attribute):
        # Strip qualifier; only the trailing attribute matters for
        # the forbidden-name check.
        return node.attr not in _FORBIDDEN_NAMES

    # Unknown shape — be conservative and reject so the rule fires.
    return False


def _subscript_is_allowed(node: ast.Subscript) -> bool:
    """Resolve a subscripted generic and recurse into its arguments."""
    outer_name = _name_of(node.value)
    if outer_name in _FORBIDDEN_NAMES:
        # ``dict[...]`` / ``Mapping[..., Any]`` / etc.
        return False

    # Optional[X] / Union[X, Y] / list[X] / tuple[X, ...] / Iterator[X]
    slice_node = node.slice
    if isinstance(slice_node, ast.Tuple):
        elements = list(slice_node.elts)
    else:
        elements = [slice_node]

    # ``tuple[X, ...]`` carries an Ellipsis in elt[1]; ignore the
    # ellipsis when recursing.
    if outer_name in _ELEMENT_CONTAINERS:
        return all(
            (isinstance(elt, ast.Constant) and elt.value is Ellipsis) or _annotation_is_allowed(elt) for elt in elements
        )

    # Optional/Union: every member must be allowed.
    if outer_name in {"Optional", "Union"}:
        return all(_annotation_is_allowed(elt) for elt in elements)

    # Any other generic carrier (e.g. a frozen-dc wrapped in a
    # custom generic) — accept if the outer name is not forbidden;
    # recurse into the type-args so a forbidden inner name (e.g.
    # ``MyHolder[Any]``) still fails.
    return outer_name not in _FORBIDDEN_NAMES and all(_annotation_is_allowed(elt) for elt in elements)


def _name_of(node: ast.expr) -> str | None:
    """Best-effort extraction of the leftmost identifier in a
    subscript head (``dict`` from ``dict[K, V]``;
    ``typing.Mapping`` -> ``Mapping``).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every Protocol class in ``protocols.py`` whose name is in
    the connector surface set and return repo-relative paths flagged
    by method-name when any method's return annotation is not on the
    F42 allow-list.

    The reported path is a synthetic ``kairix/core/protocols.py::<Class>.<method>``
    so each method violation is reported independently.
    Empty set if ``protocols.py`` doesn't exist or holds no surface
    Protocols.
    """
    protocols_file = repo_root / _PROTOCOLS_FILE_REL
    if not protocols_file.is_file():
        return set()

    try:
        tree = ast.parse(protocols_file.read_text(encoding="utf-8"), filename=str(protocols_file))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()

    violations: set[Path] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name not in _SURFACE_PROTOCOLS:
            continue
        for body_node in node.body:
            if not isinstance(body_node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # Dunder/private methods are excluded — only public surface
            # is subject to the typed-boundary discipline.
            if body_node.name.startswith("_"):
                continue
            if not _annotation_is_allowed(body_node.returns):
                synthetic = Path(f"kairix/core/protocols.py::{node.name}.{body_node.name}")
                violations.add(synthetic)
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f42", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
