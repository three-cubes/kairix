"""Centralised path resolution for kairix.

Every module that needs a file path imports from here instead of
hardcoding defaults. Paths are resolved once and cached.

Resolution order (highest wins):
  1. Environment variables (KAIRIX_DOCUMENT_ROOT, KAIRIX_DB_PATH, etc.)
     - KAIRIX_DOCUMENT_ROOT is the canonical env var
  2. Config file paths: section (kairix.config.yaml)
  3. Platform-aware defaults (macOS, Linux, Docker)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def is_docker_runtime_check() -> bool:
    """Detect if running inside a Docker container."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("KAIRIX_DOCKER", "") == "1"
        or os.environ.get("container", "") != ""
    )


def is_service_install() -> bool:
    """Detect if kairix was installed as a system service (/opt/kairix)."""
    return Path("/opt/kairix/.venv").exists()


def default_document_root() -> Path:
    """Platform-appropriate default document store location.

    Docker: /data/documents (bind mount from host)
    Server: /var/lib/kairix/documents (admin configures)
    User (all platforms): ~/Documents (most common document location)
    """
    if is_docker_runtime_check():
        return Path("/data/documents")
    if is_service_install():
        return Path("/var/lib/kairix/documents")
    return Path.home() / "Documents"


def default_data_dir(platform: str = sys.platform) -> Path:
    """Platform-appropriate data directory for DB, vectors, and state.

    Docker: /data/kairix
    Server: /var/lib/kairix
    Linux/macOS user: ~/.local/share/kairix (XDG_DATA_HOME)
    Windows user: %LOCALAPPDATA%/kairix

    ``platform`` defaults to ``sys.platform`` and is exposed as a
    parameter so unit tests can drive the Windows branch on any host
    without patching ``kairix.paths.sys``.
    """
    if is_docker_runtime_check():
        return Path("/data/kairix")
    if is_service_install():
        return Path("/var/lib/kairix")
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".local" / "share" / "kairix"


def default_cache_dir(platform: str = sys.platform) -> Path:
    """Platform-appropriate cache directory for temporary data.

    Docker: /data/kairix (same as data dir)
    Server: /var/cache/kairix
    Linux/macOS user: ~/.cache/kairix (XDG_CACHE_HOME)
    Windows user: %LOCALAPPDATA%/kairix/cache

    ``platform`` defaults to ``sys.platform``; injectable for the same
    reason as ``default_data_dir``.
    """
    if is_docker_runtime_check():
        return Path("/data/kairix")
    if is_service_install():
        return Path("/var/cache/kairix")
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".cache" / "kairix"


def default_workspace_root() -> Path:
    """Platform-appropriate workspace root for agent memory logs."""
    if is_docker_runtime_check():
        return Path("/data/workspaces")
    if is_service_install():
        return Path("/data/workspaces")
    return Path.home() / ".kairix" / "workspaces"


@dataclass(frozen=True)
class KairixPaths:
    """Resolved paths for a kairix deployment.

    Use KairixPaths.resolve() to get paths based on your environment.
    All paths are absolute.
    """

    document_root: Path
    db_path: Path
    log_dir: Path
    workspace_root: Path

    @classmethod
    def resolve(cls) -> KairixPaths:
        """Resolve paths from environment variables, config file, or platform defaults.

        Call this once at startup. The result is cached per process.
        """
        return _resolve_cached()


@lru_cache(maxsize=1)
def _resolve_cached() -> KairixPaths:
    """Internal cached resolution — called by KairixPaths.resolve()."""
    cache_dir = default_cache_dir()

    # Try loading paths from config file
    config_paths = load_paths_from_config()

    document_root = Path(
        os.environ.get("KAIRIX_DOCUMENT_ROOT") or config_paths.get("document_root") or str(default_document_root())
    ).expanduser()

    db_path = Path(
        os.environ.get("KAIRIX_DB_PATH") or config_paths.get("db_path") or str(cache_dir / "index.sqlite")
    ).expanduser()

    log_dir = Path(
        os.environ.get("KAIRIX_LOG_DIR")
        or os.environ.get("LOG_DIR")
        or config_paths.get("log_dir")
        or str(cache_dir / "logs")
    ).expanduser()

    workspace_root = Path(
        os.environ.get("KAIRIX_WORKSPACE_ROOT") or config_paths.get("workspace_root") or str(default_workspace_root())
    ).expanduser()

    return KairixPaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=log_dir,
        workspace_root=workspace_root,
    )


