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

import errno
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    """Path-resolution mode for the kairix self-installer (Plan 1).

    Three modes carve the FHS/XDG path table into distinct prefixes so a
    single ``pip install`` + ``kairix init`` flow can lay down a working
    install with the right ownership + locations for each deployment
    shape:

    - ``system`` — root-owned ``/etc/kairix/``, ``/var/lib/kairix/``,
      ``/var/cache/kairix/``, systemd unit at
      ``/etc/systemd/system/kairix.service``. Selected when ``kairix init``
      runs as root with no ``KAIRIX_CONTAINER`` signal.
    - ``user`` — XDG-rooted user install under ``$XDG_CONFIG_HOME/kairix``,
      ``$XDG_DATA_HOME/kairix``, ``$XDG_CACHE_HOME/kairix``, systemd unit
      at ``~/.config/systemd/user/kairix.service``. No root required.
    - ``container`` — same path layout as system mode (``/etc/kairix``,
      ``/var/lib/kairix``) because the container image owns the root tree.
      Selected when ``KAIRIX_CONTAINER=1`` is set by the Dockerfile.

    See ``docs/architecture/`` (forthcoming installer doc) for the full
    path-resolution table and ``Plan 1`` for the cutover rationale.
    """

    system = "system"
    user = "user"
    container = "container"

    @classmethod
    def detect(cls, env: Mapping[str, str] | None = None) -> Mode:
        """Auto-detect the install mode from the live process environment.

        Resolution order:

        1. ``KAIRIX_CONTAINER`` env var (any non-empty value) → container.
           This is the Dockerfile-controlled signal; never set by tests
           accidentally.
        2. ``os.geteuid() == 0`` → system. Root invocation outside a
           container.
        3. Otherwise → user. Non-root or a platform that lacks
           ``os.geteuid`` (Windows) — falls through to the XDG-rooted
           user layout.

        Args:
            env: Optional env mapping for unit + contract tests to drive
                the parser without mutating ``os.environ`` (mirrors the
                F2-clean test seam used by :func:`is_docker_env`,
                :func:`mcp_endpoint`, :func:`log_queries_enabled`).
                Production callers leave this ``None`` and the live
                ``os.environ`` is read at the F4 boundary.
        """
        e = env if env is not None else os.environ
        if e.get("KAIRIX_CONTAINER"):
            return cls.container
        # Windows lacks ``os.geteuid``; treat as user-mode unconditionally
        # so module import doesn't AttributeError on non-POSIX hosts.
        if not hasattr(os, "geteuid"):
            return cls.user
        return cls.system if os.geteuid() == 0 else cls.user


def _xdg(env_name: str, fallback: str) -> Path:
    """XDG base-dir helper.

    Returns ``$<env_name>`` when set + non-empty, else ``<fallback>`` —
    both expanded through ``Path.expanduser`` so ``~/...`` fallbacks
    resolve. The kairix project subdir is appended by the caller, not
    here, so callers can compose deeper paths off the XDG root.
    """
    raw = os.environ.get(env_name)
    return Path(raw if raw else fallback).expanduser()


# XDG-style user cache directory name (Path.home() / _USER_CACHE_DIR / "kairix").
# Centralised so the path is the same wherever a non-Docker, non-service install
# resolves a cache location.
_USER_CACHE_DIR = ".cache"

# Canonical agent-knowledge directory under the document root.
# Hosts agent memory subtrees and curator-managed config files (notably
# ``_entity-overrides.md``). Extracted to satisfy F17 — three resolvers
# below need to compose this segment and the literal string is otherwise
# duplicated across them.
_AGENT_KNOWLEDGE_DIR = "04-Agent-Knowledge"

# Env-var name for the operator-supplied config file path. Centralised
# so the literal isn't duplicated across the three resolvers that need
# to honour the operator override (F17 hygiene — appears ≥3 times in
# this module once the feature-flag scaffold's config-overlay reader
# lands).
_KAIRIX_CONFIG_PATH_ENV = "KAIRIX_CONFIG_PATH"

# Env-var name for the operator's document root + the stock container
# mount target it points at in the standard compose. Centralised (F17)
# — three resolvers each need both: the cached platform resolution, the
# explicit override accessor, and the setup wizard's container pre-fill.
_KAIRIX_DOCUMENT_ROOT_ENV = "KAIRIX_DOCUMENT_ROOT"
_CONTAINER_DOCUMENTS_MOUNT = "/data/documents"

# Directory name for the reference-library corpus (#450). Centralised (F17)
# — the cache candidate, the /opt install path, the repo-root candidate, the
# CWD fallback, and reference_corpus_install_dir all compose this segment.
_REFERENCE_LIBRARY_DIR = "reference-library"

# FHS-layout roots for the system + container install modes (Plan 1 / Plan 2).
# The Docker image bakes ``KAIRIX_DATA_DIR=/var/lib/kairix`` +
# ``KAIRIX_CACHE_DIR=/var/cache/kairix`` (see Dockerfile), so the standard
# deploy honours the env override; these constants are the matching fallback
# so the legacy platform-aware resolvers (``default_data_dir`` /
# ``default_cache_dir`` / ``default_workspace_root``) land on the SAME FHS
# tree as the per-mode resolvers (``data_dir`` / ``cache_dir`` /
# ``index_path``) instead of the retired ``/data/kairix`` location (#447 /
# PLA-276). Centralised (F17) — each root is referenced by ≥3 resolvers.
_FHS_DATA_DIR = "/var/lib/kairix"
_FHS_CACHE_DIR = "/var/cache/kairix"


