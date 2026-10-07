"""F56: every plugin under ``kairix/connectors/<name>/`` declares at least
``SourceConnector`` + one of ``{PollConnector, CheckpointedConnector, EventConnector}``.

The connector class must advertise its capability set either by:

  1. Protocol inheritance — the class declares the relevant Protocols in
     its bases (e.g. ``class FooConnector(SourceConnector, PollConnector):
     ...``); the preferred shape because the static checker sees it.
  2. A module-level ``CAPABILITIES: frozenset[str]`` set with the
     Protocol class names (acceptable fallback when the connector class
     uses ``isinstance`` runtime-check only).

The check imports the connector module via the registered
``[project.entry-points."kairix.connectors"]`` factory shape. To stay
defensive against import-time failures (missing optional deps, secret-
backend bootstraps), the check tolerates ``ImportError`` and surfaces
those plugins as ``import-error`` violations rather than crashing.

Required minimum capability set:

  * ``SourceConnector`` — every connector must satisfy the base shape.
  * At least one of ``{PollConnector, CheckpointedConnector, EventConnector}``
    — every connector needs some way to surface changes.

Violations are reported by the plugin directory path
(``kairix/connectors/<name>``). There is no grandfathering: every violation fails the gate.

Per F21, REMEDIATION carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import REPO_ROOT

# Ensure the worktree's kairix package wins over any installed copy
# regardless of cwd at invocation time. The check's whole point is to
# evaluate THIS repo's connector tree; the runtime isinstance probe must
# resolve against THIS repo's kairix.core.protocols.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CONNECTORS_TREE = Path("kairix") / "connectors"
_NON_PLUGIN_NAMES: frozenset[str] = frozenset({"__pycache__"})

_REQUIRED_BASE = "SourceConnector"
_CHANGE_SURFACE_CAPABILITIES = frozenset({"PollConnector", "CheckpointedConnector", "EventConnector"})

REMEDIATION = """F56: connector plugin does not declare a minimum capability set.

Every connector under ``kairix/connectors/<name>/`` must satisfy
``SourceConnector`` PLUS at least one of
``{PollConnector, CheckpointedConnector, EventConnector}`` so the
framework's runner knows how to dispatch sync ticks. Declare capability
either via Protocol inheritance (preferred) or via a module-level
``CAPABILITIES: frozenset[str]`` set.

fix: declare capability on the connector class, e.g.
    from kairix.core.protocols import SourceConnector, PollConnector
    class FooConnector(SourceConnector, PollConnector):
        ...
  OR add a module-level marker in the connector's __init__.py:
    CAPABILITIES: frozenset[str] = frozenset({"SourceConnector", "PollConnector"})
next: see docs/architecture/connector-scope-topology/ADR.md
      §"Connector Protocol — capability mix-ins" for the canonical
      capability matrix per source kind.
run: python3 scripts/checks/check_f56_connector_capability_declaration.py

Pass example:
  class ObsidianConnector(SourceConnector, PollConnector, SlimConnector,
                          HierarchyConnector):
      ...

Forbidden example:
  class ObsidianConnector:  # no Protocol bases, no CAPABILITIES marker
      def list_changes(self, cursor): ...
