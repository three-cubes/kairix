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


def _is_docker() -> bool:
    """Detect if running inside a Docker container."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("KAIRIX_DOCKER", "") == "1"
        or os.environ.get("container", "") != ""
    )


def _is_service_install() -> bool:
    """Detect if kairix was installed as a system service (/opt/kairix)."""
    return Path("/opt/kairix/.venv").exists()


def _default_document_root() -> Path:
    """Platform-appropriate default document store location.

    Docker: /data/documents (bind mount from host)
    Server: /var/lib/kairix/documents (admin configures)
    User (all platforms): ~/Documents (most common document location)
    """
    if _is_docker():
        return Path("/data/documents")
    if _is_service_install():
        return Path("/var/lib/kairix/documents")
    return Path.home() / "Documents"


def _default_data_dir() -> Path:
    """Platform-appropriate data directory for DB, vectors, and state.

    Docker: /data/kairix
    Server: /var/lib/kairix
    Linux/macOS user: ~/.local/share/kairix (XDG_DATA_HOME)
    Windows user: %LOCALAPPDATA%/kairix
    """
    if _is_docker():
        return Path("/data/kairix")
    if _is_service_install():
        return Path("/var/lib/kairix")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".local" / "share" / "kairix"


def _default_cache_dir() -> Path:
    """Platform-appropriate cache directory for temporary data.

    Docker: /data/kairix (same as data dir)
    Server: /var/cache/kairix
    Linux/macOS user: ~/.cache/kairix (XDG_CACHE_HOME)
    Windows user: %LOCALAPPDATA%/kairix/cache
    """
    if _is_docker():
        return Path("/data/kairix")
    if _is_service_install():
        return Path("/var/cache/kairix")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".cache" / "kairix"


def _default_workspace_root() -> Path:
    """Platform-appropriate workspace root for agent memory logs."""
    if _is_docker():
        return Path("/data/workspaces")
    if _is_service_install():
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
    cache_dir = _default_cache_dir()

    # Try loading paths from config file
    config_paths = _load_paths_from_config()

    document_root = Path(
        os.environ.get("KAIRIX_DOCUMENT_ROOT") or config_paths.get("document_root") or str(_default_document_root())
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
        os.environ.get("KAIRIX_WORKSPACE_ROOT") or config_paths.get("workspace_root") or str(_default_workspace_root())
    ).expanduser()

    return KairixPaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=log_dir,
        workspace_root=workspace_root,
    )


def _load_paths_from_config() -> dict[str, str]:
    """Load the paths: section from kairix.config.yaml if it exists."""
    config_path = os.environ.get("KAIRIX_CONFIG_PATH", "kairix.config.yaml")
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
    return _default_data_dir() / "worker-state.json"


def worker_pause_flag_path() -> Path:
    """Touch-file checked by the worker each loop iteration (#224 phase 4).

    When present, the worker enters WorkerPhase.PAUSED until the flag is
    removed. ``kairix worker pause/resume`` toggles the file's existence.
    """
    return _default_data_dir() / ".worker-paused"


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