def is_docker_runtime_check(*, env: Mapping[str, str] | None = None) -> bool:
    """Detect if running inside a Docker container.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    return os.path.exists("/.dockerenv") or e.get("KAIRIX_DOCKER", "") == "1" or e.get("container", "") != ""


def is_service_install() -> bool:
    """Detect if kairix was installed as a system service (/opt/kairix)."""
    return Path("/opt/kairix/.venv").exists()


def default_document_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate default document store location.

    Docker: /data/documents (bind mount from host)
    Server: /var/lib/kairix/documents (admin configures)
    User (all platforms): ~/Documents (most common document location)

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    if is_docker_runtime_check(env=e):
        return Path(_CONTAINER_DOCUMENTS_MOUNT)
    if is_service_install():
        return Path("/var/lib/kairix/documents")
    return Path.home() / "Documents"


def default_data_dir(platform: str = sys.platform, *, env: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate data directory for DB, vectors, and state.

    Resolution order:
      1. ``KAIRIX_DATA_DIR`` env if set — operator override, always wins
      2. Docker runtime (``KAIRIX_CONTAINER=1``): ``/var/lib/kairix`` (FHS;
         matches the image-baked ``KAIRIX_DATA_DIR`` and the per-mode
         ``data_dir(Mode.container)`` resolver — #447 / PLA-276 retired the
         old ``/data/kairix`` default that landed data off the mounted volume)
      3. Service install (system-mode kairix init): ``/var/lib/kairix``
      4. Windows user: ``%LOCALAPPDATA%/kairix``
      5. ``XDG_DATA_HOME``/kairix when set
      6. ``~/.local/share/kairix`` (XDG default)

    ``platform`` defaults to ``sys.platform`` and is exposed as a
    parameter so unit tests can drive the Windows branch on any host
    without patching ``kairix.paths.sys``. ``env`` is the F2-clean test
    seam (mirrors :func:`is_docker_env` / :func:`Mode.detect`) — production
    callers leave it ``None`` and the live ``os.environ`` is read at this
    paths boundary (F4); tests pass an explicit mapping to drive the
    docker / XDG branches without ``monkeypatch.setenv``.
    """
    e = env if env is not None else os.environ
    override = e.get("KAIRIX_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_docker_env(e):
        return Path(_FHS_DATA_DIR)
    if is_service_install():
        return Path(_FHS_DATA_DIR)
    if platform == "win32":
        local = e.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix"
    xdg = e.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / ".local" / "share" / "kairix"


def default_cache_dir(platform: str = sys.platform, *, env: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate cache directory for temporary data.

    Resolution order:
      1. ``KAIRIX_CACHE_DIR`` env if set — operator override, always wins
      2. Docker runtime (``KAIRIX_CONTAINER=1``): ``/var/cache/kairix`` (FHS;
         matches the image-baked ``KAIRIX_CACHE_DIR`` and the per-mode
         ``cache_dir(Mode.container)`` resolver — #447 / PLA-276 retired the
         old ``/data/kairix`` default)
      3. Service install (system-mode kairix init): ``/var/cache/kairix``
      4. Windows user: ``%LOCALAPPDATA%/kairix/cache``
      5. ``XDG_CACHE_HOME``/kairix when set
      6. ``~/.cache/kairix`` (XDG default)

    ``platform`` defaults to ``sys.platform``; injectable for the same
    reason as ``default_data_dir``. ``env`` is the F2-clean test seam —
    production leaves it ``None`` and the live ``os.environ`` is read here
    (F4); tests pass an explicit mapping to drive the docker / XDG branches.
    """
    e = env if env is not None else os.environ
    override = e.get("KAIRIX_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if is_docker_env(e):
        return Path(_FHS_CACHE_DIR)
    if is_service_install():
        return Path(_FHS_CACHE_DIR)
    if platform == "win32":
        local = e.get("LOCALAPPDATA")
        if local:
            return Path(local) / "kairix" / "cache"
    xdg = e.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "kairix"
    return Path.home() / _USER_CACHE_DIR / "kairix"


def default_workspace_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate workspace root for agent memory logs.

    Docker + service installs land under ``/var/lib/kairix/workspaces`` to
    match the image-baked ``KAIRIX_WORKSPACE_ROOT`` and sit on the same FHS
    data tree as the SQLite index (#447 / PLA-276 retired the old
    ``/data/workspaces`` default that fell off the mounted data volume). The
    user fallback stays ``~/.kairix/workspaces``. ``env`` is the F2-clean
    test seam — production leaves it ``None`` (live ``os.environ`` read here,
    F4); tests pass an explicit mapping to drive the docker branch.
    """
    e = env if env is not None else os.environ
    if is_docker_env(e) or is_service_install():
        return Path(_FHS_DATA_DIR) / "workspaces"
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
    def resolve(cls, *, env: Mapping[str, str] | None = None) -> KairixPaths:
        """Resolve paths from environment variables, config file, or platform defaults.

        Call this once at startup. The result is cached per process.

        ``env`` is the F2-clean test seam: when supplied, resolution reads
        that mapping instead of ``os.environ`` and bypasses the process
        cache (an explicit mapping is a one-off resolution, never the
        process-wide answer). Production callers leave it ``None``.
        """
        if env is not None:
            return _resolve_from_env(env)
        return _resolve_cached()


@lru_cache(maxsize=1)
def _resolve_cached() -> KairixPaths:
    """Internal cached resolution — called by KairixPaths.resolve()."""
    return _resolve_from_env(os.environ)


def _resolve_from_env(env: Mapping[str, str]) -> KairixPaths:
    """Resolve the four deployment paths against ``env`` (uncached)."""
    cache_dir = default_cache_dir(env=env)
    data_dir_default = default_data_dir(env=env)

    # Try loading paths from config file
    config_paths = load_paths_from_config(env=env)

    document_root = Path(
        env.get(_KAIRIX_DOCUMENT_ROOT_ENV) or config_paths.get("document_root") or str(default_document_root(env=env))
    ).expanduser()

    # The primary SQLite index is the source of truth (FTS5 + content_vectors),
    # NOT a regenerable cache — so it falls back to the persistent DATA dir,
    # matching index_path()/vec_index_path() and the image-baked
    # KAIRIX_DB_PATH=/var/lib/kairix/index.sqlite. Resolving it under the
    # cache dir let cache-eviction or a non-persistent cache mount silently
    # drop the index (#447 / PLA-276).
    db_path = Path(
        env.get("KAIRIX_DB_PATH") or config_paths.get("db_path") or str(data_dir_default / "index.sqlite")
    ).expanduser()

    log_dir = Path(
        env.get("KAIRIX_LOG_DIR") or env.get("LOG_DIR") or config_paths.get("log_dir") or str(cache_dir / "logs")
    ).expanduser()

    workspace_root = Path(
        env.get("KAIRIX_WORKSPACE_ROOT") or config_paths.get("workspace_root") or str(default_workspace_root(env=env))
    ).expanduser()

    return KairixPaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=log_dir,
        workspace_root=workspace_root,
    )


def load_paths_from_config(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load the paths: section from kairix.config.yaml if it exists.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    data = load_top_level_config(environ=env) or {}
    raw = data.get("paths", {})
    return raw if isinstance(raw, dict) else {}


def load_top_level_config(*, environ: Mapping[str, str] | None = None) -> dict[str, object] | None:
    """Return the merged ``kairix.config.yaml`` view as a dict, or None.

    Callers that need a top-level block other than ``paths:`` (e.g. the
    ``agents:`` block consumed by
    :func:`kairix.core.agents.scope.load_agent_scopes`) use this helper
    so the env-var read + yaml parse stays inside :mod:`kairix.paths`.
    Returns None when no config file resolves or it is malformed —
    callers fall back to their own default behaviour.

    Overlay-aware (#492): resolution + merge flow through
    :func:`kairix.config_layers.load_merged_mapping`, the SAME layered
    read path the retrieval loader uses — so a setup-wizard save landing
    on the ``KAIRIX_CONFIG_OVERLAY_PATH`` file (e.g. the picked
    ``paths.document_root``) is observed here instead of only the
    read-only-mounted base config. ``environ`` is the F2-clean test
    seam; production callers leave it ``None`` and the live process
    environment is read at this paths boundary (F4).
    """
    from kairix.config_layers import load_merged_mapping

    try:
        env = dict(environ) if environ is not None else None
        data = load_merged_mapping(env=env)
    except Exception:
        # Graceful fallback when config resolution fails; callers tolerate None.
        return None
    return data or None


def clear_cache() -> None:
    """Clear the cached path resolution. Call after changing env vars in tests."""
    _resolve_cached.cache_clear()


# Convenience functions — import these directly instead of calling KairixPaths.resolve()


def document_root(mode: Mode | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """Return the document store root path.

    When ``mode`` is supplied (the installer + contract-test surface),
    return the per-mode default location:

    - ``Mode.system`` → ``/var/lib/kairix/documents``
    - ``Mode.user``   → ``<data_dir(Mode.user)>/documents``
      (``$XDG_DATA_HOME/kairix/documents`` fallback
      ``~/.local/share/kairix/documents``)
    - ``Mode.container`` → ``/data/documents`` (Docker bind-mount target)

    When ``mode`` is ``None`` (existing callers — backwards-compat):
    flow through :meth:`KairixPaths.resolve` so ``KAIRIX_DOCUMENT_ROOT``,
    ``kairix.config.yaml``'s ``paths.document_root``, and the legacy
    platform-aware defaults all keep working unchanged.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    if mode is not None:
        if mode == Mode.system:
            return Path("/var/lib/kairix/documents")
        if mode == Mode.container:
            return Path(_CONTAINER_DOCUMENTS_MOUNT)
        # Mode.user — sit under the user-mode data dir so the document
        # tree co-locates with the SQLite index + vector index.
        return data_dir(Mode.user) / "documents"
    return KairixPaths.resolve(env=env).document_root


def reference_library_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the reference-library corpus root (NOT bundled in the wheel; #450).

    The corpus is ~50 MB of mixed-license documents, so unlike the
    benchmark suites it does not ship inside the wheel. ``kairix
    benchmark install-corpus`` fetches it into the cache candidate
    below; the resolution chain then finds it there on subsequent runs.

    Resolution order (first existing dir wins; the env override wins
    even when its target is missing, so a misconfigured override
    surfaces as an explicit downstream error rather than a silent
    fallback):

      1. ``$KAIRIX_REFLIB_ROOT`` — operator override.
      2. ``<cache-dir>/reference-library`` — where ``install-corpus``
         lands the fetched corpus (= :func:`reference_corpus_install_dir`).
      3. ``/opt/kairix/reference-library`` — the canonical Docker path.
      4. ``<repo-root>/reference-library`` — source-checkout dev UX.
      5. ``reference-library`` — final CWD fallback (legacy behaviour).

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    cache_corpus = default_cache_dir(env=e) / _REFERENCE_LIBRARY_DIR
    installed_corpus = Path("/opt/kairix") / _REFERENCE_LIBRARY_DIR
    repo_root_corpus = Path(__file__).resolve().parent.parent / _REFERENCE_LIBRARY_DIR
    return resolve_first_existing_dir(
        override=e.get("KAIRIX_REFLIB_ROOT"),
        candidates=[cache_corpus, installed_corpus, repo_root_corpus],
        fallback=Path(_REFERENCE_LIBRARY_DIR),
    )


def reference_corpus_install_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Target dir for ``kairix benchmark install-corpus`` (#450).

    Equals the cache-dir candidate :func:`reference_library_root`
    resolves to after a successful install, so a fetch lands exactly
    where the next ``--suite reflib`` run will look for it.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    return default_cache_dir(env=env) / _REFERENCE_LIBRARY_DIR


def resolve_first_existing_dir(
    override: str | None,
    candidates: list[Path],
    fallback: Path,
) -> Path:
    """Return the first usable directory from the resolution chain.

    Used by ``bundled_suites_root`` (and any future shipped-asset
    resolver that needs the same env-override → candidate-list → CWD
    fallback semantics).

    Args:
        override: When non-empty, returned as a ``Path`` immediately.
                  A misconfigured operator override should surface as a
                  downstream ``FileNotFoundError`` rather than silently
                  fall through to a default.
        candidates: Ordered list of paths; the first one whose
                  ``is_dir()`` returns True wins.
        fallback: Returned when ``override`` is empty and no candidate
                  exists on disk. Typically the legacy CWD-relative
                  path so behaviour from before the resolver existed is
                  preserved.

    The helper is pure (no env reads of its own) so tests can drive it
    with crafted ``tmp_path`` candidate lists — no env-var monkeypatch
    needed (F2-clean).
    """
    if override:
        return Path(override)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return fallback


def bundled_suites_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the bundled benchmark suites root.

    Resolution order (first existing path wins; the env-var override
    wins even if its target is missing, so misconfigurations surface as
    explicit ``FileNotFoundError`` downstream rather than silently
    using a fallback):

      1. ``$KAIRIX_SUITES_ROOT`` — operator override.
      2. ``<package>/data/suites/`` — the in-wheel package-data copy
         (#450). Makes a plain ``pip install`` resolve the suites with
         no source checkout and no Docker image present. Derived from
         the kairix package location (``Path(__file__).parent`` = the
         ``kairix`` package root).
      3. ``<repo-root>/suites/`` — when running from a kairix source
         checkout; preserves the dev UX where ``cd`` to the repo finds
         ``./suites/``. Derived from the kairix package location
         (``Path(__file__).parent.parent`` = repo root).
      4. ``/opt/kairix/suites/`` — canonical install path the Docker
         image ships suites at. Closes #268: the host wrapper does
         ``docker exec`` into the container, where the CWD is unrelated
         to where suites live, and the Dockerfile stages suites at
         ``/opt`` so a bare ``docker exec`` still resolves them.
      5. ``./suites/`` — final CWD fallback (legacy behaviour).

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    in_package_suites = Path(__file__).resolve().parent / "data" / "suites"
    repo_root_suites = Path(__file__).resolve().parent.parent / "suites"
    installed_suites = Path("/opt/kairix/suites")
    return resolve_first_existing_dir(
        override=e.get("KAIRIX_SUITES_ROOT"),
        candidates=[in_package_suites, repo_root_suites, installed_suites],
        fallback=Path("suites"),
    )


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


def maintenance_skip_noop_threshold(*, env: Mapping[str, str] | None = None) -> int:
    """#224 phase 2 — number of consecutive no-op embed cycles after which
    the worker also skips the three maintenance scans (entity_seed,
    health_check, wikilinks_inject).

    When embed runs find nothing changed N times in a row, the maintenance
    scans are pointless work; skipping them lets a long-idle shared host
    drop to near-zero CPU/IO until the next document change. Reads
    ``KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD`` (int) — default 10. F4
    keeps the env read centralised here in paths.py.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD")
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


def maintenance_retention_days() -> int:
    """KFEAT-021 Phase 1 — how long pruned vectors stay in the soft-delete table.

    The :class:`kairix.core.maintenance.MaintenanceScheduler` moves orphan
    ``content_vectors`` rows to ``content_vectors_pruned`` before any hard
    delete. Rows whose ``pruned_at`` timestamp is older than this many days
    are hard-deleted on the next tick — the window is the operator's
    recovery affordance.

    Reads ``KAIRIX_MAINTENANCE_RETENTION_DAYS`` (int) — default 7. F4
    keeps the env read centralised here in paths.py.
    """
    raw = os.environ.get("KAIRIX_MAINTENANCE_RETENTION_DAYS")
    if raw is None:
        return 7
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_MAINTENANCE_RETENTION_DAYS=%r is not an int; using default 7",
            raw,
        )
        return 7
    if parsed < 0:
        logger.warning(
            "KAIRIX_MAINTENANCE_RETENTION_DAYS=%r is negative; using default 7",
            raw,
        )
        return 7
    return parsed


def bronze_ttl_days(*, env: Mapping[str, str] | None = None) -> int:
    """#316 — TTL for bronze raw blobs when ``bronze_ttl_gc`` flag is ON.

    The :class:`kairix.core.connectors.bronze.FilesystemBronzeStore`
    :meth:`gc_aged` step deletes bronze_records rows + raw blobs older
    than this many days on every maintenance tick (when the
    ``bronze_ttl_gc`` flag is ON). Bounds bronze growth — without it,
    the SharePoint dogfood corpus accumulated 36 GB of correctly-
    registered blobs that the orphan reaper (#318) couldn't touch.

    Reads ``KAIRIX_BRONZE_TTL_DAYS`` (int) — default 7. F4 keeps the
    env read centralised here in paths.py.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_BRONZE_TTL_DAYS")
    if raw is None:
        return 7
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_BRONZE_TTL_DAYS=%r is not an int; using default 7",
            raw,
        )
        return 7
    if parsed < 0:
        logger.warning(
            "KAIRIX_BRONZE_TTL_DAYS=%r is negative; using default 7",
            raw,
        )
        return 7
    return parsed


def maintenance_interval_seconds() -> int:
    """KFEAT-021 Phase 1 — seconds between maintenance ticks in the worker loop.

    When the ``maintenance_loop`` feature flag is ON, the worker fires a
    :func:`kairix.core.maintenance.scheduler.MaintenanceScheduler.tick`
    every N seconds. Default 86400 (24 h) fits low-write deployments;
    busier ones might tune this to 4-6 hours via the env override.

    Reads ``KAIRIX_MAINTENANCE_INTERVAL_S`` (int seconds) — default
    86400. F4 keeps the env read centralised here in paths.py.
    """
    raw = os.environ.get("KAIRIX_MAINTENANCE_INTERVAL_S")
    if raw is None:
        return 86400
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_MAINTENANCE_INTERVAL_S=%r is not an int; using default 86400",
            raw,
        )
        return 86400
    if parsed <= 0:
        logger.warning(
            "KAIRIX_MAINTENANCE_INTERVAL_S=%r is not positive; using default 86400",
            raw,
        )
        return 86400
    return parsed


def rechunk_sweep_per_tick_cap(*, env: Mapping[str, str] | None = None) -> int:
    """ADR-028 Wave F.4 — max documents the re-chunk sweep scans per tick.

    Bounds the per-tick scan (F66); the sweep walks the rest of the corpus on
    later ticks via its persisted cursor. Default 200 keeps a single tick cheap.

    Reads ``KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP`` (positive int) — default 200.
    F4 keeps the env read centralised here in paths.py.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP")
    if raw is None:
        return 200
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP=%r is not an int; using default 200", raw)
        return 200
    if parsed <= 0:
        logger.warning("KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP=%r is not positive; using default 200", raw)
        return 200
    return parsed


def trace_enabled(*, env: Mapping[str, str] | None = None) -> bool:
    """Return True when ``KAIRIX_TRACE=1`` opts into structured pipeline diagnostics.

    Off by default; production stays quiet. Operators investigating a
    retrieval-vs-synthesis regression set the env var, re-run, and read
    the per-stage counter logs. Centralised here per F4 so KAIRIX_*
    reads stay at the paths.py boundary.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    return e.get("KAIRIX_TRACE") == "1"


def db_path(*, env: Mapping[str, str] | None = None) -> Path:
    """Get the database path.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    return KairixPaths.resolve(env=env).db_path


def log_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Get the log directory path.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    return KairixPaths.resolve(env=env).log_dir


def workspace_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Get the workspace root path.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    return KairixPaths.resolve(env=env).workspace_root


def embedding_cache_path(mode: Mode | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the SQLite-backed persistent embedding cache path.

    When ``mode`` is supplied (the installer + contract-test surface),
    return ``<data_dir(mode)>/cache/embedding_cache.sqlite``. This is
    the per-mode FHS/XDG-aligned location the kairix installer creates.

    When ``mode`` is ``None`` (the runtime default), return
    ``<cache_dir()>/embedding_cache.sqlite`` — honours
    ``KAIRIX_CACHE_DIR`` env and the per-mode FHS/XDG defaults via
    :func:`cache_dir`. **Closes #426**: the previous default put the
    cache under ``<document_root>/.kairix/cache/`` which on production
    deployments IS the operator's synced knowledge store (Obsidian
    vault, Notion export, etc.). 8.7 GB of cache writes were dragged
    into the user's vault sync surface on every embed run.

    The cache is regenerable: relocating to a kairix-controlled
    writable directory means a deployment move no longer needs the
    cache to travel. The embed worker rebuilds from scratch when the
    cache is absent.

    See :mod:`kairix.core.embed.embedding_cache` for cache shape +
    invariants. F4-clean — env reads stay at the paths boundary.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    if mode is not None:
        return data_dir(mode) / "cache" / "embedding_cache.sqlite"
    # default_cache_dir honours KAIRIX_CACHE_DIR env first, then per-mode
    # FHS/XDG defaults (/var/cache/kairix on system + docker installs,
    # ~/.cache/kairix on user installs). cache_dir(mode) is the Plan 1
    # FHS-only resolver that ignores the env override, so we use
    # default_cache_dir here to keep operator overrides functional.
    return default_cache_dir(env=env) / "embedding_cache.sqlite"


def embed_cache_path() -> Path:
    """Resolve the SQLite-backed persistent transport-layer embed cache path.

    Distinct from :func:`embedding_cache_path`:

      - :func:`embedding_cache_path` (``embedding_cache.sqlite`` under
        ``<document_root>/.kairix/cache/``) caches **chunk** embeddings
        keyed on ``(model, dimension, chunk_hash)`` — the embed-ingest
        pipeline's restart-resilient store.
      - :func:`embed_cache_path` (``embed_cache.sqlite`` under
        :func:`data_dir`) caches **query** embeddings keyed on the
        normalised query text — the transport-layer cache that sits
        in front of every search-time embed call.

    Lives under :func:`data_dir` (``/var/lib/kairix`` on v2026.6.8+ FHS
    containers + service installs, ``/data/kairix`` on legacy
    container layouts, XDG data dir for user installs) so it travels
    with the kairix data volume across
    ``docker compose down/up`` cycles. Closes #391: the previous
    in-process LRU implementation lost its entries on every container
    restart, so every release fan-out re-paid the ~250-500 ms Azure
    embed roundtrip for every repeat query.

    F4-clean — the env read for ``KAIRIX_DATA_DIR`` happens inside
    :func:`data_dir` at the paths boundary.
    """
    return data_dir() / "embed_cache.sqlite"


def pipeline_cache_path() -> Path:
    """Resolve the SQLite-backed marker file for the pipeline build cache (#411 Phase 2).

    The pipeline itself can't be persisted — it owns live SQLite handles,
    HNSW indexes, and OAuth tokens. What we persist instead is a marker:
    "config X was last built at time T". The next process consults this
    marker to validate that any persisted query / prep cache entries are
    still keyed against the same pipeline shape.

    Lives under :func:`data_dir` next to :func:`embed_cache_path`, so a
    ``docker compose restart`` (and the cold CLI starts that follow when
    no MCP is up) inherits the marker the previous warm process wrote.

    F4-clean — env reads stay inside :func:`data_dir`.
    """
    return data_dir() / "pipeline_cache.sqlite"


def query_cache_path() -> Path:
    """Resolve the SQLite-backed persistent query-result cache path (#411 Phase 2).

    Sibling to :func:`embed_cache_path`. The query-result cache holds
    full :class:`SearchResult` envelopes keyed on
    ``(cfg_hash, query_hash)``; a cold CLI start that finds a non-expired
    row for the same cfg + query skips the ~1-3 s search dispatch
    entirely.

    Lives under :func:`data_dir` so the file rides the kairix data
    volume across restarts.

    F4-clean — env reads stay inside :func:`data_dir`.
    """
    return data_dir() / "query_cache.sqlite"


def prep_cache_path() -> Path:
    """Resolve the SQLite-backed persistent prep-summary cache path (#411 Phase 2).

    Sibling to :func:`embed_cache_path` and :func:`query_cache_path`.
    The prep cache holds LLM-synthesised summaries keyed on
    ``(cfg_hash, prep_key_hash)`` (where ``prep_key_hash`` folds
    ``(query, tier, context)``). A cold CLI ``kairix prep`` that hits
    a persisted row skips the 2-4 s LLM synthesis call.

    Lives under :func:`data_dir` so the file rides the kairix data
    volume across restarts.

    F4-clean — env reads stay inside :func:`data_dir`.
    """
    return data_dir() / "prep_cache.sqlite"


def summaries_db_path() -> Path:
    """Get the summaries database path.

    Configurable via KAIRIX_SUMMARIES_DB env var.
    Default: ~/.cache/kairix/summaries.db
    """
    return Path(
        os.environ.get(
            "KAIRIX_SUMMARIES_DB",
            str(Path.home() / _USER_CACHE_DIR / "kairix" / "summaries.db"),
        )
    )


def read_int_env(name: str, *, default: int, env: Mapping[str, str] | None = None) -> int:
    """Read an int from the named env var, falling back to ``default``.

    Centralised here so callers needing tunable int knobs do not scatter
    ``os.environ.get`` reads across production modules (F4). Malformed
    values log a warning and fall back to ``default`` — the same
    defensive policy used by the other typed env-var readers above.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using default %d", name, raw, default)
        return default


def read_float_env(name: str, *, default: float, env: Mapping[str, str] | None = None) -> float:
    """Read a float from the named env var, falling back to ``default``.

    Counterpart to :func:`read_int_env` for float-typed knobs (e.g.
    cache TTLs in seconds). F4-clean — env reads stay in this module.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a float; using default %f", name, raw, default)
        return default


def gotenberg_extractor_config() -> Any:
    """Resolve the gotenberg extractor config from the ``KAIRIX_GOTENBERG_*`` env.

    F4 boundary: the gotenberg extractor (``kairix/extractors/gotenberg``)
    must not read ``os.environ`` directly, so its env defaults are
    composed here and returned as a frozen
    :class:`~kairix.extractors.gotenberg.GotenbergExtractorConfig`.
    Unset vars fall back to the dataclass literals; malformed numeric
    values log a warning and fall back too (via :func:`read_float_env`
    / :func:`read_int_env`). The import is local so ``kairix.paths``
    stays free of an extractor-package import at module load.

    Env vars:
      * ``KAIRIX_GOTENBERG_URL`` — conversion service base URL.
      * ``KAIRIX_GOTENBERG_TIMEOUT_S`` — per-convert deadline (float, seconds).
      * ``KAIRIX_GOTENBERG_MAX_FILE_SIZE_MB`` — pre-HTTP size ceiling (int, MiB).
    """
    from kairix.extractors.gotenberg import GotenbergExtractorConfig

    defaults = GotenbergExtractorConfig()
    return GotenbergExtractorConfig(
        gotenberg_url=os.environ.get("KAIRIX_GOTENBERG_URL", defaults.gotenberg_url),
        timeout_s=read_float_env("KAIRIX_GOTENBERG_TIMEOUT_S", default=defaults.timeout_s),
        max_file_size_mb=read_int_env("KAIRIX_GOTENBERG_MAX_FILE_SIZE_MB", default=defaults.max_file_size_mb),
    )


def embed_vector_dims(default: int = 1536, *, env: Mapping[str, str] | None = None) -> int:
    """Embedding vector dimensions — configurable via ``KAIRIX_EMBED_DIMS``.

    Returns the int value of the env var, or ``default`` when unset.
    Reads at call time (not import time) so test fakes that mutate the
    environment win — but production code should treat the value as fixed
    for the lifetime of the process.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_DIMS")
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


def is_docker_env(env: Mapping[str, str] | None = None) -> bool:
    """Return True when running inside a Docker container.

    Detection: ``/.dockerenv`` exists, ``KAIRIX_DOCKER=1``, or the generic
    ``container`` env var is set. Used by factories that want to swap log
    paths between container and host layouts.

    Args:
        env: Optional explicit env mapping (F2-clean test seam). When
            ``None``, the live ``os.environ`` is consulted (production
            default). Tests pass a dict so the env-driven branches can
            be exercised without monkey-patching ``os.environ``.
    """
    if env is None:
        return is_docker_runtime_check()
    return os.path.exists("/.dockerenv") or env.get("KAIRIX_DOCKER", "") == "1" or env.get("container", "") != ""


def log_queries_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Privacy-gated query-log toggle: ``KAIRIX_LOG_QUERIES=1`` enables the
    raw-query JSONL emitter. Off by default.

    Args:
        env: Optional explicit env mapping (F2-clean test seam). When
            ``None``, the live ``os.environ`` is consulted.
    """
    e = env if env is not None else os.environ
    return e.get("KAIRIX_LOG_QUERIES") == "1"


def extra_collections(env: Mapping[str, str] | None = None) -> list[str]:
    """Operator-supplied extra collection names — ad-hoc additions when
    there's no full config file. Parses ``KAIRIX_EXTRA_COLLECTIONS`` as a
    comma-separated list and returns the non-empty entries.

    Args:
        env: Optional explicit env mapping (F2-clean test seam). When
            ``None``, the live ``os.environ`` is consulted.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EXTRA_COLLECTIONS", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def config_path_override() -> str | None:
    """Explicit config path from ``KAIRIX_CONFIG_PATH``, or ``None`` when unset.

    The single source of truth for the env-var override consumed by
    ``kairix.core.search.config_loader.resolve_config_path`` and
    ``load_paths_from_config`` (which still reads via ``os.environ`` to
    avoid a circular import inside this module).
    """
    value = os.environ.get(_KAIRIX_CONFIG_PATH_ENV)
    return value if value else None


def config_overlay_path_override(*, environ: Mapping[str, str] | None = None) -> str | None:
    """Operator overlay config path from ``KAIRIX_CONFIG_OVERLAY_PATH``.

    Returns ``None`` when unset. This is the same env var the layered
    config loader (``kairix.core.search.config_loader.resolve_layered_paths``)
    consumes on the READ side; surfacing it here gives the setup wizard's
    config WRITER one F4-clean place to learn the overlay target — when
    set, wizard saves land on the overlay file (typically on the writable
    data volume) instead of the read-only-mounted base config.

    ``environ`` mirrors :func:`mcp_endpoint`'s F2-clean test seam —
    production callers leave it ``None`` and the live ``os.environ`` is
    read at the paths boundary.
    """
    env = environ if environ is not None else os.environ
    value = env.get("KAIRIX_CONFIG_OVERLAY_PATH")
    return value if value else None


def container_source_prefill(environ: Mapping[str, str] | None = None) -> str | None:
    """Folder path the setup wizard pre-fills when running in a container.

    Inside a container (``KAIRIX_CONTAINER`` set — the Dockerfile-controlled
    signal :meth:`Mode.detect` also keys on), the operator's documents are
    whatever folder the compose file mounted, so the wizard pre-fills the
    configured document root: the ``KAIRIX_DOCUMENT_ROOT`` value, or the
    stock compose mount target ``/data/documents`` when unset.

    Returns ``None`` on non-container installs so the wizard leaves the
    field blank. ``environ`` is the F2-clean test seam; production
    callers leave it ``None`` and the live ``os.environ`` is read at
    the paths boundary (F4).
    """
    env = environ if environ is not None else os.environ
    if not env.get("KAIRIX_CONTAINER"):
        return None
    return env.get(_KAIRIX_DOCUMENT_ROOT_ENV) or _CONTAINER_DOCUMENTS_MOUNT


def boards_dir_override() -> Path | None:
    """Operator override for the Kanban boards directory.

    Reads ``KAIRIX_BOARDS_DIR``. Returns ``None`` when unset so callers can
    fall back to ``document_root() / "01-Projects" / "Boards"``.
    """
    raw = os.environ.get("KAIRIX_BOARDS_DIR")
    return Path(raw) if raw else None


def provider_name() -> str | None:
    """Configured provider plugin name from ``kairix.config.yaml``, or ``None``.

    Reads the top-level ``provider:`` field from the operator's
    ``kairix.config.yaml``. Returns the stripped string when present
    and non-empty; returns ``None`` otherwise so callers that depend
    on a configured plugin can surface a typed
    ``ProviderNotRegistered``-shaped error themselves.

    The seam moved from ``KAIRIX_PROVIDER`` (env var) to the config
    file in v2026.5.17 — operators pick a plugin in config; the plugin
    owns its own credential-retrieval pattern (Azure → Key Vault;
    AWS → Secrets Manager; etc.) so the secrets surface is shaped by
    the plugin, not the env vocabulary. See
    ``docs/architecture/provider-plugin-architecture.md``.

    Lives in :mod:`kairix.paths` so the file-system read stays at the
    F4 boundary even when the underlying source is a yaml file rather
    than ``os.environ``. The import lives inside the function to keep
    ``kairix.paths`` free of a module-level dependency on the
    retrieval-config loader (which itself imports ``kairix.paths`` for
    ``config_path_override``).
    """
    # Lazy import — avoid circular dependency with config_loader, which
    # imports ``config_path_override`` from this module.
    from kairix.core.search.config_loader import load_config

    try:
        cfg = load_config()
    except Exception as exc:
        # YAML parse errors / ConfigValidationError shouldn't crash
        # operator-facing probes; surface ``None`` and let the caller
        # render the actionable affordance.
        logger.warning("provider_name: failed to load kairix.config.yaml — %s", exc)
        return None
    value = getattr(cfg, "provider", None)
    return value if value else None


def azure_api_version(default: str = "2024-12-01-preview") -> str:
    """Azure OpenAI API version — configurable via ``KAIRIX_AZURE_API_VERSION``."""
    return os.environ.get("KAIRIX_AZURE_API_VERSION", default)


def bedrock_region_override() -> str | None:
    """AWS region for the bedrock provider — configurable via ``KAIRIX_BEDROCK_REGION``.

    Overrides whatever the boto3 default credential chain picked
    (``AWS_DEFAULT_REGION`` / ``~/.aws/config``) so operators can pin
    the Bedrock inference region distinct from their AWS control-plane
    region. Returns ``None`` when unset; the bedrock plugin then falls
    back to boto3's resolved region. Lives in :mod:`kairix.paths` per
    F4 — no other module may read ``KAIRIX_*`` env vars.
    """
    value = os.environ.get("KAIRIX_BEDROCK_REGION")
    return value if value else None


def bedrock_embed_model(default: str = "amazon.titan-embed-text-v2:0") -> str:
    """Bedrock embed model id — configurable via ``KAIRIX_BEDROCK_EMBED_MODEL``.

    Defaults to Amazon Titan Text Embeddings V2. Cohere embed models on
    Bedrock (``cohere.embed-*``) are also supported by the plugin's
    body-shape dispatch. Lives in :mod:`kairix.paths` per F4.
    """
    return os.environ.get("KAIRIX_BEDROCK_EMBED_MODEL", default)


def bedrock_chat_model(default: str = "anthropic.claude-3-5-sonnet-20241022-v2:0") -> str:
    """Bedrock chat model id — configurable via ``KAIRIX_BEDROCK_CHAT_MODEL``.

    Defaults to Anthropic Claude 3.5 Sonnet on Bedrock. Only
    ``anthropic.*`` model ids are wired for chat at present; non-
    Anthropic ids surface as a typed ``ClientError`` from
    :meth:`kairix.providers.bedrock.BedrockProvider.chat`. Lives in
    :mod:`kairix.paths` per F4.
    """
    return os.environ.get("KAIRIX_BEDROCK_CHAT_MODEL", default)


def embed_pool_size(default: int = 20, *, env: Mapping[str, str] | None = None) -> int:
    """Max concurrent HTTP connections to the embed provider.

    Configurable via ``KAIRIX_EMBED_POOL_SIZE``. Sized for kairix's teaming
    concurrency profile (20 agents, 5-15 sustained) with headroom. Invalid
    values fall back to ``default`` with a logged warning so a bad operator
    secret can't crash the embed dispatch stage. Read at call time so the
    operator can rotate the value via Key Vault without restarting.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_POOL_SIZE")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_POOL_SIZE=%r is not an int; using default %d",
            raw,
            default,
        )
        return default


def embed_pool_keepalive(default: int = 10, *, env: Mapping[str, str] | None = None) -> int:
    """Max idle HTTP connections kept warm against the embed provider.

    Configurable via ``KAIRIX_EMBED_POOL_KEEPALIVE``. Balances connection
    reuse against socket churn under burst load. Invalid values fall back
    to ``default`` with a logged warning.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_POOL_KEEPALIVE")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_POOL_KEEPALIVE=%r is not an int; using default %d",
            raw,
            default,
        )
        return default


def embed_pool_expiry_s(default: float = 30.0, *, env: Mapping[str, str] | None = None) -> float:
    """Idle-connection expiry (seconds) for the embed-provider pool.

    Configurable via ``KAIRIX_EMBED_POOL_EXPIRY_S``. Invalid values fall
    back to ``default`` with a logged warning.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_POOL_EXPIRY_S")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_POOL_EXPIRY_S=%r is not a float; using default %s",
            raw,
            default,
        )
        return default