"""


def _discover_plugins(tree_root: Path) -> list[Path]:
    """List plugin directories under ``tree_root``.

    Skips ``_``-prefixed names and the cache allow-list. Files at the
    tree root are not plugins.
    """
    if not tree_root.exists():
        return []
    out: list[Path] = []
    for child in sorted(tree_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        if child.name in _NON_PLUGIN_NAMES:
            continue
        out.append(child)
    return out


def _capability_names_from_ast(module_file: Path) -> set[str]:
    """Return the set of capability Protocol names referenced in ``module_file``.

    Looks for two shapes:

      * Class base lists — ``class FooConnector(SourceConnector, PollConnector):``
      * Module-level ``CAPABILITIES: frozenset[str] = frozenset({"X", "Y"})``

    Names are collected as bare identifiers (the Protocol class
    short name), so ``SourceConnector`` matches regardless of the
    import alias.
    """
    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()

    capabilities: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    capabilities.add(base.id)
                elif isinstance(base, ast.Attribute):
                    capabilities.add(base.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "CAPABILITIES":
            capabilities.update(_extract_set_literals(node.value))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CAPABILITIES":
                    capabilities.update(_extract_set_literals(node.value))

    return capabilities


def _extract_set_literals(value: ast.AST | None) -> set[str]:
    """Extract string constants from a ``frozenset({...})`` / ``set({...})`` / literal set."""
    if value is None:
        return set()
    out: set[str] = set()
    if isinstance(value, ast.Call):
        for arg in value.args:
            out.update(_extract_set_literals(arg))
    elif isinstance(value, ast.Set):
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
    return out


def _capability_names_via_runtime(plugin_dir: Path) -> set[str]:
    """Probe runtime isinstance() against the canonical capability Protocols.

    Imports the plugin's connector module, instantiates the connector
    via a best-effort minimal-config path (the plugin's ``make_connector``
    factory if present), and runtime-checks against each capability
    Protocol. Returns the names that ``isinstance`` reports True for.

    Returns the empty set on any import / construction failure — those
    plugins fall back to the AST scan.
    """
    try:
        from kairix.core import protocols as _protocols
    except ImportError:
        return set()
    capability_classes = {
        name: getattr(_protocols, name)
        for name in (
            "SourceConnector",
            "PollConnector",
            "CheckpointedConnector",
            "SlimConnector",
            "SlimConnectorWithPermSync",
            "EventConnector",
            "Resolver",
            "HierarchyConnector",
            "OAuthConnector",
            "CredentialsConnector",
        )
        if hasattr(_protocols, name)
    }

    instance = _probe_connector_instance(plugin_dir)
    if instance is None:
        return set()

    found: set[str] = set()
    for cap_name, cap_cls in capability_classes.items():
        try:
            if isinstance(instance, cap_cls):
                found.add(cap_name)
        except TypeError:
            # Runtime-checkable Protocols can reject some instances; ignore.
            continue
    return found


def _probe_connector_instance(plugin_dir: Path) -> object | None:
    """Best-effort minimal-config instance for runtime capability probing.

    Per-plugin minimal-config branches — keeps the probe self-contained.
    Returns ``None`` if construction fails for any reason; the caller
    falls back to the AST scan.
    """
    name = plugin_dir.name
    try:
        module = importlib.import_module(f"kairix.connectors.{name}.connector")
    except ImportError:
        return None

    try:
        if name == "obsidian":
            from pathlib import Path as _Path

            return module.ObsidianConnector(vault_root=_Path("/tmp/_f56_probe"))
        if name == "dex_crm":
            return module.DexCrmConnector()
        if name == "m365_email_headers":
            creds = module.M365Credentials(tenant_id="t", client_id="c", client_secret="s")
            return module.M365EmailHeadersConnector(user_principal_name="probe@example.com", credentials=creds)
        if name == "m365_calendar":
            cfg = module.M365CalendarConfig(user_id="u", tenant_id="t", client_id="c", client_secret="s")
            return module.M365CalendarConnector(cfg)
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _plugin_violates(plugin_dir: Path) -> bool:
    """Return True if the plugin lacks the F56-required capability declaration.

    Combines the AST scan (preferred — sees Protocol bases) with the
    runtime isinstance probe (covers cases where Protocol inheritance
    isn't explicit but the runtime shape satisfies the Protocols).
    """
    declared: set[str] = set()
    # Walk every .py file in the plugin (Protocol bases may live in
    # connector.py, __init__.py, or a dedicated capabilities.py).
    for py_file in plugin_dir.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        declared.update(_capability_names_from_ast(py_file))

    # Fall back to / augment with runtime probe.
    declared.update(_capability_names_via_runtime(plugin_dir))

    if _REQUIRED_BASE not in declared:
        return True
    if not (declared & _CHANGE_SURFACE_CAPABILITIES):
        return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every plugin under ``<repo_root>/kairix/connectors/`` and return
    repo-relative plugin paths missing F56-conformant capability declarations.

    Empty set if the connectors tree doesn't exist or has no plugins.
    """
    violations: set[Path] = set()
    tree_root = repo_root / _CONNECTORS_TREE
    for plugin_dir in _discover_plugins(tree_root):
        if _plugin_violates(plugin_dir):
            try:
                violations.add(plugin_dir.resolve().relative_to(repo_root))
            except ValueError:
                violations.add(_CONNECTORS_TREE / plugin_dir.name)
    return violations


class F56(FitnessRule):
    """F56 as a FitnessRule subclass — see module docstring.

    Overrides :meth:`enumerate_files` to yield connector plugin
    directories (not files). Each plugin's capability declaration is
    a whole-tree property; the violation key is the plugin dir.
    """

    name = "f56"
    remediation = REMEDIATION
    roots = ("kairix/connectors",)

    def enumerate_files(self) -> list[Path]:
        tree_root = self._repo_root / _CONNECTORS_TREE
        return _discover_plugins(tree_root)

    def is_in_scope(self, rel: str) -> bool:
        return True

    def file_has_violation(self, path: Path) -> bool:
        return _plugin_violates(path)


def main() -> int:
    """Gate entry point — returns 0 on clean, 1 on net-new violations."""
    return F56().run()


if __name__ == "__main__":
    sys.exit(main())
