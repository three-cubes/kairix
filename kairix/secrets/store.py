"""Write-side persistence for canonical kairix secrets.

:class:`kairix.secrets.SecretsLoader` is the read side; this module is
the matching write side. ``kairix secrets set`` (CLI), the setup
wizard, and ``kairix connect``'s :class:`FileTokenStore` all route
through :func:`set_secret`, so there is exactly one upsert site for the
operator bundle file.

Multi-line values (the GitHub App PEM private key is the canonical
case) are stored newline-safe: :func:`set_secret` encodes them to one
quoted, escaped line via
:func:`kairix.secrets.encoding.encode_bundle_value`; the bundle parse
sites in :mod:`kairix.secrets._legacy` decode symmetrically, so every
resolver downstream sees the original value byte-for-byte.

Path resolution is shared with the read side: :func:`resolve_bundle_path`
implements the single resolution rule (``$KAIRIX_SECRETS_FILE`` →
container ``/run/secrets/kairix.env`` → pip-install
``~/.config/kairix/secrets/kairix.env``), and
:func:`kairix.secrets._legacy.load_secrets` resolves through the same
function. A value persisted here is therefore hydrated into the process
environment at the next CLI/worker/MCP boot via
:func:`kairix.secrets.bootstrap.bootstrap_secrets`, where the canonical
:class:`SecretsLoader` resolves it.

Per F4, this module reads ``KAIRIX_SECRETS_FILE`` / ``XDG_CONFIG_HOME``
directly — it lives under ``kairix/secrets/``, the allow-listed env
boundary. The ``env`` / ``home`` / ``container_dir`` keyword arguments
are the F2-clean test seams (same shape as ``SecretsLoader(env=...)``).

F15: no function in this module logs, prints, or interpolates the
secret value — error messages name the secret SLOT, never its value.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from kairix.secrets.encoding import encode_bundle_value, split_bundle_lines
from kairix.secrets.naming import canonical_env_var, parse_canonical_name

# Container layout: the Docker-secrets tmpfs dir. When this directory
# exists we are in a container/sidecar deployment and the bundle lives
# inside it; otherwise the pip-install XDG layout applies.
_CONTAINER_SECRETS_DIR = Path("/run/secrets")
_BUNDLE_FILENAME = "kairix.env"

# Two canonical example names quoted by every naming-rejection message.
_EXAMPLE_NAMES = ("kairix-provider-llm-api-key", "kairix-connector-github-pat")

_RUN_VERIFY = "run: kairix secrets verify"


def resolve_bundle_path(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    container_dir: Path | None = None,
) -> Path:
    """Resolve the operator secrets bundle path — one rule for read + write.

    Resolution order:

      1. ``$KAIRIX_SECRETS_FILE`` — explicit operator override.
      2. ``/run/secrets/kairix.env`` — when ``/run/secrets`` exists
         (container / vault-agent sidecar layout).
      3. ``$XDG_CONFIG_HOME/kairix/secrets/kairix.env`` (default
         ``~/.config/...``) — pip-install layout, matching the
         ``kairix connect`` file store default.

    Args:
        env: Override the env mapping (test seam). Defaults to
            ``os.environ``.
        home: Override the home directory (test seam). Defaults to
            ``Path.home()``.
        container_dir: Override the container secrets dir probe (test
            seam). Defaults to ``/run/secrets``.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    override = env_map.get("KAIRIX_SECRETS_FILE")
    if override:
        return Path(override)
    probe_dir = container_dir if container_dir is not None else _CONTAINER_SECRETS_DIR
    if probe_dir.is_dir():
        return probe_dir / _BUNDLE_FILENAME
    xdg = env_map.get("XDG_CONFIG_HOME")
    if xdg:
        config_base = Path(xdg)
    else:
        home_dir = home if home is not None else Path.home()
        config_base = home_dir / ".config"
    return config_base / "kairix" / "secrets" / _BUNDLE_FILENAME


