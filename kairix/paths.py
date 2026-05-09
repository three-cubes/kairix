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
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_docker(
    env: Mapping[str, str] | None = None,
    dockerenv_exists: bool | None = None,
) -> bool:
    """Detect if running inside a Docker container.

    ``env`` defaults to ``os.environ``. ``dockerenv_exists`` defaults to a
    real check on ``/.dockerenv`` — tests pass a bool to skip the FS check.
    """
    if env is None:
        env = os.environ
    if dockerenv_exists is None:
        dockerenv_exists = os.path.exists("/.dockerenv")
    return dockerenv_exists or env.get("KAIRIX_DOCKER", "") == "1" or env.get("container", "") != ""


def _is_service_install(venv_exists: bool | None = None) -> bool:
    """Detect if kairix was installed as a system service (/opt/kairix).

    ``venv_exists`` defaults to a real check on ``/opt/kairix/.venv``.
    """
    if venv_exists is None:
        venv_exists = Path("/opt/kairix/.venv").exists()
    return venv_exists


def _default_document_root(
    env: Mapping[str, str] | None = None,
    is_docker: bool | None = None,
    is_service_install: bool | None = None,
) -> Path:
    """Platform-appropriate default document store location.

    Docker: /data/documents (bind mount from host)
    Server: /var/lib/kairix/documents (admin configures)
    User (all platforms): ~/Documents (most common document location)
    """
    if env is None:
        env = os.environ
    if is_docker is None:
        is_docker = _is_docker(env)
    if is_service_install is None:
        is_service_install = _is_service_install()
    if is_docker:
        return Path("/data/documents")
    if is_service_install:
        return Path("/var/lib/kairix/documents")
    return Path.home() / "Documents"


def _default_data_dir(
    env: Mapping[str, str] | None = None,
    is_docker: bool | None = None,
    is_service_install: bool | None = None,
    platform: str | None = None,
) -> Path:
    """Platform-appropriate data directory for DB, vectors, and state.

    Docker: /data/kairix
    Server: /var/lib/kairix
    Linux/macOS user: ~/.local/share/kairix (XDG_DATA_HOME)
    Windows user: %LOCALAPPDATA%/kairix
    """
    if env is None:
        env = os.environ
    if is_docker is None:
        is_docker = _is_docker(env)
    if is_service_install is None:
        is_service_install = _is_service_install()
    if platform is None:
        platform = sys.platform
    if is_docker:
        return Path("/data/kairix")
    if is_service_install:
        return Path("/var/lib/kairix")
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix"
    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".local" / "share" / "kairix"


def _default_cache_dir(
    env: Mapping[str, str] | None = None,
    is_docker: bool | None = None,
    is_service_install: bool | None = None,
    platform: str | None = None,
) -> Path:
    """Platform-appropriate cache directory for temporary data.

    Docker: /data/kairix (same as data dir)
    Server: /var/cache/kairix
    Linux/macOS user: ~/.cache/kairix (XDG_CACHE_HOME)
    Windows user: %LOCALAPPDATA%/kairix/cache
    """
    if env is None:
        env = os.environ
    if is_docker is None:
        is_docker = _is_docker(env)
    if is_service_install is None:
        is_service_install = _is_service_install()
    if platform is None:
        platform = sys.platform
    if is_docker:
        return Path("/data/kairix")
    if is_service_install:
        return Path("/var/cache/kairix")
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix" / "cache"
    xdg = env.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".cache" / "kairix"


def _default_workspace_root(
    env: Mapping[str, str] | None = None,
    is_docker: bool | None = None,
    is_service_install: bool | None = None,
) -> Path:
    """Platform-appropriate workspace root for agent memory logs."""
    if env is None:
        env = os.environ
    if is_docker is None:
        is_docker = _is_docker(env)
    if is_service_install is None:
        is_service_install = _is_service_install()
    if is_docker:
        return Path("/data/workspaces")
    if is_service_install:
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
    def resolve(cls, env: Mapping[str, str] | None = None) -> KairixPaths:
        """Resolve paths from environment variables, config file, or platform defaults.

        Call this once at startup. The default ``env=None`` reads
        ``os.environ`` and caches the result per process. Tests pass an
        explicit env mapping (cache is bypassed for non-default env).
        """
        if env is None:
            return _resolve_cached()
        return _resolve(env)


