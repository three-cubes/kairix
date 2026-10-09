"""Unit tests for :mod:`kairix.transport.auth.api_key`.

Covers the cache hit/miss paths, the missing-secret error shape, the
frozen-dataclass discipline of :class:`BearerHeaders`, and the
``reset_api_key_cache`` test affordance.

The resolver-chain integration uses real per-file secrets under a
tmp_path XDG_CONFIG_HOME so the test exercises the production
:func:`kairix.secrets.get_secret` walk end-to-end. No
monkeypatch.setattr on kairix internals (F1-clean); no setenv on
``KAIRIX_*`` variables (F2-clean) — only ``XDG_CONFIG_HOME`` (a
stdlib boundary input) is redirected.

Sabotage proofs:
  * Inverting the ``if cached is None`` branch makes the cache-hit
    test see a second resolver call — fails. Restored.
  * Removing the ``fix:`` marker from the error message breaks the
    F21 marker assertion. Restored.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kairix.transport.auth.api_key import (
    ApiKeyAuth,
    BearerHeaders,
    MissingCredentialsError,
    reset_api_key_cache,
)

pytestmark = pytest.mark.unit


def _xdg_secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the secrets resolver chain at a tmp_path-rooted XDG path.

    Uses only ``XDG_CONFIG_HOME`` (stdlib env var, not ``KAIRIX_*``,
    so F2-clean). The default ``/run/secrets/kairix.env`` bundle is
    absent on dev / CI machines and the Azure KV path only fires when
    ``KAIRIX_KV_NAME`` is set (also absent on dev / CI), so the resolver
    chain reduces to the XDG per-file directory for the duration of
    the test.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    secrets_dir = tmp_path / "kairix" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    return secrets_dir


def test_bearer_headers_is_frozen() -> None:
    """:class:`BearerHeaders` is a frozen dataclass — boundary discipline (F42)."""
    headers = BearerHeaders(mapping={"Authorization": "Bearer abc"})
    with pytest.raises(FrozenInstanceError):
        headers.mapping = {}  # type: ignore[misc] — testing immutability


def test_api_key_auth_resolves_via_get_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful resolution returns a :class:`BearerHeaders` with the bearer."""
    reset_api_key_cache()
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    (secrets_dir / "test-secret").write_text("deadbeef", encoding="utf-8")

    auth = ApiKeyAuth()
    headers = auth.headers("test-secret")
    assert headers.mapping == {"Authorization": "Bearer deadbeef"}


def test_api_key_auth_caches_resolved_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call reuses the cached resolution — survives a secret-file
    rotation without seeing the new value.
    """
    reset_api_key_cache()
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    secret_file = secrets_dir / "cache-test-secret"
    secret_file.write_text("first-value", encoding="utf-8")

    auth = ApiKeyAuth()
    first = auth.headers("cache-test-secret")
    # Rotate the on-disk secret. A non-caching implementation would
    # surface the new value on the next call; the cache keeps the
    # original.
    secret_file.write_text("rotated-value", encoding="utf-8")
    second = auth.headers("cache-test-secret")
    assert first.mapping == second.mapping == {"Authorization": "Bearer first-value"}


def test_api_key_auth_missing_secret_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolved secret raises ``MissingCredentialsError`` with ``fix:``."""
    reset_api_key_cache()
    _xdg_secrets_dir(tmp_path, monkeypatch)

    auth = ApiKeyAuth()
    with pytest.raises(MissingCredentialsError) as excinfo:
        auth.headers("missing-secret-name")
    message = str(excinfo.value)
    assert "fix:" in message
    assert "missing-secret-name" in message


def test_api_key_auth_blank_secret_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only secret value is treated as missing."""
    reset_api_key_cache()
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    (secrets_dir / "blank-secret").write_text("   ", encoding="utf-8")

    auth = ApiKeyAuth()
    with pytest.raises(MissingCredentialsError):
        auth.headers("blank-secret")


def test_reset_cache_drops_resolved_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset_api_key_cache`` forces the next call to re-resolve."""
    reset_api_key_cache()
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    secret_file = secrets_dir / "rotating-secret"
    secret_file.write_text("first", encoding="utf-8")

    auth = ApiKeyAuth()
    first = auth.headers("rotating-secret")
    secret_file.write_text("second", encoding="utf-8")
    reset_api_key_cache()
    second = auth.headers("rotating-secret")
    assert first.mapping["Authorization"] == "Bearer first"
    assert second.mapping["Authorization"] == "Bearer second"


def test_injected_resolver_is_called_every_time_and_never_cached() -> None:
    """An injected ``secret_lookup`` is not cached by the helper: every
    ``headers`` call asks it again, so a rotation is seen immediately and
    nothing it resolved outlives it in module state.

    Sabotage proof (executed): route injected resolvers through the
    process-wide ``_CACHE`` → the second call returns the stale value and
    the resolver is asked once; restored.
    """
    reset_api_key_cache()
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"value-{len(calls)}"

    auth = ApiKeyAuth(secret_lookup=lookup)
    assert auth.headers("rotating").mapping == {"Authorization": "Bearer value-1"}
    assert auth.headers("rotating").mapping == {"Authorization": "Bearer value-2"}
    assert calls == ["rotating", "rotating"]


def test_resolvers_never_share_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two injected resolvers asking for the SAME secret name each get their
    own value, and neither leaks into the default resolver's cache.

    Sabotage proof (executed): cache every resolver's value under the secret
    name alone → tenant B is served tenant A's key; restored.
    """
    reset_api_key_cache()
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    (secrets_dir / "shared-name").write_text("from-default-chain", encoding="utf-8")

    tenant_a = ApiKeyAuth(secret_lookup=lambda _name: "tenant-a-key")
    tenant_b = ApiKeyAuth(secret_lookup=lambda _name: "tenant-b-key")
    assert tenant_a.headers("shared-name").mapping == {"Authorization": "Bearer tenant-a-key"}
    assert tenant_b.headers("shared-name").mapping == {"Authorization": "Bearer tenant-b-key"}
    assert ApiKeyAuth().headers("shared-name").mapping == {"Authorization": "Bearer from-default-chain"}


def test_injected_resolver_missing_secret_raises_typed_error() -> None:
    """A resolver that finds nothing raises :class:`MissingCredentialsError`."""
    with pytest.raises(MissingCredentialsError, match="fix:"):
        ApiKeyAuth(secret_lookup=lambda _name: None).headers("absent")