def embed_coalesce_window_ms(default: int = 50, *, env: Mapping[str, str] | None = None) -> int:
    """Coalesce window (ms) for the embed request coalescer (#288).

    Configurable via ``KAIRIX_EMBED_COALESCE_WINDOW_MS``. Range 0-500;
    out-of-range values clamp to the bound. ``0`` disables the
    coalescer entirely — useful for low-concurrency deployments and
    debugging. Invalid (non-int) values fall back to ``default`` with
    a logged warning so a typo can't crash the embed dispatch stage.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_COALESCE_WINDOW_MS")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_COALESCE_WINDOW_MS=%r is not an int; using default %d",
            raw,
            default,
        )
        return default
    return max(0, min(500, value))


def embed_coalesce_max_batch(default: int = 16, *, env: Mapping[str, str] | None = None) -> int:
    """Max batch size for the embed request coalescer (#288).

    Configurable via ``KAIRIX_EMBED_COALESCE_MAX_BATCH``. Range 1-64.
    Invalid values fall back to ``default`` with a logged warning.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_EMBED_COALESCE_MAX_BATCH")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "KAIRIX_EMBED_COALESCE_MAX_BATCH=%r is not an int; using default %d",
            raw,
            default,
        )
        return default
    return max(1, min(64, value))


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


