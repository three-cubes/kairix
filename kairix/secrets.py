"""
kairix.secrets — load secrets from the vault-agent sidecar secrets file,
and resolve individual secrets by name.

In Docker deployments the vault-agent sidecar fetches secrets from Azure Key
Vault and writes them to /run/secrets/kairix.env (a tmpfs path). This module
reads that file and loads the KEY=VALUE pairs into os.environ so that the rest
of the application can find credentials via the standard env-var paths.

Usage (called at module level before env-var reads):
    from kairix.secrets import load_secrets
    load_secrets()

Semantics for load_secrets():
  - If the secrets file does not exist, this is a no-op (local dev / CI).
  - Environment variables that are already set are never overwritten. This
    preserves the existing priority: direct env overrides > sidecar secrets.
  - Comments (#) and blank lines in the file are ignored.
  - Multiline values are not supported — each secret must fit on one line.
  - Never raises.

The secrets file path can be overridden via KAIRIX_SECRETS_FILE for testing.

Resolution order for get_secret():
  1. Direct env vars (KAIRIX_LLM_API_KEY etc.) — fastest, for tests and local dev
  2. KAIRIX_SECRETS_DIR/kairix.env — Docker sidecar pattern
  3. KAIRIX_KV_NAME env var → az keyvault secret show — VM fallback

Never returns None for a required secret — raises OSError with clear message.
"""

from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SECRETS_FILE = "/run/secrets/kairix.env"
_DEFAULT_SECRETS_DIR = "/run/secrets"

# Map of logical secret name → env var name (provider-agnostic)
_SECRET_ENV_MAP = {
    "kairix-llm-api-key": "KAIRIX_LLM_API_KEY",
    "kairix-llm-endpoint": "KAIRIX_LLM_ENDPOINT",
    "kairix-llm-model": "KAIRIX_LLM_MODEL",
    "kairix-embed-api-key": "KAIRIX_EMBED_API_KEY",
    "kairix-embed-endpoint": "KAIRIX_EMBED_ENDPOINT",
    "kairix-embed-model": "KAIRIX_EMBED_MODEL",
    "kairix-neo4j-password": "KAIRIX_NEO4J_PASSWORD",
}


def load_secrets(path: str | Path | None = None) -> int:
    """
    Load KEY=VALUE pairs from the secrets file into os.environ.

    Args:
        path: Path to the secrets file. Defaults to KAIRIX_SECRETS_FILE env
              var, or /run/secrets/kairix.env if not set.

    Returns:
        Number of environment variables loaded (0 if file absent or empty).
        Never raises.
    """
    if path is None:
        path = os.environ.get("KAIRIX_SECRETS_FILE", _DEFAULT_SECRETS_FILE)
    secrets_path = Path(path)

    if not secrets_path.exists():
        return 0

    count = 0
    try:
        for lineno, line in enumerate(secrets_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                logger.debug("secrets: skipping malformed line %d (no '=')", lineno)
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not key:
                continue
            if key in os.environ:
                # Existing env var takes priority — sidecar secrets are fallback
                continue
            os.environ[key] = value
            count += 1
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("secrets: failed to load secrets file — %s", exc)
        return 0

    if count:
        logger.debug("secrets: loaded %d variable(s)", count)
    return count


@lru_cache(maxsize=1)
def load_secrets_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a secrets file. Cached per path."""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key:
                result[key] = value
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("secrets: failed to parse secrets file — %s", exc)
    return result


def _read_secret_file(name: str) -> str | None:
    """Read a single secret from a per-file secret (Docker secrets pattern).

    Looks for a file named ``name`` in the secrets directory
    (``/run/secrets/`` for Docker, ``~/.config/kairix/secrets/`` for pip).
    Returns the file content stripped of whitespace, or None.
    """
    for secrets_dir in _secret_file_dirs():
        secret_path = Path(secrets_dir) / name
        if secret_path.is_file():
            try:
                value = secret_path.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
    return None


def _secret_file_dirs() -> list[str]:
    """Return directories to scan for per-file secrets, in priority order."""
    dirs = []
    # Explicit override
    override = os.environ.get("KAIRIX_SECRETS_DIR")
    if override:
        dirs.append(override)
    # Docker secrets (tmpfs)
    dirs.append(_DEFAULT_SECRETS_DIR)
    # pip install path (XDG)
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    dirs.append(str(Path(xdg) / "kairix" / "secrets"))
    return dirs


def get_secret(name: str, required: bool = True) -> str | None:
    """
    Resolve a secret by name. Returns value or None (raises if required).

    Resolution order:
      1. Direct env vars (fastest, for tests and CI)
      2. Per-file secret (/run/secrets/<name> or ~/.config/kairix/secrets/<name>)
      3. Bundle file (kairix.env — vault-agent sidecar pattern)
      4. Azure Key Vault CLI fallback (if KAIRIX_KV_NAME set)

    Args:
        name:     Secret name (e.g. "kairix-llm-api-key").
        required: If True and no value found, raises OSError. Default True.

    Returns:
        Secret value string, or None if not found and required=False.

    Raises:
        OSError: When required=True and the secret cannot be resolved.
    """
    env_var = _SECRET_ENV_MAP.get(name)

    # Step 1: direct environment variable
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value

    # Step 2: per-file secret (Docker secrets / XDG config)
    value = _read_secret_file(name)
    if value:
        return value

    # Step 3: legacy bundle file (vault-agent sidecar)
    secrets_dir = os.environ.get("KAIRIX_SECRETS_DIR", _DEFAULT_SECRETS_DIR)
    secrets_file = Path(secrets_dir) / "kairix.env"
    if secrets_file.exists():
        file_secrets = load_secrets_file(secrets_file)
        if env_var and env_var in file_secrets:
            value = file_secrets[env_var]
            if value:
                return value

    # Step 4: Azure Key Vault CLI fallback
    kv_name = os.environ.get("KAIRIX_KV_NAME", "")
    if kv_name:
        try:
            result = subprocess.run(  # noqa: S603 — az keyvault is a trusted CLI binary
                [  # noqa: S607
                    "az",
                    "keyvault",
                    "secret",
                    "show",
                    "--vault-name",
                    kv_name,
                    "--name",
                    name,
                    "--query",
                    "value",
                    "-o",
                    "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()  # nosec: returns secret value to caller (intended)
            else:
                logger.warning("get_secret: KV fetch failed for requested key")
        except (subprocess.SubprocessError, OSError, ValueError):
            logger.warning("get_secret: KV fetch error for requested key")

    # Not found
    if required:
        logger.error("get_secret: required secret not found")
        raise OSError("Required secret not available. Check environment, secrets file, or Key Vault configuration.")
    return None


def refresh_secrets(path: str | Path | None = None) -> int:
    """Clear cached secrets and reload from the secrets file.

    Clears the lru_cache on ``load_secrets_file`` so the next
    ``get_secret`` call re-reads the file. Then calls ``load_secrets``
    to re-populate ``os.environ`` with any new or rotated values.

    Use this after rotating credentials in Azure Key Vault and
    re-fetching the secrets file (e.g., via a cron job or systemd
    timer).

    Returns the number of environment variables loaded.
    """
    load_secrets_file.cache_clear()
    return load_secrets(path)