def load_paths_from_config() -> dict[str, str]:
    """Load the paths: section from kairix.config.yaml if it exists."""
    config_path = os.environ.get("KAIRIX_CONFIG_PATH") or "kairix.config.yaml"
    try:
        import yaml

        p = Path(config_path).expanduser()
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            result: dict[str, str] = data.get("paths", {})
            return result
    except Exception:  # noqa: S110 — graceful fallback when config is missing or malformed
        pass
    return {}


def clear_cache() -> None:
    """Clear the cached path resolution. Call after changing env vars in tests."""
    _resolve_cached.cache_clear()


# Convenience functions — import these directly instead of calling KairixPaths.resolve()


def document_root() -> Path:
    """Return the document store root path."""
    return KairixPaths.resolve().document_root


def reference_library_root() -> Path:
    """Reference library root — ships inside the container at /opt/kairix/reference-library."""
    return Path(os.environ.get("KAIRIX_REFLIB_ROOT", "reference-library"))


def bundled_suites_root() -> Path:
    """Bundled benchmark suites — ships inside the container at /opt/kairix/suites."""
    return Path(os.environ.get("KAIRIX_SUITES_ROOT", "suites"))


def worker_state_path() -> Path:
    """Path to the worker state JSON (#224). Sits in the kairix data dir so
    ``docker compose down/up`` preserves restart_count across worker restarts."""
    return default_data_dir() / "worker-state.json"


def worker_pause_flag_path() -> Path:
    """Touch-file checked by the worker each loop iteration (#224 phase 4).

    When present, the worker enters WorkerPhase.PAUSED until the flag is
    removed. ``kairix worker pause/resume`` toggles the file's existence.
    """
    return default_data_dir() / ".worker-paused"


def maintenance_skip_noop_threshold() -> int:
    """#224 phase 2 — number of consecutive no-op embed cycles after which
    the worker also skips the three maintenance scans (entity_seed,
    health_check, wikilinks_inject).

    When embed runs find nothing changed N times in a row, the maintenance
    scans are pointless work; skipping them lets a long-idle shared host
    drop to near-zero CPU/IO until the next document change. Reads
    ``KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD`` (int) — default 10. F4
    keeps the env read centralised here in paths.py.
    """
    raw = os.environ.get("KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD")
    if raw is None:
        return 10
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD=%r is not an int; using default 10",
            raw,
        )
        return 10


def db_path() -> Path:
    """Get the database path."""
    return KairixPaths.resolve().db_path


def log_dir() -> Path:
    """Get the log directory path."""
    return KairixPaths.resolve().log_dir


def workspace_root() -> Path:
    """Get the workspace root path."""
    return KairixPaths.resolve().workspace_root


def summaries_db_path() -> Path:
    """Get the summaries database path.

    Configurable via KAIRIX_SUMMARIES_DB env var.
    Default: ~/.cache/kairix/summaries.db
    """
    return Path(
        os.environ.get(
            "KAIRIX_SUMMARIES_DB",
            str(Path.home() / ".cache" / "kairix" / "summaries.db"),
        )
    )


def set_agent_memory_root_override(root: str) -> None:
    """Set the ``KAIRIX_AGENT_MEMORY_ROOT`` env var so subsequent calls to
    :func:`agent_memory_path` see the override.

    Used by CLI entry points (``kairix brief --memory-root ...``) to thread
    an operator-supplied memory root through the use case. The write
    lives here so F4's "env reads stay in paths.py" gate covers both
    sides of the env-var boundary.
    """
    os.environ["KAIRIX_AGENT_MEMORY_ROOT"] = root


def embed_vector_dims(default: int = 1536) -> int:
    """Embedding vector dimensions — configurable via ``KAIRIX_EMBED_DIMS``.

    Returns the int value of the env var, or ``default`` when unset.
    Reads at call time (not import time) so test fakes that mutate the
    environment win — but production code should treat the value as fixed
    for the lifetime of the process.
    """
    raw = os.environ.get("KAIRIX_EMBED_DIMS")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_DIMS=%r is not an int; using default %d",
            raw,
            default,
        )
        return default


