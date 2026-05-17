"""F27: ``kairix/providers/<name>/**`` may not import another provider.

The three-layer provider-plugin split (see
``docs/architecture/provider-plugin-architecture.md``) treats each
plugin under ``kairix/providers/<name>/`` as independently
shippable — a third party can ``pip install kairix-provider-foo`` and
register a new endpoint family without touching kairix's tree. That
guarantee breaks the moment one plugin imports another: the imported
plugin must then ship alongside, the dependency graph fans out, and
shared concerns leak across plugin boundaries instead of through
``kairix/transport/``.

Allowed from ``kairix/providers/<name>/``:
  - sibling imports within the same plugin (``kairix.providers.<name>.*``)
  - the shared base in ``kairix.providers._base`` (Provider Protocol,
    registry contract — explicitly designed for cross-plugin use)
  - ``kairix.core.*`` (the Protocol surface)
  - ``kairix.transport.*`` (the universal concerns — that's what
    transport exists for)

Rejected from ``kairix/providers/<name>/``:
  - ``from kairix.providers.<other> import ...`` (any other plugin)
  - ``import kairix.providers.<other>`` (any other plugin)

The detector AST-walks every ``.py`` file under ``kairix/providers/``
(skipping ``_base.py`` and ``__init__.py`` at the top level), figures
out which plugin directory owns the file, and flags any import that
points at a sibling plugin. Pre-existing violations are grandfathered
in ``.architecture/baseline/f27-files.txt``.

If ``kairix/providers/`` does not exist (fresh checkout) or contains
no plugin subdirectories, the check passes trivially — F27 only fires
once plugins appear.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate, repo_relative

# Files / directories at the top level of kairix/providers/ that are NOT
# plugins (they're the shared scaffolding the plugin Protocol lives in).
_NON_PLUGIN_ENTRIES: frozenset[str] = frozenset(
    {
        "__init__.py",
        "_base.py",
        "_base",
        "__pycache__",
    }
)

REMEDIATION = """Refactor to remove the cross-provider import — a
plugin must not depend on another plugin.

fix: extract the shared concern to kairix/transport/ (auth resolution,
client pooling, retry/coalesce/cache — anything universal across
endpoint families goes in transport). If the concern is genuinely
provider-specific shape, duplicate it inline rather than importing
another plugin; plugins must stay independently shippable as separate
pip distributions.
next: re-run python3 scripts/checks/check_no_cross_provider.py to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(providers): move <concern> to kairix/transport/"

Pass example:
  # kairix/providers/openai/embed.py
  from kairix.transport.pool import get_openai_client     # shared transport
  from kairix.providers._base import Provider             # shared base

Forbidden example:
  # kairix/providers/openai/embed.py
  from kairix.providers.azure_foundry import auth_header  # F27 — sibling plugin
  import kairix.providers.bedrock.sigv4                   # F27 — sibling plugin

Why: see docs/architecture/provider-plugin-architecture.md - "Plugin
discovery". Each plugin is meant to ship independently (a third party
can pip install kairix-provider-foo with zero kairix changes); a plugin
that imports another can't be split out without dragging its sibling
along, and the dependency graph becomes a tangle that defeats the
plugin model."""


def _plugin_dir_for(path: Path, providers_root: Path) -> str | None:
    """Return the plugin directory name that ``path`` lives in, or None
    if the path sits at the top level of ``providers/`` (so isn't part
    of any plugin) or outside ``providers/`` entirely.

    A "plugin directory" is the first path segment under
    ``kairix/providers/`` for the file's location.
    """
    try:
        rel = path.relative_to(providers_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        # Top-level file (e.g. _base.py, __init__.py) — not inside a
        # plugin directory.
        return None
    return parts[0]


def _is_cross_provider_import(module: str | None, owning_plugin: str) -> bool:
    """True if ``module`` (the source of an Import / ImportFrom node)
    points at a kairix.providers.<other> plugin different from
    ``owning_plugin``.

    ``kairix.providers._base`` and ``kairix.providers`` itself are
    explicitly NOT cross-plugin — they are the shared scaffolding.
    """
    if module is None:
        return False
    prefix = "kairix.providers."
    if not module.startswith(prefix):
        return False
    rest = module[len(prefix) :]
    # Pull out the first segment after kairix.providers. — that's the
    # plugin name (or the shared _base module name).
    head = rest.split(".", 1)[0]
    if not head:
        return False
    if head.startswith("_"):
        # Shared scaffolding (e.g. _base) — explicitly allowed.
        return False
    return head != owning_plugin


def file_has_violation(path: Path, providers_root: Path) -> bool:
    """True if ``path`` (a .py file under a plugin directory) imports
    from another plugin under ``kairix/providers/``.
    """
    owning = _plugin_dir_for(path, providers_root)
    if owning is None:
        # Top-level provider scaffolding — not subject to F27.
        return False
    if owning.startswith("_") or owning in _NON_PLUGIN_ENTRIES:
        return False

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _is_cross_provider_import(node.module, owning):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_cross_provider_import(alias.name, owning):
                    return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every .py file under ``<repo_root>/kairix/providers/<plugin>/``
    and return repo-relative paths of files that import a sibling
    plugin. Empty set if ``kairix/providers/`` is absent or holds no
    plugin subdirectories yet.
    """
    providers_root = repo_root / "kairix" / "providers"
    if not providers_root.exists():
        return set()

    violations: set[Path] = set()
    for path in providers_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if file_has_violation(path, providers_root):
            try:
                violations.add(path.resolve().relative_to(repo_root))
            except ValueError:
                violations.add(repo_relative(path))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f27", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
