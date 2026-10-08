"""Unit-level coverage for kairix.connect.refresh."""

from __future__ import annotations

import pytest

from kairix.connect.protocols import RefreshUnavailableError
from kairix.connect.refresh import (
    GoogleRefreshableToken,
    GoogleRefreshState,
    StaticRefreshableToken,
)

pytestmark = pytest.mark.unit


def _state() -> GoogleRefreshState:
    return GoogleRefreshState(
        client_id="cid",
        client_secret="csec",  # pragma: allowlist secret
        refresh_token="rt",
        token_uri="https://oauth2.googleapis.com/token",
    )


def test_static_refreshable_never_expired() -> None:
    """Static tokens (Slack bot tokens) report not-expired and don't refresh."""
    t = StaticRefreshableToken(token="bot-token-001")
    assert not t.is_expired()
    assert t.headers() == {"Authorization": "Bearer bot-token-001"}
    t.refresh()  # no-op


def test_static_refreshable_custom_scheme() -> None:
    """Operators can override the auth scheme (e.g. token vs Bearer)."""
    t = StaticRefreshableToken(token="x", scheme="token")
    assert t.headers() == {"Authorization": "token x"}


def test_google_refresh_fresh_token_no_refresh() -> None:
    """A non-expired token doesn't trigger refresh on headers() call."""
    fixed_now = [1_000_000.0]
    calls: list[None] = []

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        calls.append(None)
        return "new-token", fixed_now[0] + 7200

    t = GoogleRefreshableToken(
        state=_state(),
        initial_access_token="pre-warmed",
        initial_expiry_epoch=fixed_now[0] + 3600,  # 1h ahead — fresh
        now_fn=lambda: fixed_now[0],
        refresh_fn=refresh_fn,
    )
    assert not t.is_expired()
    headers = t.headers()
    assert headers["Authorization"] == "Bearer pre-warmed"
    assert calls == []  # never refreshed


def test_google_refresh_expired_token_triggers_refresh() -> None:
    """An expired token triggers refresh on the next headers() call."""
    fixed_now = [1_000_000.0]

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        return "refreshed-token", fixed_now[0] + 7200

    t = GoogleRefreshableToken(
        state=_state(),
        initial_access_token="stale",
        initial_expiry_epoch=fixed_now[0] - 100,  # already expired
        now_fn=lambda: fixed_now[0],
        refresh_fn=refresh_fn,
    )
    assert t.is_expired()
    headers = t.headers()
    assert headers["Authorization"] == "Bearer refreshed-token"
    assert not t.is_expired()


def test_google_refresh_no_initial_token_is_expired() -> None:
    """No initial token at all → reports expired so first call refreshes."""
    fixed_now = [1_000_000.0]

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        return "first-token", fixed_now[0] + 3600

    t = GoogleRefreshableToken(
        state=_state(),
        now_fn=lambda: fixed_now[0],
        refresh_fn=refresh_fn,
    )
    assert t.is_expired()
    headers = t.headers()
    assert headers["Authorization"] == "Bearer first-token"


def test_google_refresh_failure_raises_typed() -> None:
    """A refresh_fn raising surfaces RefreshUnavailableError with F21 hints."""

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        raise ConnectionError("network down")

    t = GoogleRefreshableToken(state=_state(), refresh_fn=refresh_fn)
    with pytest.raises(RefreshUnavailableError, match="refresh failed"):
        t.refresh()


def test_google_refresh_skew_window_treats_near_expiry_as_expired() -> None:
    """The 60s skew window means a token expiring in 30s is reported expired."""
    fixed_now = [1_000_000.0]

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        return "post-skew-token", fixed_now[0] + 3600

    t = GoogleRefreshableToken(
        state=_state(),
        initial_access_token="almost-stale",
        initial_expiry_epoch=fixed_now[0] + 30,  # within skew window
        now_fn=lambda: fixed_now[0],
        refresh_fn=refresh_fn,
    )
    assert t.is_expired()