def set_secret(
    name: str,
    value: str,
    *,
    bundle_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    container_dir: Path | None = None,
) -> Path:
    """Persist one canonical secret into the resolved bundle file.

    Upserts the ``ENV_VAR=value`` line: an existing line for the same
    canonical name is replaced in place; comments and unrelated lines
    are preserved verbatim. The file is created with its parent dirs if
    absent and locked to mode 0600 either way. Multi-line values are
    encoded to one quoted, escaped line via
    :func:`kairix.secrets.encoding.encode_bundle_value`; the bundle
    parse sites decode symmetrically, so callers read back the exact
    bytes they stored.

    Args:
        name: Canonical secret name
            (``kairix-<scope>-<area>[-<instance>]-<leaf>``).
        value: The secret value. Never logged or echoed (F15).
        bundle_path: Explicit target file (test / wizard seam).
            ``None`` resolves via :func:`resolve_bundle_path`.
        env / home / container_dir: forwarded to
            :func:`resolve_bundle_path` (test seams).

    Returns:
        The path the value was written to.

    Raises:
        ValueError: non-canonical ``name`` or empty ``value``. Messages
            carry F21 ``fix:``/``next:``/``run:`` affordances and never
            include the value.
    """
    env_var = _validated_env_var(name)
    _validate_value(name, value)
    stored_value = encode_bundle_value(value)
    if bundle_path is not None:
        path = bundle_path
    else:
        path = resolve_bundle_path(env=env, home=home, container_dir=container_dir)
    path = _confine_to_allowed_root(path, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = split_bundle_lines(path.read_text(encoding="utf-8")) if path.exists() else []
    merged = _upsert_line(existing, env_var, stored_value)
    if not path.exists():
        path.touch(mode=0o600)
    path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    # Pre-existing bundles may carry looser modes — tighten on every write.
    os.chmod(path, 0o600)
    # Drop the read-side parse cache so the just-written value is what
    # the next in-process read-back (wizard connection test, verify
    # walk) sees, not a stale cached parse.
    from kairix.secrets._legacy import load_secrets_file

    load_secrets_file.cache_clear()
    return path


def _confine_to_allowed_root(path: Path, *, home: Path | None = None) -> Path:
    """Canonicalise the bundle path and verify it sits under an allowed root.

    The write target must live under the operator's home, ``/etc/kairix``,
    ``/run/secrets``, ``/run/kairix``, or the system temp dir (test
    fixtures). Escape raises before any filesystem mutation — the S2083
    confinement contract, mirroring
    ``kairix.connect.store.file_store._confine_to_allowed_root``.
    """
    resolved = path.expanduser().resolve()
    home_root = (home if home is not None else Path.home()).resolve()
    allowed = (
        home_root,
        Path("/etc/kairix"),
        Path("/run/secrets"),
        Path("/run/kairix"),
        Path(tempfile.gettempdir()).resolve(),
    )
    for root in allowed:
        if resolved == root or root in resolved.parents:
            return resolved
    roots_text = ", ".join(str(root) for root in allowed)
    raise ValueError(
        f"Refusing to write secrets to {resolved} — outside the allowed roots ({roots_text}). "
        "fix: point KAIRIX_SECRETS_FILE (or bundle_path) at a file under your home directory, "
        "/etc/kairix, or /run/secrets. "
        f"next: re-run kairix secrets set. {_RUN_VERIFY}"
    )


def _validated_env_var(name: str) -> str:
    """Parse + validate a canonical name; return its canonical env-var form."""
    try:
        scope, area, instance, leaf = parse_canonical_name(name)
    except ValueError as exc:
        raise ValueError(
            f"Secret name {name!r} is not canonical: {exc} "
            f"Canonical form: kairix-<scope>-<area>[-<instance>]-<leaf>. "
            f"fix: use a canonical name such as {_EXAMPLE_NAMES[0]} or {_EXAMPLE_NAMES[1]}. "
            f"next: kairix secrets verify lists every registered credential identity. "
            f"{_RUN_VERIFY}",
        ) from exc
    return canonical_env_var(scope, area, instance, leaf)


def _validate_value(name: str, value: str) -> None:
    """Reject empty values with F21 affordances (value never echoed).

    Multi-line values are accepted — :func:`set_secret` encodes them to
    one bundle line via ``encode_bundle_value`` before writing.
    """
    if not value:
        raise ValueError(
            f"No value provided for {name}. "
            f"fix: pipe the value via stdin — printf '%s' '<the-value>' | kairix secrets set {name} — "
            f"or pass --value for non-sensitive values. "
            f"next: stdin keeps the value out of your shell history. "
            f"{_RUN_VERIFY}",
        )


def _upsert_line(lines: list[str], env_var: str, value: str) -> list[str]:
    """Replace the env_var's line in place or append; preserve everything else."""
    output: list[str] = []
    replaced = False
    for line in lines:
        key = line.partition("=")[0].strip() if "=" in line else None
        if key == env_var:
            output.append(f"{env_var}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{env_var}={value}")
    return output


__all__ = ["resolve_bundle_path", "set_secret"]