def is_docker_env() -> bool:
    """Return True when running inside a Docker container.

    Detection: ``/.dockerenv`` exists, ``KAIRIX_DOCKER=1``, or the generic
    ``container`` env var is set. Used by factories that want to swap log
    paths between container and host layouts.
    """
    return is_docker_runtime_check()


def log_queries_enabled() -> bool:
    """Privacy-gated query-log toggle: ``KAIRIX_LOG_QUERIES=1`` enables the
    raw-query JSONL emitter. Off by default."""
    return os.environ.get("KAIRIX_LOG_QUERIES") == "1"


def extra_collections() -> list[str]:
    """Operator-supplied extra collection names — ad-hoc additions when
    there's no full config file. Parses ``KAIRIX_EXTRA_COLLECTIONS`` as a
    comma-separated list and returns the non-empty entries.
    """
    raw = os.environ.get("KAIRIX_EXTRA_COLLECTIONS", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def config_path_override() -> str | None:
    """Explicit config path from ``KAIRIX_CONFIG_PATH``, or ``None`` when unset.

    The single source of truth for the env-var override consumed by
    ``kairix.core.search.config_loader.resolve_config_path`` and
    ``load_paths_from_config`` (which still reads via ``os.environ`` to
    avoid a circular import inside this module).
    """
    value = os.environ.get("KAIRIX_CONFIG_PATH")
    return value if value else None


def boards_dir_override() -> Path | None:
    """Operator override for the Kanban boards directory.

    Reads ``KAIRIX_BOARDS_DIR``. Returns ``None`` when unset so callers can
    fall back to ``document_root() / "01-Projects" / "Boards"``.
    """
    raw = os.environ.get("KAIRIX_BOARDS_DIR")
    return Path(raw) if raw else None


def azure_api_version(default: str = "2024-12-01-preview") -> str:
    """Azure OpenAI API version — configurable via ``KAIRIX_AZURE_API_VERSION``."""
    return os.environ.get("KAIRIX_AZURE_API_VERSION", default)


def mcp_port(default: int = 8080) -> int:
    """Resolve the MCP server port from ``KAIRIX_MCP_PORT``, or ``default``.

    Used by both the MCP CLI's auto-detect path and the onboarding probe.
    """
    raw = os.environ.get("KAIRIX_MCP_PORT")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_MCP_PORT=%r is not an int; using default %d",
            raw,
            default,
        )
        return default


def mcp_port_raw() -> str | None:
    """Raw ``KAIRIX_MCP_PORT`` env-var value, or ``None`` when unset.

    Use this when callers need to distinguish "operator set the env var"
    from "fell back to the default" — e.g. argparse-driven flag-vs-env
    precedence in ``kairix mcp serve``.
    """
    raw = os.environ.get("KAIRIX_MCP_PORT")
    return raw if raw else None


def reflib_root_override() -> str | None:
    """Operator override for the reference-library root via
    ``KAIRIX_REFLIB_ROOT``. ``None`` when unset so callers can demand
    ``--reflib-root`` instead of falling back to a baked-in default."""
    value = os.environ.get("KAIRIX_REFLIB_ROOT")
    return value if value else None


def document_root_override() -> str | None:
    """Operator override for the document-root via ``KAIRIX_DOCUMENT_ROOT``.

    Mirrors :func:`reflib_root_override` semantics — returns ``None`` so
    CLI handlers can show a "required flag" message instead of silently
    using a default. The cached :func:`document_root` keeps its own
    independent read (with platform defaults) for non-CLI callers.
    """
    value = os.environ.get("KAIRIX_DOCUMENT_ROOT")
    return value if value else None


def data_dir() -> Path:
    """Public accessor for the platform-aware data dir.

    Wraps the previously-private ``default_data_dir`` so other modules can
    centralise their "log / cache under the kairix data dir" path resolution
    without re-reading ``KAIRIX_DATA_DIR`` (or its legacy fallback) themselves.
    Honours ``KAIRIX_DATA_DIR`` when set — operators occasionally pin the
    data dir directly rather than via Docker / service detection.
    """
    raw = os.environ.get("KAIRIX_DATA_DIR")
    if raw:
        return Path(raw)
    return default_data_dir()


