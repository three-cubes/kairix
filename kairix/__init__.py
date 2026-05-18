"""kairix — Shared knowledge layer for human-agent teams."""

try:
    from importlib.metadata import version

    # The distribution name in pyproject.toml is
    # "Kairix-agentic-knowledge-mgt", not "kairix" — querying the wrong
    # name silently fell through to the 0.0.0 fallback in every install,
    # which surfaced as `kairix --version` reporting `kairix 0.0.0`
    # everywhere (Docker, pip install, editable). The Dockerfile passes
    # SETUPTOOLS_SCM_PRETEND_VERSION so the wheel's metadata carries the
    # real version; this lookup just had to ask for the right name (#267).
    __version__ = version("Kairix-agentic-knowledge-mgt")
except Exception:
    __version__ = "0.0.0"  # fallback for editable installs without metadata

__all__ = ["QueryIntent", "RetrievalConfig", "SearchResult", "__version__"]

# Public API surface — guarded so the package loads even when optional deps
# (e.g. neo4j) are missing.
try:
    from kairix.core.search.pipeline import SearchResult
except ImportError:
    pass

try:
    from kairix.core.search.config import RetrievalConfig
except ImportError:
    pass

try:
    from kairix.core.search.intent import QueryIntent
except ImportError:
    pass
