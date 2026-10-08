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

from dataclasses import FrozenInstanceError, dataclass
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


def test_injected_resolvers_never_share_cached_secrets() -> None:
    """Two injected resolvers asking for the SAME secret name each get their
    own value — tenant B is never served tenant A's cached key.

    Sabotage proof (executed): key ``_CACHE`` by ``secret_name`` alone →
    the second auth returns tenant A's bearer and this fails; restored.
    """
    reset_api_key_cache()
    tenant_a = ApiKeyAuth(secret_lookup=lambda _name: "tenant-a-key")
    tenant_b = ApiKeyAuth(secret_lookup=lambda _name: "tenant-b-key")

    assert tenant_a.headers("shared-secret-name").mapping == {"Authorization": "Bearer tenant-a-key"}
    assert tenant_b.headers("shared-secret-name").mapping == {"Authorization": "Bearer tenant-b-key"}


def test_instances_sharing_a_resolver_share_the_cached_secret() -> None:
    """Instances built on the same resolver reuse one resolution per name —
    the canonical default resolver's sharing behaviour is preserved."""
    reset_api_key_cache()
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"value-{len(calls)}"

    first = ApiKeyAuth(secret_lookup=lookup).headers("shared")
    second = ApiKeyAuth(secret_lookup=lookup).headers("shared")
    assert first.mapping == second.mapping == {"Authorization": "Bearer value-1"}
    assert calls == ["shared"]


class _EqualResolver:
    """A resolver whose ``__eq__`` / ``__hash__`` make every instance equal."""

    def __init__(self, token: str) -> None:
        self.token = token

    def __call__(self, _name: str) -> str:
        return self.token

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EqualResolver)

    def __hash__(self) -> int:
        return 0


@dataclass
class _UnhashableResolver:
    """A mutable dataclass with ``__call__`` — ``__hash__`` is ``None``."""

    token: str

    def __call__(self, _name: str) -> str:
        return self.token


def test_equal_but_distinct_resolvers_get_their_own_tokens() -> None:
    """Two resolver OBJECTS that compare equal still never share a cached token
    — the cache is keyed by identity, not ``__eq__`` / ``__hash__``.

    Sabotage proof (executed): key the cache by ``(resolver, secret_name)``
    (equality-based dict key) → tenant B is served tenant A's token; restored.
    """
    reset_api_key_cache()
    tenant_a = ApiKeyAuth(secret_lookup=_EqualResolver("tenant-a-key"))
    tenant_b = ApiKeyAuth(secret_lookup=_EqualResolver("tenant-b-key"))

    assert tenant_a.headers("same-name").mapping == {"Authorization": "Bearer tenant-a-key"}
    assert tenant_b.headers("same-name").mapping == {"Authorization": "Bearer tenant-b-key"}


def test_unhashable_resolver_is_supported_and_cached() -> None:
    """An unhashable callable resolver works (no ``TypeError``) and its
    resolution is still cached for the same resolver object.

    Sabotage proof (executed): key the cache by ``(resolver, secret_name)``
    → ``TypeError: unhashable type`` on the first call; restored.
    """
    reset_api_key_cache()
    resolver = _UnhashableResolver("first")
    auth = ApiKeyAuth(secret_lookup=resolver)

    assert auth.headers("name").mapping == {"Authorization": "Bearer first"}
    resolver.token = "rotated"
    assert ApiKeyAuth(secret_lookup=resolver).headers("name").mapping == {"Authorization": "Bearer first"}