def monitor_log_path() -> Path:
    """Search-monitor JSONL log path.

    Reads ``KAIRIX_MONITOR_LOG`` directly, falling back to
    ``~/.cache/kairix/monitor.jsonl``. Kept as a separate helper from the
    platform-aware :func:`data_dir` because the legacy default predates the
    XDG-aware data-dir resolution and operators are wired to the old path.
    """
    raw = os.environ.get("KAIRIX_MONITOR_LOG")
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "kairix" / "monitor.jsonl"


def search_log_path() -> Path:
    """Query/search-event JSONL log path.

    Order: ``KAIRIX_SEARCH_LOG`` → ``$KAIRIX_DATA_DIR/logs/search.jsonl`` →
    ``~/.cache/kairix/logs/search.jsonl``.
    """
    raw = os.environ.get("KAIRIX_SEARCH_LOG")
    if raw:
        return Path(raw)
    return data_dir() / "logs" / "search.jsonl"


def wikilinks_last_run_path() -> Path:
    """Touch-file recording the wikilinks-inject high-water timestamp.

    Lives under :func:`data_dir` (so ``KAIRIX_DATA_DIR`` overrides honour
    it) and is read by ``kairix wikilinks inject --changed``.
    """
    return data_dir() / "wikilinks-last-run"


def env_file_override() -> str | None:
    """Explicit env-file path for the deployment-check self-load step.

    Reads ``KAIRIX_ENV_FILE``. Returns ``None`` (not ``""``) when unset so
    callers can use ``is None`` / ``or`` without ambiguity.
    """
    value = os.environ.get("KAIRIX_ENV_FILE")
    return value if value else None


def entity_overrides_path() -> Path:
    """Path to the operator-edited entity overrides file.

    Default: ``{document_root}/04-Agent-Knowledge/_entity-overrides.md``.

    Operators add terms the NER model misses or mistypes (e.g. company
    acronyms, project codenames) so ``kairix entity suggest`` picks them
    up. The file format is documented in
    ``docs/user-guide/entity-overrides.md`` — closes #166.

    Override via ``KAIRIX_ENTITY_OVERRIDES_PATH`` for tests and custom
    deployments. The env read stays in this module (F4).
    """
    raw = os.environ.get("KAIRIX_ENTITY_OVERRIDES_PATH")
    if raw:
        return Path(raw).expanduser()
    return document_root() / "04-Agent-Knowledge" / "_entity-overrides.md"


def agent_memory_path(agent: str, *, root: Path | str | None = None) -> Path:
    """Get the memory directory for an agent.

    Default: {document_root}/04-Agent-Knowledge/{agent}/memory
    Override via the ``root`` kwarg, or via ``KAIRIX_AGENT_MEMORY_ROOT``
    env var for custom layouts.

    If the override path already ends with /{agent}/memory (a common
    misuse — passing the full agent-memory path rather than the parent
    of agent directories), the function detects this and returns the
    path as-is rather than double-appending. This is the regression
    guard for the path-doubling bug fixed in #67 / #93 — silently
    handling the misuse with a warning is friendlier than failing.

    ``root`` is the test seam (F2-clean): tests pass an explicit root
    instead of monkeypatching ``KAIRIX_AGENT_MEMORY_ROOT``.
    Production callers leave it None and the env-var path applies.
    """
    if root is None:
        root = os.environ.get("KAIRIX_AGENT_MEMORY_ROOT")
    if root:
        override_path = Path(root)
        if override_path.parts[-2:] == (agent, "memory"):
            logger.warning(
                "agent_memory_path: root override already ends with "
                "/%s/memory; using it as-is to avoid path-doubling. Pass the "
                "parent of the agent directories (e.g. .../04-Agent-Knowledge) "
                "to silence this warning.",
                agent,
            )
            return override_path
        return override_path / agent / "memory"
    return document_root() / "04-Agent-Knowledge" / agent / "memory"
