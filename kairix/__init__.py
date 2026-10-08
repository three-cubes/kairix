"""kairix — Shared knowledge layer for human-agent teams."""

from typing import TYPE_CHECKING

from kairix.package_meta import bind_public_api, resolve_version

# Falls back to "0.0.0" for editable installs without metadata (see
# kairix/package_meta.py for the distribution-name history, #267).
__version__ = resolve_version()

__all__ = ["QueryIntent", "RetrievalConfig", "SearchResult", "__version__"]

# Public API surface — guarded so the package loads even when optional deps
# (e.g. neo4j) are missing: a symbol whose module fails to import is simply
# not bound.
if TYPE_CHECKING:
    from kairix.core.search.config import RetrievalConfig
    from kairix.core.search.intent import QueryIntent
    from kairix.core.search.pipeline import SearchResult
else:
    bind_public_api(globals())