# Default MCP endpoint for the CLI-routes-via-MCP dispatcher (#411). The
# CLI does a sub-100ms HEAD probe against ``<endpoint>/healthz/ready``
# and, when responsive, routes subcommands through the warm MCP process
# instead of paying the cold-start cost in-process. F4 keeps the env
# read inside ``paths.py`` rather than scattering it through cli.py.
_DEFAULT_MCP_ENDPOINT = "http://localhost:8080/mcp"
_FALSY_FLAG_VALUES = frozenset({"0", "false", "off", "no"})


def mcp_endpoint(
    default: str = _DEFAULT_MCP_ENDPOINT,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the MCP server endpoint from ``KAIRIX_MCP_ENDPOINT``, or ``default``.

    Used by the CLI dispatcher (#411) to decide whether a warm MCP server
    is reachable. The endpoint is the streamable-HTTP base URL (e.g.
    ``http://localhost:8080/mcp``) — the dispatcher derives the readiness
    probe URL (``<endpoint-host>/healthz/ready``) from this.

    ``environ`` is the test seam: production callers leave it None and
    the function reads ``os.environ`` once. Tests pass an explicit dict
    so F2 (no ``monkeypatch.setenv("KAIRIX_*")``) stays clean — the env
    var read still lives inside paths.py (F4) but the test never has to
    mutate process env to exercise the parsing.
    """
    env = environ if environ is not None else os.environ
    raw = env.get("KAIRIX_MCP_ENDPOINT")
    if raw:
        return raw
    return default


def mcp_routing_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    """Whether CLI-routes-via-MCP dispatch is enabled (#411).

    Defaults to True: when a warm MCP server is detected on
    :func:`mcp_endpoint`, subcommands route through it. Operators that
    want to disable the optimisation entirely (force every CLI call to
    run in-process) set ``KAIRIX_MCP_ROUTING=0``. Any other value (or
    unset) leaves the optimisation on.

    ``environ`` mirrors :func:`mcp_endpoint`'s test seam — production
    leaves it None; tests pass an explicit dict to stay F2-clean.
    """
    env = environ if environ is not None else os.environ
    raw = env.get("KAIRIX_MCP_ROUTING")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY_FLAG_VALUES


def mcp_bind_host(default: str = "localhost", *, environ: Mapping[str, str] | None = None) -> str:
    """Resolve the externally-reachable host for the MCP / wizard URL (#500).

    Operators expose the container's published port on a chosen host via
    ``KAIRIX_MCP_BIND_HOST`` in their compose ``.env`` (e.g.
    ``0.0.0.0`` to bind every interface). That env value is the
    host the published port maps onto, so it is the right base for the
    printed tokened wizard URL — except ``0.0.0.0`` is a bind directive,
    not a reachable address, so it normalises to ``localhost`` for the
    URL an operator pastes into a browser (they reach it through the
    tunnel / mapped host, then the cookie carries them the rest of the
    way). ``None`` / empty falls back to ``default``.

    ``environ`` is the test seam — mirrors :func:`mcp_endpoint`: production
    leaves it None and the function reads ``os.environ`` once (the env
    read still lives inside paths.py per F4); tests pass an explicit dict
    so F2 (no ``monkeypatch.setenv``) stays clean.
    """
    env = environ if environ is not None else os.environ
    raw = env.get("KAIRIX_MCP_BIND_HOST")
    if not raw:
        return default
    host = raw.strip()
    if host in {"0.0.0.0", "::", "[::]"}:  # noqa: S104 — matching a bind-any directive, not binding to it
        return default
    return host


def wizard_tokened_url(
    *,
    token: str,
    host: str | None = None,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Build the one-time tokened wizard URL an operator opens once (#500).

    Shape: ``http://<host>:<port>/setup/?operator_token=<token>``. Opening
    it sets the signed grant cookie (see
    ``kairix.platform.setup.web.routes.OperatorTokenGuard``); the token
    then never needs to appear again. The token is interpolated verbatim
    and the whole URL is the sanctioned onboarding surface — callers that
    print it own the F15 boundary (it carries a live credential, so it
    belongs only in the first-boot operator log, never an app log).

    ``host`` / ``port`` default to :func:`mcp_bind_host` / :func:`mcp_port`
    so the printed URL matches the deployment; ``environ`` is forwarded
    as the test seam.
    """
    resolved_host = host if host is not None else mcp_bind_host(environ=environ)
    resolved_port = port if port is not None else mcp_port()
    return f"http://{resolved_host}:{resolved_port}/setup/?operator_token={token}"


def reflib_root_override() -> str | None:
    """Operator override for the reference-library root via
    ``KAIRIX_REFLIB_ROOT``. ``None`` when unset so callers can demand
    ``--reflib-root`` instead of falling back to a baked-in default."""
    value = os.environ.get("KAIRIX_REFLIB_ROOT")
    return value if value else None


def document_root_override(environ: Mapping[str, str] | None = None) -> str | None:
    """Operator override for the document-root via ``KAIRIX_DOCUMENT_ROOT``.

    Mirrors :func:`reflib_root_override` semantics — returns ``None`` so
    CLI handlers can show a "required flag" message instead of silently
    using a default. The cached :func:`document_root` keeps its own
    independent read (with platform defaults) for non-CLI callers.

    ``environ`` mirrors :func:`container_source_prefill`'s F2-clean test
    seam — production callers leave it ``None`` and the live
    ``os.environ`` is read at the paths boundary (F4). The setup wizard
    uses this to warn when a picked folder is shadowed by the env
    override (#492).
    """
    env = environ if environ is not None else os.environ
    value = env.get(_KAIRIX_DOCUMENT_ROOT_ENV)
    return value if value else None


def data_dir(mode: Mode | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """Public accessor for the kairix data dir, per-mode aware.

    When ``mode`` is supplied explicitly (the installer + contract-test
    surface), dispatch on the Mode enum:

    - ``Mode.system`` → ``/var/lib/kairix``
    - ``Mode.user``   → ``$XDG_DATA_HOME/kairix`` (fallback ``~/.local/share/kairix``)
    - ``Mode.container`` → ``/var/lib/kairix``

    When ``mode`` is ``None`` (existing callers — backwards-compat):

    1. ``KAIRIX_DATA_DIR`` env override wins (legacy pin operators rely on).
    2. Otherwise dispatch on ``Mode.detect()`` via this same function.

    Mode-explicit calls intentionally do NOT consult the env override so
    the contract test in ``tests/contracts/test_path_resolvers_dispatch_per_mode.py``
    can assert per-mode distinctness without any test-env pollution.

    The override is ``~``-expanded, matching :func:`default_data_dir` —
    ``KAIRIX_DATA_DIR=~/kairix-data`` must not resolve to a literal ``~``
    directory under the CWD.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    if mode is not None:
        if mode == Mode.user:
            return _xdg("XDG_DATA_HOME", "~/.local/share") / "kairix"
        # system + container share /var/lib/kairix
        return Path(_FHS_DATA_DIR)
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_DATA_DIR")
    if raw:
        return Path(raw).expanduser()
    return data_dir(Mode.detect(e))


def config_dir(mode: Mode | None = None) -> Path:
    """Per-mode config-directory resolver (Plan 1).

    - ``Mode.system`` → ``/etc/kairix``
    - ``Mode.user``   → ``$XDG_CONFIG_HOME/kairix`` (fallback ``~/.config/kairix``)
    - ``Mode.container`` → ``/etc/kairix``

    ``mode=None`` → auto-detect via :meth:`Mode.detect`. Forthcoming
    installer code uses the per-mode form; existing callers that read
    ``KAIRIX_CONFIG_PATH`` continue to do so via :func:`config_path_override`.
    """
    m = mode or Mode.detect()
    if m == Mode.user:
        return _xdg("XDG_CONFIG_HOME", "~/.config") / "kairix"
    return Path("/etc/kairix")


def cache_dir(mode: Mode | None = None) -> Path:
    """Per-mode cache-directory resolver (Plan 1).

    - ``Mode.system`` → ``/var/cache/kairix``
    - ``Mode.user``   → ``$XDG_CACHE_HOME/kairix`` (fallback ``~/.cache/kairix``)
    - ``Mode.container`` → ``/var/cache/kairix``

    ``mode=None`` → auto-detect via :meth:`Mode.detect`. Existing callers
    that want the legacy platform-aware (incl. Windows ``%LOCALAPPDATA%``)
    layout keep using :func:`default_cache_dir`.
    """
    m = mode or Mode.detect()
    if m == Mode.user:
        return _xdg("XDG_CACHE_HOME", "~/.cache") / "kairix"
    return Path(_FHS_CACHE_DIR)


def briefing_dir(mode: Mode | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the agent-briefing output directory (PLA-267).

    ``<cache_dir>/briefing`` — paths-routed and resolved at call time,
    never at import. The retired writer evaluated ``Path.home()`` at
    module import, which crashed the briefing import on a hardened
    no-HOME deploy; routing through this lazy resolver removes that.

    When ``mode`` is supplied (installer + contract surface) the per-mode
    FHS/XDG cache dir is used with no env read —
    ``/var/cache/kairix/briefing`` for ``system`` / ``container`` (needs
    no HOME), ``$XDG_CACHE_HOME/kairix/briefing`` (fallback
    ``~/.cache/kairix/briefing``) for ``user``.

    When ``mode`` is ``None`` (runtime default) the operator override
    ``KAIRIX_BRIEFING_DIR`` wins when set, else the auto-detected per-mode
    cache dir. The env read lives here in :mod:`kairix.paths` so F4 holds
    — the retired writer read a MISSPELLED ``KAIRIXBRIEFING_DIR`` that
    both bypassed paths.py and evaded the F4 ``KAIRIX_*`` lint.

    ``environ`` mirrors :func:`mcp_endpoint`'s F2-clean test seam:
    production callers leave it ``None`` and the live ``os.environ`` is
    read at this paths boundary; tests pass an explicit mapping so the
    override branch is exercised without mutating process env.
    """
    if mode is not None:
        return cache_dir(mode) / "briefing"
    env = environ if environ is not None else os.environ
    raw = env.get("KAIRIX_BRIEFING_DIR")
    if raw:
        return Path(raw).expanduser()
    return cache_dir() / "briefing"


def runtime_secrets_dir(mode: Mode | None = None) -> Path:
    """Per-mode runtime-secrets directory resolver (Plan 1).

    - ``Mode.system`` → ``/run/secrets/kairix``
    - ``Mode.user``   → ``$XDG_RUNTIME_DIR/kairix/secrets`` (fallback ``/tmp/kairix/secrets``)
    - ``Mode.container`` → ``/run/secrets/kairix``

    ``mode=None`` → auto-detect via :meth:`Mode.detect`. Used by the
    installer to lay down the tmpfs-backed secrets dir under systemd.
    """
    m = mode or Mode.detect()
    if m == Mode.user:
        # XDG_RUNTIME_DIR is mandated by the XDG base-dir spec; the
        # ``/tmp`` fallback is the conventional last-resort when the env
        # var is unset (matches ``man pam_systemd``). S108 fires on the
        # ``/tmp`` literal; suppressed because we're emitting a kairix-
        # owned subdir (``/tmp/kairix/secrets``) per the XDG fallback
        # contract, not creating an unscoped tempfile.
        # Sonar python:S5443 suppressed via sonar-project.properties
        # (xdg-runtime-tmp-fallback entry) — the XDG fallback contract +
        # 0700 subdir creation by kairix.install.dirs means the path is
        # not world-writable in operator use.
        return _xdg("XDG_RUNTIME_DIR", "/tmp") / "kairix" / "secrets"  # noqa: S108 — XDG-spec fallback path
    return Path("/run/secrets/kairix")


def index_path(mode: Mode | None = None) -> Path:
    """Per-mode SQLite index path (Plan 1).

    Returns ``<data_dir(mode)>/index.sqlite``. New resolver; existing
    callers in ``kairix.worker`` and ``kairix.core.embed.embed`` still
    compute this locally from ``db_path.parent`` — those migrate to this
    resolver in subsequent installer-cutover commits.
    """
    return data_dir(mode) / "index.sqlite"


def vec_index_path(mode: Mode | None = None) -> Path:
    """Per-mode usearch vector-index path (Plan 1).

    Returns ``<data_dir(mode)>/vectors.usearch``. Mirrors
    :func:`index_path` — derives per-mode distinctness from
    :func:`data_dir`.
    """
    return data_dir(mode) / "vectors.usearch"


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
    return Path.home() / _USER_CACHE_DIR / "kairix" / "monitor.jsonl"


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


def warm_flag_path(mode: Mode | None = None) -> Path:
    """Path to the cross-process warm-state flag — single env-read boundary
    for ``KAIRIX_WARM_FLAG_PATH``.

    The MCP server writes this flag when it finishes warming;
    ``kairix onboard ready`` (running as the docker healthcheck) reads it
    to decide whether ``docker compose up --wait`` can return.

    The flag lives under :func:`data_dir` — owned by the kairix process
    user, not world-writable — so other users on the host can't poison
    the readiness signal (spoof "ready" by ``touch``\\ ing the path) or
    set up a symlink attack on a predictable ``/tmp`` filename. This is
    the right home in production (mounted as the kairix data volume) and
    in dev (under the per-user XDG / Application Support tree).

    Because the flag now lives on the same persistent volume as the
    SQLite index, a restarted container will see the previous process's
    flag if it shut down without clearing it. The MCP entry point calls
    :func:`kairix.platform.warm.state.reset_warm_state` at startup so a
    cold container correctly reports not-ready until it has actually
    re-warmed.

    Operators with a layout where ``data_dir`` is read-only can set
    ``KAIRIX_WARM_FLAG_PATH`` to relocate the flag — but it must point
    at a writable, non-world-writable location.

    Per-mode form: when ``mode`` is supplied (installer + contract-test
    surface), bypass the env override and return
    ``<data_dir(mode)>/warm.flag`` directly. The env override only
    affects the no-arg form, preserving F2 (no env coupling) for the
    explicit-mode call sites.
    """
    if mode is not None:
        return data_dir(mode) / "warm.flag"
    override = os.environ.get("KAIRIX_WARM_FLAG_PATH", "").strip()
    return Path(override) if override else data_dir() / "warm.flag"


def connector_sync_disabled() -> bool:
    """Return True when ``KAIRIX_CONNECTOR_SYNC_DISABLED`` is set to any truthy value.

    Operators set this to short-circuit the worker's connector sync tick
    without removing the worker dispatch slot — useful on hosts where
    the connector framework is intentionally inert (legacy scanner
    deploys, partial rollouts).

    Accepted truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    Anything else — including unset — is False. F4-clean: the env read
    lives here at the boundary.
    """
    return os.environ.get("KAIRIX_CONNECTOR_SYNC_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def connect_browser_disabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True when ``kairix connect *`` must NOT open a real browser.

    Hard kill-switch on the ``_DefaultBrowser.open`` fallback in every
    ``kairix.connect.oauth2.*`` flow. Production leaves this unset so
    the operator's normal flow opens the consent screen; the pytest
    session in ``tests/conftest.py`` sets it to ``"1"`` at collection
    time so any test that forgets to inject a ``FakeBrowserLauncher``
    (or any subprocess that escapes the ``_inject`` patching pattern)
    is hard-blocked rather than silently firing a real popup against
    the operator's machine.

    Reason for existence: 2026-06-01 incident — agent test runs leaked
    real ``webbrowser.open`` calls with placeholder OAuth ``client_id``s,
    producing a stream of Slack "client_id not valid" approval popups
    on the operator's desktop. The injection seams in each flow are
    correct; this is defence-in-depth so a single missed seam can't
    repeat the symptom.

    Args:
        env: Optional env mapping for unit tests to drive the parser
            without monkeypatching ``os.environ`` (F2-clean). Production
            callers leave this ``None`` and the live ``os.environ`` is
            read at call time.

    Accepted truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    Anything else — including unset — is False. F4-clean: the env read
    lives here at the boundary.
    """
    e = env if env is not None else os.environ
    return e.get("KAIRIX_CONNECT_DISABLE_BROWSER", "").strip().lower() in {"1", "true", "yes"}


def worker_writes_vec_index(*, env: Mapping[str, str] | None = None) -> bool:
    """Return True when the embed-loop worker should write to the usearch ANN index.

    Default is **False** because the in-process usearch writer rebuilds the
    full HNSW graph in RAM on first write per cycle (see issue #335 — the
    1.27M-vector rebuild needs ~7.8 GB resident, blowing past any sane
    worker mem_limit). With the default, the worker writes only to
    ``content_vectors`` in SQLite; the usearch on-disk index is brought
    up to date by ``kairix index-rebuild`` run out-of-band (manual or
    scheduled subprocess with appropriate RAM ceiling).

    Operators who run on hosts where the worker can spare 10+ GiB during
    embed cycles can opt in with ``KAIRIX_WORKER_WRITES_VEC_INDEX=1``;
    everywhere else, treat the usearch index as eventually consistent
    against ``content_vectors``.

    Accepted truthy values: ``1``, ``true``, ``yes`` (case-insensitive).

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    return e.get("KAIRIX_WORKER_WRITES_VEC_INDEX", "").strip().lower() in {"1", "true", "yes"}


def noninteractive_mode() -> bool:
    """Return True when ``KAIRIX_NONINTERACTIVE=1`` is set in the environment.

    Centralised here so destructive CLI surfaces (``kairix store crawl
    --reset``, future bulk-delete primitives) read one canonical boundary
    instead of each scattering an ``os.environ.get`` (F4). Operators set
    this in pipelines / containers where prompting is impossible and the
    ``--confirm`` interlock would otherwise block automation.

    Accepted truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    Anything else — including unset — is False.
    """
    raw = os.environ.get("KAIRIX_NONINTERACTIVE", "").strip().lower()
    return raw in {"1", "true", "yes"}


def preflight_strict() -> bool:
    """Return True when ``KAIRIX_PREFLIGHT_STRICT=1`` makes preflight gaps fatal.

    The worker calls :func:`kairix.core.db.integrity.check_integrity`
    on boot and, by default, logs error-severity gaps but keeps
    running so a slightly-degraded VM doesn't crashloop. Operators
    who want a hard stop on degraded boot set this env var to ``1``
    (typical for staging / canary deploys); production VMs leave it
    unset.

    Accepted truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    Anything else — including unset — is False. F4-clean: the env
    read lives here at the boundary, the worker passes the boolean
    through.
    """
    raw = os.environ.get("KAIRIX_PREFLIGHT_STRICT", "").strip().lower()
    return raw in {"1", "true", "yes"}


def entity_overrides_path(*, document_root_arg: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Path to the operator-edited entity overrides file.

    Default: ``{document_root}/04-Agent-Knowledge/_entity-overrides.md``.

    Operators add terms the NER model misses or mistypes (e.g. company
    acronyms, project codenames) so ``kairix entity suggest`` picks them
    up. The file format is documented in
    ``docs/user-guide/entity-overrides.md`` — closes #166.

    Override via ``KAIRIX_ENTITY_OVERRIDES_PATH`` for tests and custom
    deployments. The env read stays in this module (F4).

    ``document_root_arg`` lets callers (e.g. the store CLI) pin the path
    against a per-invocation document root that does not necessarily
    match the cached default. When supplied, the env-var override still
    wins so operators retain the documented escape hatch.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    raw = e.get("KAIRIX_ENTITY_OVERRIDES_PATH")
    if raw:
        return Path(raw).expanduser()
    base = Path(document_root_arg) if document_root_arg is not None else document_root(env=env)
    return base / _AGENT_KNOWLEDGE_DIR / "_entity-overrides.md"


def feature_flag_override(name: str, *, env: Mapping[str, str] | None = None) -> bool | None:
    """Read the ``KAIRIX_FEATURE_<UPPERCASE>`` env-var override for a flag.

    Returns ``True`` / ``False`` when the env var is set to a recognised
    truthy / falsy value; returns ``None`` when the var is unset (so the
    resolver falls through to the config-overlay layer).

    Accepted truthy values: ``1``, ``true``, ``yes``, ``on`` (case-
    insensitive). Accepted falsy values: ``0``, ``false``, ``no``,
    ``off``. Anything else logs a warning and returns ``None`` so the
    resolver treats the override as absent.

    Lives in :mod:`kairix.paths` per F4 — every ``KAIRIX_*`` env read
    stays at the paths boundary. See
    ``docs/architecture/feature-flag-architecture.md`` §3.4.

    ``env``: F2-clean test seam — ``None`` (production) reads the live
    ``os.environ`` at this paths boundary (F4); tests pass a mapping.
    """
    e = env if env is not None else os.environ
    env_name = f"KAIRIX_FEATURE_{name.upper()}"
    raw = e.get(env_name)
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    logger.warning(
        "%s=%r is not a recognised boolean; ignoring override",
        env_name,
        raw,
    )
    return None


def feature_flag_config_overlay(*, environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Return the ``features:`` section from the merged ``kairix.config.yaml``.

    Returns an empty dict when the section is absent or no config file
    resolves. Non-bool values are coerced through Python's truthiness so
    operators who write ``features: {foo: 1}`` get the intuitive
    interpretation; explicit ``True`` / ``False`` are preserved untouched.

    Overlay-aware (#492): reads through :func:`load_top_level_config`,
    so a ``features:`` block in the operator overlay file
    (``KAIRIX_CONFIG_OVERLAY_PATH``) is honoured on the shipped compose
    — not just the read-only base config. ``environ`` is the F2-clean
    test seam.

    F4 boundary: keeping the config read here means the resolver does not
    grow its own ``os.environ`` / file-system read surface. See
    ``docs/architecture/feature-flag-architecture.md`` §3.4.
    """
    data = load_top_level_config(environ=environ) or {}
    section = data.get("features") or {}
    if not isinstance(section, dict):
        return {}
    return {str(k): bool(v) for k, v in section.items()}


def agent_knowledge_dir_name(*, config: dict[str, Any] | None = None) -> str:
    """Return the agent-knowledge directory name under ``document_root``.

    Default ``"04-Agent-Knowledge"``. Override via ``paths.agent_knowledge_dir``
    in ``kairix.config.yaml`` for vaults that name the tree differently.

    ``config`` is the test seam — production callers leave it ``None`` and
    the helper reads ``kairix.config.yaml`` itself.
    """
    paths_cfg = load_paths_from_config() if config is None else config
    return str(paths_cfg.get("agent_knowledge_dir") or _AGENT_KNOWLEDGE_DIR)


def agent_memory_glob(*, config: dict[str, Any] | None = None) -> str:
    """Glob (relative to the agent-knowledge dir) for memory log files.

    Default ``"**/*.md"`` — any markdown anywhere under the agent-knowledge
    tree counts as a memory log. Operators with a stricter layout pin the
    pattern via ``paths.agent_memory_glob`` in ``kairix.config.yaml``
    (e.g. ``"*/memory/*.md"`` to require ``<agent>/memory/<file>.md``).

    ``config`` is the test seam.
    """
    paths_cfg = load_paths_from_config() if config is None else config
    return str(paths_cfg.get("agent_memory_glob") or "**/*.md")


def agent_conversations_dir(document_root_arg: str | Path | None = None) -> Path:
    """Writable directory for agent-ingested conversation transcripts (PLA-275).

    Conversation markdown written by ``kairix ingest-chat`` / the
    ``ingest_chat`` MCP tool lands under the agent-knowledge submount
    (``{document_root}/04-Agent-Knowledge/conversations``) rather than the
    bare document root. On the standard compose the operator's documents
    bind-mount read-only (``./documents:/data/documents:ro``) while the
    agent-knowledge subtree is overlaid as a *separate writable* mount
    (``./documents/04-Agent-Knowledge:/data/documents/04-Agent-Knowledge``)
    — the one place kairix itself writes (ADR-017). Writing conversations
    here means the ingest succeeds on the stock deploy instead of crashing
    with ``OSError: [Errno 30] Read-only file system``.

    The ``04-Agent-Knowledge`` segment is fixed (not the configurable
    :func:`agent_knowledge_dir_name`) on purpose: the compose writable
    submount is literally that path, so a renamed agent-knowledge dir
    would still land conversations on the read-only base.

    ``document_root_arg`` pins the path against a per-invocation document
    root (typically the injected ``KairixPaths.document_root``); when
    ``None`` the cached default resolves. F94: the path is composed here at
    the paths boundary, never hardcoded as a system path by callers.
    """
    base = Path(document_root_arg) if document_root_arg is not None else document_root()
    return base / _AGENT_KNOWLEDGE_DIR / "conversations"


def agent_conversation_doc_rel_path(conversation_id: str) -> str:
    """Resolvable breadcrumb (document-relative path) for one conversation's markdown (PLA-261).

    ``ingest_chat`` writes each conversation to
    ``{document_root}/04-Agent-Knowledge/conversations/<conversation_id>.md`` and
    the scanner indexes it under the SAME document-root-relative path
    (``documents.path`` — see ``kairix/core/db/scanner.py``). Returning that
    relative path as a fact's ``source_uri`` therefore hands an agent a pointer
    it can actually re-open to verify a recalled fact (the recall→verify→act
    loop #467 broke). Posix separators keep the breadcrumb stable across the
    macOS-author / Linux-deploy split.

    Single source for the ``04-Agent-Knowledge/conversations`` segment so the
    write site (``ingest_chat``) and the read-time resolver
    (``resolve_fact_source_uri``) can never drift (F17).
    """
    return f"{_AGENT_KNOWLEDGE_DIR}/conversations/{conversation_id}.md"


# PR 1.2 / #420 — the legacy memory-root helpers (and their backing env var)
# have been deleted. The hardcoded ``<root>/<agent>/memory`` convention no
# longer reflects production vault shapes (operators run flat ``<agent>/``
# layouts or multi-surface scopes that include workspace directories
# alongside memory). Every callsite now resolves via
# :func:`kairix.core.agents.scope.get_agent_scope`, which reads operator
# config from ``agents:`` / ``agent_defaults:`` in ``kairix.config.yaml``.
# The regression guard against re-adding the legacy helpers lives at
# ``tests/contracts/`` — see the no-legacy-helper contract test there.


# ---------------------------------------------------------------------------
# Path confinement — the canonical home for the allow-list sanitiser pattern
# ---------------------------------------------------------------------------
#
# Two threat shapes share one mechanism:
#
#   * S2083 / S8707 (agentic) path traversal — a CLI flag, MCP tool arg, or
#     suite-YAML field carries ``../../etc/passwd`` (or an absolute escape) and
#     an LLM driving the CLI is tricked into reading/writing outside the
#     legitimate working area.
#
# ``confine_to`` (single root) and ``confine_to_roots`` (allow-list) resolve
# the candidate, collapse ``..`` segments via ``Path.resolve()`` (so a symlink
# inside the root that points out is also caught), and verify the result sits
# under an allowed root — raising :class:`PathTraversalError` BEFORE any
# filesystem call. This is the single canonical implementation;
# ``kairix.quality.eval.security`` re-exports it so the eval module and every
# CLI share one auditable guard. (``kairix.secrets.store`` keeps its own
# secrets-specific allow-list policy.)


class PathTraversalError(ValueError):
    """Raised when a candidate path resolves outside its allowed root(s).

    Subclass of ``ValueError`` so existing ``except ValueError`` blocks around
    path resolution catch it without code churn, while callers that care about
    the distinction can ``except PathTraversalError``.
    """


def confine_to(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` against ``root`` and verify it stays inside ``root``.

    ``candidate`` may be absolute or relative. Symlinks are followed via
    ``Path.resolve()``, so a symlink inside ``root`` that points outside is
    detected. Raises :class:`PathTraversalError` on escape; the caller decides
    whether to log, abort, or fall back.

    Args:
        root:      The allowed root directory. Must exist for ``.resolve()`` to
                   canonicalise correctly; the function does not create it.
        candidate: A user-supplied path string or ``Path`` object.

    Returns:
        The resolved absolute ``Path`` inside ``root``.

    Raises:
        PathTraversalError: when the resolved candidate is not inside the
                            resolved root.
    """
    root_resolved = Path(root).resolve()
    cand = Path(candidate)
    # When candidate is absolute, the / operator returns it unchanged; when
    # relative, it's joined onto root. Either way, ``.resolve()`` then
    # canonicalises so ``..`` segments are collapsed before the check.
    combined = cand if cand.is_absolute() else (root_resolved / cand)
    resolved = combined.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise PathTraversalError(f"Path {str(candidate)!r} escapes allowed root {str(root_resolved)!r}") from e
    return resolved


def agent_cli_roots(*extra: str | Path) -> tuple[Path, ...]:
    """The roots an agent-invoked CLI may legitimately read/write under.

    Defaults to the current working directory, the operator's home, and the
    system temp dir (where ``pytest``'s ``tmp_path`` lives, so outcome tests
    pass unchanged). Pass ``extra`` roots for a surface with an additional
    legitimate base (e.g. the resolved document root). All roots are
    canonicalised so the membership test in :func:`confine_to_roots` is exact.
    """
    roots: list[Path] = [Path.cwd(), Path.home(), Path(tempfile.gettempdir())]
    roots.extend(Path(e) for e in extra)
    return tuple(r.expanduser().resolve() for r in roots)


def confine_to_roots(candidate: str | Path, roots: Sequence[Path]) -> Path:
    """Canonicalise ``candidate`` and verify it sits under one of ``roots``.

    The allow-list generalisation of :func:`confine_to` — the canonical shape
    mirrored from ``kairix.secrets.store._confine_to_allowed_root``. Resolves
    the candidate (collapsing ``..`` and following symlinks) and returns it
    only when it equals, or sits beneath, one of the allowed roots. Raises
    :class:`PathTraversalError` BEFORE any filesystem access otherwise.

    Args:
        candidate: A user-supplied path string or ``Path`` object.
        roots:     Allowed roots; typically :func:`agent_cli_roots`.

    Returns:
        The resolved absolute ``Path`` under one of ``roots``.

    Raises:
        PathTraversalError: when the resolved candidate escapes every root.
    """
    resolved = Path(candidate).expanduser().resolve()
    for root in roots:
        root_resolved = Path(root).expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    roots_repr = ", ".join(str(r) for r in roots)
    raise PathTraversalError(
        f"Path {str(candidate)!r} escapes the allowed roots ({roots_repr}). "
        f"fix: pass a path under your working directory, home, or temp dir. "
        f"next: re-run with a path inside one of those roots."
    )


# ---------------------------------------------------------------------------
# Write-access probing — the canonical "can kairix actually write here?" check
# ---------------------------------------------------------------------------
#
# A memory write (``kairix remember`` / the ``memory_write`` MCP tool) only
# helps an agent if the target directory is actually writable. On hardened /
# least-privilege deployments the agent surface can be a ``:ro`` bind-mount
# (``EROFS``) or owned by another uid (``EACCES``) — the write then fails at
# the worst possible moment. :func:`probe_write_access` is the shared probe the
# ``doctor`` preflight uses to surface that BEFORE an agent tries to write, and
# :func:`write_access_fix_hint` renders the matching F21 ``fix:`` line so both
# the preflight verdict and the live ``remember`` failure speak the same
# actionable language (which path, which permission, how to fix).


@dataclass(frozen=True)
class WriteAccessProbe:
    """Outcome of a write-access probe against a directory (PLA-259).

    Attributes:
        path:       The directory the probe targeted.
        writable:   True when a file could be created (and removed) there.
        reason:     Human-readable cause when ``writable`` is False — the
                    OS ``strerror`` (e.g. ``"Read-only file system"``),
                    or ``""`` on success.
        errno_name: The symbolic errno of the failure (e.g. ``"EROFS"``,
                    ``"EACCES"``, ``"ENOENT"``), or ``""`` on success — lets
                    callers tailor the F21 fix line via
                    :func:`write_access_fix_hint`.
    """

    path: Path
    writable: bool
    reason: str = ""
    errno_name: str = ""


def _probe_failure(target: Path, exc: OSError) -> WriteAccessProbe:
    """Build the not-writable result from an :class:`OSError` (never raises)."""
    code = exc.errno or 0
    return WriteAccessProbe(
        path=target,
        writable=False,
        reason=exc.strerror or str(exc),
        errno_name=errno.errorcode.get(code, ""),
    )


def probe_write_access(path: str | Path, *, create: bool = True) -> WriteAccessProbe:
    """Probe whether kairix can actually create a file under ``path``.

    Attempts to create (then immediately remove) a uniquely-named probe
    file inside ``path``. Returns a structured :class:`WriteAccessProbe`
    rather than raising, so a preflight can render an F21-actionable
    verdict (which path, which permission, how to fix) instead of a silent
    green or an opaque ``OSError`` surfacing later at write time.

    Args:
        path:   The directory to probe.
        create: When True (default), ``mkdir(parents=True)`` the directory
                first — the live ``remember`` write surface is created on
                demand, so this mirrors that. When False (the ``doctor``
                preflight, a non-mutating validator) a missing directory is
                reported as ``ENOENT`` rather than created.

    Returns:
        A :class:`WriteAccessProbe`; ``writable`` is True only when the
        probe file was created and removed cleanly.
    """
    target = Path(path)
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _probe_failure(target, exc)
    elif not target.is_dir():
        return WriteAccessProbe(
            path=target,
            writable=False,
            reason="directory does not exist",
            errno_name=errno.errorcode[errno.ENOENT],
        )
    # A uuid-named probe file avoids collisions and symlink races; it is
    # created in the kairix-owned target dir (not a shared temp dir) and
    # removed immediately. A create OR cleanup failure both read as
    # not-writable, so the probe never raises and leaves no debris on the
    # happy path.
    probe_file = target / f".kairix-write-probe-{uuid.uuid4().hex}"
    try:
        probe_file.touch()
        probe_file.unlink()
    except OSError as exc:
        return _probe_failure(target, exc)
    return WriteAccessProbe(path=target, writable=True)


def write_access_fix_hint(errno_name: str) -> str:
    """Return the F21 ``fix:`` line tailored to a write failure's errno (PLA-259).

    Shared by the live ``remember`` failure envelope and the ``doctor``
    preflight so both speak the same remediation language. The hint names the
    concrete action for the common least-privilege failure shapes
    (``EROFS`` read-only mount, ``EACCES`` / ``EPERM`` wrong ownership) and
    falls back to a generic-but-actionable line otherwise.
    """
    if errno_name == errno.errorcode[errno.EROFS]:
        return (
            "fix: this path is on a read-only mount — mount it read-write "
            "(the standard compose overlays ./documents/04-Agent-Knowledge as a "
            "writable mount) or point the agent surface at a writable path"
        )
    if errno_name in {errno.errorcode[errno.EACCES], errno.errorcode[errno.EPERM]}:
        return (
            "fix: grant the kairix process write access to this directory "
            "(chown/chmod it to the kairix user) or point the agent surface at a "
            "path it owns"
        )
    return "fix: ensure the directory exists and the kairix process can write to it"


# ---------------------------------------------------------------------------
# Writable-memory resolver — preferred overlay with a data-dir fallback
# ---------------------------------------------------------------------------
#
# On the stock compose (ADR-017) the document root mounts read-only and only
# ``04-Agent-Knowledge`` is a *separate writable* overlay. When that overlay
# isn't writable for the agent's uid (``:ro`` mount → EROFS, wrong owner →
# EACCES/EPERM), a memory-write / conversation-ingest would die with an
# ``OSError`` and the agent could persist nothing. :func:`resolve_writable_memory_dir`
# PREFERS the ADR-017 overlay path but, when a real write-access probe reports
# the read-only / permission family, FALLS BACK to a writable location under
# the kairix data dir (the F94 posture — persist through ``kairix.paths``,
# never a read-only path with no fallback) so the write is never lost. The
# fallback base is registered as an extra scan collection by the caller so a
# fallback write stays BM25/vector-searchable immediately.

# The read-only / permission errno family that a data-dir fallback can rescue.
# Any OTHER OSError (ENOSPC disk full, etc.) is NOT masked by a fallback — the
# caller's write hits it and surfaces the real cause, rather than a silent
# fallback papering over a genuinely-broken box.
_MEMORY_WRITE_FALLBACK_ERRNOS = frozenset(
    {
        errno.errorcode[errno.EROFS],
        errno.errorcode[errno.EACCES],
        errno.errorcode[errno.EPERM],
    }
)

# Collection name under which fallback memories/conversations are indexed. It
# is deliberately DISTINCT from the operator's scanned collections: the worker
# full-scan never walks this collection, and per-collection deactivation
# (kairix/core/db/scanner.py) therefore never marks a fallback document
# inactive — a fallback write stays searchable until it is genuinely re-indexed.
AGENT_MEMORY_FALLBACK_COLLECTION = "agent-memory-fallback"


def agent_memory_fallback_root(mode: Mode | None = None) -> Path:
    """Writable data-dir base for agent memory when the preferred overlay is read-only (PLA-296).

    Resolves under :func:`data_dir` (the persistent writable data dir, e.g.
    ``/var/lib/kairix`` on the container mode), so a fallback write lands on a
    surface kairix always owns even when the ``04-Agent-Knowledge`` overlay is
    a ``:ro`` mount or owned by another uid. ``mode`` mirrors :func:`data_dir`'s
    per-mode dispatch; production callers leave it ``None``.
    """
    return data_dir(mode) / "agent-memory"


@dataclass(frozen=True)
class ResolvedMemoryDir:
    """Where an agent memory / conversation write should actually land (PLA-296).

    Attributes:
        write_dir:      The directory to write into — ``preferred_dir`` when it
                        is writable, else ``fallback_dir``.
        preferred_dir:  The ADR-017 intent (``04-Agent-Knowledge/...`` under the
                        document root).
        used_fallback:  True when the preferred overlay was not writable and the
                        data-dir fallback was chosen.
        probe:          The :class:`WriteAccessProbe` for ``preferred_dir`` — the
                        evidence behind the choice.
        scan_root:      The absolute base to register as an
                        :data:`AGENT_MEMORY_FALLBACK_COLLECTION` scan collection
                        when a fallback happened, else ``None`` (no extra
                        collection needed — the preferred path is already scanned).
    """

    write_dir: Path
    preferred_dir: Path
    used_fallback: bool
    probe: WriteAccessProbe
    scan_root: Path | None


def resolve_writable_memory_dir(
    preferred_dir: Path,
    fallback_dir: Path,
    *,
    label: str,
    fallback_scan_root: Path,
    probe_fn: Callable[[str | Path], WriteAccessProbe] = probe_write_access,
) -> ResolvedMemoryDir:
    """Choose a writable directory: prefer ``preferred_dir``, fall back on read-only (PLA-296).

    Probes ``preferred_dir`` with ``probe_fn`` (default
    :func:`probe_write_access`, which creates the directory on demand and
    round-trips a probe file). When it is writable the preferred path is
    returned unchanged — behaviour on a correctly-mounted deploy is identical
    to before this resolver existed. When the probe reports the read-only /
    permission family (:data:`_MEMORY_WRITE_FALLBACK_ERRNOS`) the write would
    otherwise be lost, so ``fallback_dir`` is returned, a LOUD ``WARN`` is
    emitted (naming the surface, the errno, and the F21 fix hint so a
    genuinely-misconfigured box is still surfaced rather than silently masked),
    and ``fallback_scan_root`` is handed back so the caller can register it as
    an extra scan collection. Any other probe failure (e.g. ENOSPC) is NOT
    masked — the preferred path is returned so the caller's write surfaces the
    real error.

    Args:
        preferred_dir:       The ADR-017 intent directory.
        fallback_dir:        The scope-preserving directory under the writable
                             data dir (caller composes it so namespace /
                             engagement isolation is preserved on the fallback).
        label:               Human tag for the WARN (e.g. ``"agent 'shape'"``).
        fallback_scan_root:  The absolute base registered as the fallback scan
                             collection when a fallback happens.
        probe_fn:            Write-access probe seam — production default
                             :func:`probe_write_access`; tests inject a fake to
                             drive the writable / read-only branches.
    """
    probe = probe_fn(preferred_dir)
    if probe.writable:
        return ResolvedMemoryDir(
            write_dir=preferred_dir,
            preferred_dir=preferred_dir,
            used_fallback=False,
            probe=probe,
            scan_root=None,
        )
    if probe.errno_name not in _MEMORY_WRITE_FALLBACK_ERRNOS:
        # Not a read-only / permission failure — do not paper over it with a
        # fallback; let the caller's write hit the same OSError and report it.
        return ResolvedMemoryDir(
            write_dir=preferred_dir,
            preferred_dir=preferred_dir,
            used_fallback=False,
            probe=probe,
            scan_root=None,
        )
    logger.warning(
        "agent memory surface for %s is not writable: %s [%s] at %s — falling back to the "
        "writable data dir at %s so the write is not lost. This box is misconfigured: %s",
        label,
        probe.reason,
        probe.errno_name,
        preferred_dir,
        fallback_dir,
        write_access_fix_hint(probe.errno_name),
    )
    return ResolvedMemoryDir(
        write_dir=fallback_dir,
        preferred_dir=preferred_dir,
        used_fallback=True,
        probe=probe,
        scan_root=fallback_scan_root,
    )
