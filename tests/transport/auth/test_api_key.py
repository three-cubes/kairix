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

import gc
import weakref
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


class _TrackedSecret(str):
    """A resolved secret value a test can hold weakly (plain ``str`` cannot)."""


def _register_tenant_auth(token: str) -> tuple[weakref.ref[object], weakref.ref[_TrackedSecret]]:
    """Resolve once through a per-tenant closure resolver, then drop it.

    Returns only weak references to the resolver and to the resolved secret
    value, so the caller can observe whether anything (the auth cache) still
    keeps either alive.
    """
    secret = _TrackedSecret(token)

    def tenant_lookup(_name: str) -> str | None:
        return secret

    auth = ApiKeyAuth(secret_lookup=tenant_lookup)
    assert auth.headers("tenant-secret").mapping == {"Authorization": f"Bearer {token}"}
    return weakref.ref(tenant_lookup), weakref.ref(secret)


def test_dropped_injected_resolver_is_not_retained_by_the_cache() -> None:
    """Per-tenant resolvers — and the secrets cached for them — are released
    once their auth objects are dropped: the resolved-secret cache never pins
    either for the life of the process.

    Sabotage proofs (executed, each restored):
      * store a strong reference to the resolver in the cache entry instead of
        a ``weakref.ref`` → the resolver refs stay alive;
      * make the weakref callback skip eviction (``entry[0] is not dead``) →
        the resolver dies but its bucket keeps the secret value alive.
    """
    reset_api_key_cache()
    refs = [_register_tenant_auth(f"tenant-{n}") for n in range(3)]
    gc.collect()

    assert [resolver_ref() for resolver_ref, _ in refs] == [None, None, None]
    assert [secret_ref() for _, secret_ref in refs] == [None, None, None]


def test_default_resolver_cache_stays_process_wide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical default resolver keeps one process-wide resolution:
    separate ``ApiKeyAuth()`` instances share it until the cache is reset."""
    secrets_dir = _xdg_secrets_dir(tmp_path, monkeypatch)
    reset_api_key_cache()
    (secrets_dir / "default-shared").write_text("first")
    assert ApiKeyAuth().headers("default-shared").mapping == {"Authorization": "Bearer first"}

    (secrets_dir / "default-shared").write_text("rotated")
    gc.collect()
    assert ApiKeyAuth().headers("default-shared").mapping == {"Authorization": "Bearer first"}

    reset_api_key_cache()
    assert ApiKeyAuth().headers("default-shared").mapping == {"Authorization": "Bearer rotated"}
