"""Static-API-key Bearer authentication helper.

Reusable helper for connectors and providers whose authentication shape
is a single, long-lived API key surfaced in the ``Authorization: Bearer``
header. The Dex CRM connector (Wave 5 KP-1) is the first caller; future
connectors / providers that authenticate with a static token will share
this same helper rather than re-rolling the lookup + caching dance.

The helper resolves the secret value via the canonical
:func:`kairix.secrets.get_secret` chain (env vars → per-file secrets →
sidecar bundle → Azure Key Vault) and caches the resolved value per
process. Construction is cheap (no I/O); resolution happens on first
:meth:`ApiKeyAuth.headers` call. When the secret is unset the helper
raises :class:`MissingCredentialsError` with an actionable ``fix:``
message — never a bare KeyError, never a stack trace into ``secrets.py``.

Frozen-dataclass return per F42: :class:`BearerHeaders` is the boundary
value object so callers cannot mutate the produced header mapping in
place.

F15 — the helper never logs the resolved secret. Diagnostics name the
secret slot (``connector-dex-api-key``) but never echo the token bytes.

Per F26/F35: this module sits in ``kairix/transport/auth/`` so any
connector / provider can import it without taking a dependency on another
plugin's tree. No plugin-private state lives here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


class MissingCredentialsError(RuntimeError):
    """Raised when an :class:`ApiKeyAuth` secret cannot be resolved.

    Distinct exception type so connectors / providers can catch the
    "operator has not configured the secret yet" case and surface a
    typed error from their public ``list_changes`` / ``embed`` /
    ``chat`` entry points. The string carries a ``fix:`` marker per
    F21 so the agent reading the failure has the correction action,
    not just a diagnosis.
    """


@dataclass(frozen=True)
class BearerHeaders:
    """Immutable container for the resolved ``Authorization`` header.

    F42 frozen-dataclass at the boundary: callers receive an immutable
    object they cannot mutate in place, so a returned headers mapping
    cannot accidentally be aliased and corrupted by another caller.

    The ``mapping`` attribute is the dict-shaped form an
    :class:`httpx.Client` (or similar) expects to splat into a request
    builder; callers should pass it as-is rather than rebuilding their
    own dict each call.
    """

    mapping: Mapping[str, str]


# Process-wide cache so repeated ``headers(...)`` calls reuse the
# resolved secret without re-walking the resolver chain on every HTTP
# request. Keyed on the logical secret name so an operator can declare
# multiple API-key-backed connectors without their lookups colliding.
# Only the canonical default resolver is cached here: an injected
# ``secret_lookup`` is called on every request (its owner caches if it
# wants to), so two resolvers can never share — or leak — an entry.
_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()


def reset_api_key_cache() -> None:
    """Drop the module-global resolved-secret cache.

    Test affordance — used in test setup/teardown to ensure a
    previously-unset → newly-set lookup transition actually re-walks
    the resolver chain instead of returning the stale cached value.
    Mirrors the same shape as
    :func:`kairix.transport.coalesce.reset_embed_coalescer` and
    :func:`kairix.transport.pool.reset_client_cache` — both public,
    both consumed by ``tests/conftest.py`` teardown fixtures.

    Public name (no leading underscore) so tests under
    ``tests/transport/auth/`` and ``tests/connectors/dex_crm/`` can
    import it without tripping the no-internal-test-imports gate.
    """
    with _CACHE_LOCK:
        _CACHE.clear()


def _default_secret_lookup(secret_name: str) -> str | None:
    """Production default — walk the canonical :func:`kairix.secrets.get_secret` chain.

    ``required=False`` so a missing secret returns control to
    :meth:`ApiKeyAuth.headers` for the typed
    :class:`MissingCredentialsError` rather than an :class:`OSError` from
    deep inside ``secrets.py``. Lazy import so the helper itself doesn't
    pull in the secrets module at construction time — keeps module import
    cheap and avoids the circular-import shape between transport/auth and
    the secrets resolver.
    """
    from kairix.secrets import get_secret

    return get_secret(secret_name, required=False)


@dataclass(frozen=True)
class ApiKeyAuth:
    """Static-API-key Bearer auth helper.

    Single-method surface — :meth:`headers` resolves the secret and
    returns the ``{"Authorization": "Bearer <token>"}`` mapping wrapped
    in a frozen :class:`BearerHeaders`. The first call hits the
    :func:`kairix.secrets.get_secret` chain; subsequent calls return
    the cached resolution.

    Frozen dataclass — the helper itself carries no mutable state.
    The cache lives at module scope so multiple instances pointing at
    the same secret-name share one resolution.

    Construction is cheap and side-effect-free, so callers can build an
    :class:`ApiKeyAuth` at module import without paying the resolver
    cost or risking an ``OSError`` from a missing secret. Resolution
    happens on first :meth:`headers` call, and a missing secret raises
    :class:`MissingCredentialsError` with an actionable message — not
    the raw :func:`get_secret` stack trace.

    ``secret_lookup`` is the resolver seam: ``(secret_name) -> value | None``.
    Production leaves it at the default (the :func:`kairix.secrets.get_secret`
    chain), which is cached process-wide; a caller holding its own secret
    source — or a test proving the missing-secret path without touching the
    process env — injects one, which is called on every :meth:`headers` call
    and never cached here.
    """

    secret_lookup: Callable[[str], str | None] = field(default=_default_secret_lookup, repr=False)

    def headers(self, secret_name: str) -> BearerHeaders:
        """Return the Bearer header mapping for ``secret_name``.

        Looks up the secret via :func:`kairix.secrets.get_secret` with
        ``required=False`` (so missing-secret control returns to this
        helper for a typed error rather than an :class:`OSError` from
        deep inside ``secrets.py``) and wraps the resolved value in the
        ``Authorization: Bearer <value>`` shape.

        Raises:
            MissingCredentialsError: when the secret resolves to
                ``None``. The error message carries a ``fix:`` marker
                naming the secret slot — so a connector that exposes
                this exception up to the operator surface stays F21-
                actionable.
        """
        if self.secret_lookup is not _default_secret_lookup:
            token = _require(secret_name, self.secret_lookup(secret_name))
        else:
            with _CACHE_LOCK:
                cached = _CACHE.get(secret_name)
            if cached is None:
                cached = _require(secret_name, _default_secret_lookup(secret_name))
                with _CACHE_LOCK:
                    _CACHE[secret_name] = cached
            token = cached
        return BearerHeaders(mapping={"Authorization": f"Bearer {token}"})


def _require(secret_name: str, resolved: str | None) -> str:
    """Return ``resolved``, or raise the typed error when it is missing / blank."""
    if resolved is None or not resolved.strip():
        raise MissingCredentialsError(
            f"api_key_auth: secret {secret_name!r} is not configured. "
            f"fix: set the secret via the configured resolver chain "
            f"(env var, per-file secret, sidecar bundle, or Azure Key Vault). "
            f"next: see docs/operations/OPERATIONS.md for the secret-loading runbook."
        )
    return resolved