@lru_cache(maxsize=1)
def _resolve_cached() -> KairixPaths:
    """Cached no-arg resolution against ``os.environ``."""
    return _resolve(os.environ)


def _resolve(env: Mapping[str, str]) -> KairixPaths:
    """Resolve KairixPaths from an explicit env mapping. No caching."""
    cache_dir = _default_cache_dir(env=env)

    # Try loading paths from config file
    config_paths = _load_paths_from_config(env=env)

    document_root = Path(
        env.get("KAIRIX_DOCUMENT_ROOT") or config_paths.get("document_root") or str(_default_document_root(env=env))
    ).expanduser()

    db_path = Path(
        env.get("KAIRIX_DB_PATH") or config_paths.get("db_path") or str(cache_dir / "index.sqlite")
    ).expanduser()

    log_dir = Path(
        env.get("KAIRIX_LOG_DIR") or env.get("LOG_DIR") or config_paths.get("log_dir") or str(cache_dir / "logs")
    ).expanduser()

    workspace_root = Path(
        env.get("KAIRIX_WORKSPACE_ROOT") or config_paths.get("workspace_root") or str(_default_workspace_root(env=env))
    ).expanduser()

    return KairixPaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=log_dir,
        workspace_root=workspace_root,
    )


def _load_paths_from_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load the paths: section from kairix.config.yaml if it exists."""
    if env is None:
        env = os.environ
    config_path = env.get("KAIRIX_CONFIG_PATH", "kairix.config.yaml")
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


def document_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the document store root path."""
    return KairixPaths.resolve(env=env).document_root


def reference_library_root(env: Mapping[str, str] | None = None) -> Path:
    """Reference library root — ships inside the container at /opt/kairix/reference-library."""
    if env is None:
        env = os.environ
    return Path(env.get("KAIRIX_REFLIB_ROOT", "reference-library"))


def db_path(env: Mapping[str, str] | None = None) -> Path:
    """Get the database path."""
    return KairixPaths.resolve(env=env).db_path


def log_dir(env: Mapping[str, str] | None = None) -> Path:
    """Get the log directory path."""
    return KairixPaths.resolve(env=env).log_dir


def workspace_root(env: Mapping[str, str] | None = None) -> Path:
    """Get the workspace root path."""
    return KairixPaths.resolve(env=env).workspace_root


def summaries_db_path(env: Mapping[str, str] | None = None) -> Path:
    """Get the summaries database path.

    Configurable via KAIRIX_SUMMARIES_DB env var.
    Default: ~/.cache/kairix/summaries.db
    """
    if env is None:
        env = os.environ
    return Path(
        env.get(
            "KAIRIX_SUMMARIES_DB",
            str(Path.home() / ".cache" / "kairix" / "summaries.db"),
        )
    )


def agent_memory_path(agent: str, env: Mapping[str, str] | None = None) -> Path:
    """Get the memory directory for an agent.

    Default: {document_root}/04-Agent-Knowledge/{agent}/memory
    Override with KAIRIX_AGENT_MEMORY_ROOT env var for custom layouts.

    If the override path already ends with /{agent}/memory (a common
    misuse — passing the full agent-memory path rather than the parent
    of agent directories), the function detects this and returns the
    path as-is rather than double-appending. This is the regression
    guard for the path-doubling bug fixed in #67 / #93 — silently
    handling the misuse with a warning is friendlier than failing.

    ``env`` is a DI seam (defaults to ``os.environ``); tests pass an
    explicit mapping rather than monkeypatching the process environment.
    """
    if env is None:
        env = os.environ
    override = env.get("KAIRIX_AGENT_MEMORY_ROOT")
    if override:
        override_path = Path(override)
        if override_path.parts[-2:] == (agent, "memory"):
            logger.warning(
                "agent_memory_path: KAIRIX_AGENT_MEMORY_ROOT already ends with "
                "/%s/memory; using it as-is to avoid path-doubling. Pass the "
                "parent of the agent directories (e.g. .../04-Agent-Knowledge) "
                "to silence this warning.",
                agent,
            )
            return override_path
        return override_path / agent / "memory"
    return document_root(env=env) / "04-Agent-Knowledge" / agent / "memory"
