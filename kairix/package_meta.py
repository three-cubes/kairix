"""Package-level bootstrap for ``kairix/__init__.py``: version + public API binding.

``kairix/__init__.py`` does two things at import time that can each fail on a
partial install, and must never take the package import down with them:

* resolve ``__version__`` from the installed distribution metadata, falling
  back to ``"0.0.0"`` for an editable install without metadata;
* bind the public API symbols (``SearchResult``, ``RetrievalConfig``,
  ``QueryIntent``) from their home modules, skipping any whose import fails
  (e.g. an optional dependency missing).

Both run through :class:`PackageInitDeps` so the collaborators — the metadata
lookup and the module importer — are injectable. Production uses the stdlib
defaults; a test proves the fallback / skip paths by passing a failing
lookup or importer instead of poisoning ``sys.modules`` and re-importing the
package (F1).
"""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

# The distribution name in pyproject.toml is "Kairix-agentic-knowledge-mgt",
# not "kairix" — querying the wrong name silently fell through to the 0.0.0
# fallback in every install, which surfaced as `kairix --version` reporting
# `kairix 0.0.0` everywhere (Docker, pip install, editable). The Dockerfile
# passes SETUPTOOLS_SCM_PRETEND_VERSION so the wheel's metadata carries the
# real version; this lookup just has to ask for the right name (#267).
DISTRIBUTION_NAME = "Kairix-agentic-knowledge-mgt"

FALLBACK_VERSION = "0.0.0"

# (home module, symbol) for every public name re-exported from ``kairix``.
PUBLIC_API: tuple[tuple[str, str], ...] = (
    ("kairix.core.search.pipeline", "SearchResult"),
    ("kairix.core.search.config", "RetrievalConfig"),
    ("kairix.core.search.intent", "QueryIntent"),
)


@dataclass(frozen=True)
class PackageInitDeps:
    """Injectable collaborators for the ``kairix`` package bootstrap.

    - ``version_lookup``: ``(distribution name) -> version string``.
      Production default is :func:`importlib.metadata.version`.
    - ``import_module``: ``(dotted module name) -> module``. Production
      default is :func:`importlib.import_module`.
    """

    version_lookup: Callable[[str], str] = field(default_factory=lambda: importlib.metadata.version)
    import_module: Callable[[str], ModuleType] = field(default_factory=lambda: importlib.import_module)


def resolve_version(deps: PackageInitDeps | None = None) -> str:
    """Return the installed distribution version, or ``"0.0.0"`` when unknown.

    Any lookup failure (``PackageNotFoundError`` on an editable install without
    metadata, or anything else the metadata backend raises) falls back rather
    than failing the package import.
    """
    deps = deps if deps is not None else PackageInitDeps()
    try:
        return deps.version_lookup(DISTRIBUTION_NAME)
    except Exception:
        return FALLBACK_VERSION


def bind_public_api(namespace: MutableMapping[str, Any], deps: PackageInitDeps | None = None) -> list[str]:
    """Bind every importable :data:`PUBLIC_API` symbol into ``namespace``.

    A symbol whose home module raises ``ImportError`` (or lacks the attribute)
    is skipped, so the package still loads when an optional dependency is
    missing — the symbol simply isn't bound. Returns the names that were bound.
    """
    deps = deps if deps is not None else PackageInitDeps()
    bound: list[str] = []
    for module_name, symbol in PUBLIC_API:
        try:
            namespace[symbol] = getattr(deps.import_module(module_name), symbol)
        except (ImportError, AttributeError):
            continue
        bound.append(symbol)
    return bound