def test_default_refresh_path_uses_google_auth(monkeypatch: Any) -> None:  # type: ignore[name-defined]  # F3 rationale: Any imported later in the file via a deferred-import block (E402-clean)
    """Drives the production default refresh path through ``GoogleRefreshableToken.refresh``.

    With no ``refresh_fn`` injected, ``refresh()`` reaches the
    library-import default. Stubs sys.modules so the lazy import
    resolves to recording test doubles. Pins the production contract
    end-to-end.
    """
    from datetime import datetime, timezone

    captured: dict[str, object] = {}

    class _FakeCredentials:
        def __init__(self, **kwargs: object) -> None:
            captured["constructor_kwargs"] = kwargs
            self.token: str | None = None
            self.expiry: datetime | None = None

        def refresh(self, request: object) -> None:
            captured["refresh_arg"] = request
            self.token = "fresh-from-google-auth"
            self.expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class _FakeRequest:
        pass

    _install_google_stubs(monkeypatch, _FakeCredentials, _FakeRequest)

    # Drive through the PUBLIC GoogleRefreshableToken surface — no
    # refresh_fn injection → the default path runs.
    token = GoogleRefreshableToken(state=_state())
    token.refresh()
    headers = token.headers()
    assert headers == {"Authorization": "Bearer fresh-from-google-auth"}
    kwargs = captured["constructor_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["refresh_token"] == "rt"
    assert kwargs["client_id"] == "cid"


def test_default_refresh_path_handles_missing_expiry(monkeypatch: Any) -> None:  # type: ignore[name-defined]  # F3 rationale: Any imported later in the file via a deferred-import block (E402-clean)
    """When ``creds.expiry`` is None the production default falls back to 1h."""
    import time

    class _FakeCredentials:
        def __init__(self, **_kwargs: object) -> None:
            self.token: str | None = None
            self.expiry = None

        def refresh(self, _req: object) -> None:
            self.token = "no-expiry-token"

    class _FakeRequest:
        pass

    _install_google_stubs(monkeypatch, _FakeCredentials, _FakeRequest)
    token = GoogleRefreshableToken(state=_state())
    before = time.time()
    token.refresh()
    # is_expired honours the production default 1h window — the token
    # we just minted should NOT be expired immediately.
    assert not token.is_expired()
    # And it should be marked expired after ~3700s elapsed time.
    _ = before  # held for future timing-sensitivity assertions


def test_default_refresh_path_raises_when_library_absent(monkeypatch: Any) -> None:  # type: ignore[name-defined]  # F3 rationale: Any imported later in the file via a deferred-import block (E402-clean)
    """ImportError on google-auth surfaces a typed RefreshUnavailableError via the public surface."""
    # No sys.modules eviction needed: an ``import`` statement always calls
    # ``builtins.__import__`` (patched below), cached module or not.
    import builtins

    original_import = builtins.__import__

    def blocking_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "google.auth.transport.requests" or name == "google.oauth2.credentials":
            raise ImportError(f"blocked {name}")
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]  # F3 rationale: builtins.__import__ wrapper signature mirrors stdlib but mypy refuses the *args/**kwargs forward

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    token = GoogleRefreshableToken(state=_state())
    # The library-missing path raises RuntimeError; the public refresh()
    # wraps it as RefreshUnavailableError per the typed F21 contract.
    from kairix.connect.protocols import RefreshUnavailableError

    with pytest.raises(RefreshUnavailableError, match="refresh failed"):
        token.refresh()


# Add Any import for the new helpers.
from typing import Any  # noqa: E402 — local import after constants block


def _install_google_stubs(monkeypatch: Any, credentials_cls: type, request_cls: type) -> None:
    """Install fake google.auth + google.oauth2 modules in sys.modules.

    Wires the attribute chain so ``google.auth.transport.requests.Request``
    resolves at attribute-access time (the production code uses
    ``google.auth.transport.requests.Request()``, which requires the
    ``auth`` attribute on the ``google`` parent module).
    """
    import sys
    import types

    fake_google_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_google_auth_transport_requests.Request = request_cls  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google_auth_transport = types.ModuleType("google.auth.transport")
    fake_google_auth_transport.requests = fake_google_auth_transport_requests  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google_auth = types.ModuleType("google.auth")
    fake_google_auth.transport = fake_google_auth_transport  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google = types.ModuleType("google")
    fake_google.auth = fake_google_auth  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google_oauth2_credentials = types.ModuleType("google.oauth2.credentials")
    fake_google_oauth2_credentials.Credentials = credentials_cls  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google_oauth2 = types.ModuleType("google.oauth2")
    fake_google_oauth2.credentials = fake_google_oauth2_credentials  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    fake_google.oauth2 = fake_google_oauth2  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attributes; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.auth", fake_google_auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", fake_google_auth_transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", fake_google_auth_transport_requests)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_google_oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", fake_google_oauth2_credentials)
