"""Unit-level coverage for kairix.connect.refresh.GitHubAppRefreshableToken."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.connect.protocols import RefreshUnavailableError
from kairix.connect.refresh import GitHubAppRefreshableToken

pytestmark = pytest.mark.unit


_FAKE_PEM = (  # pragma: allowlist secret
    "-----BEGIN RSA PRIVATE KEY-----\nFAKE-PEM-BODY-FOR-TESTING\n-----END RSA PRIVATE KEY-----\n"
)


def _token(
    **kwargs: Any,
) -> GitHubAppRefreshableToken:
    """Build a GitHubAppRefreshableToken with safe defaults for tests."""
    defaults: dict[str, Any] = {
        "app_id": "12345",
        "private_key_pem": _FAKE_PEM,
        "installation_id": "98765",
    }
    defaults.update(kwargs)
    return GitHubAppRefreshableToken(**defaults)


def test_cold_start_is_expired_and_first_headers_refreshes() -> None:
    """No cached token → is_expired() == True; first headers() triggers refresh."""
    fixed_now = [1_000_000.0]
    refresh_calls: list[tuple[str, str, str]] = []

    def fake_exchanger(app_id: str, pem: str, installation_id: str) -> tuple[str, float]:
        refresh_calls.append((app_id, pem, installation_id))
        return "fresh-installation-token", fixed_now[0] + 3600

    token = _token(now_fn=lambda: fixed_now[0], token_exchanger=fake_exchanger)
    assert token.is_expired()  # cold start
    headers = token.headers()
    assert headers == {"Authorization": "Bearer fresh-installation-token"}
    assert refresh_calls == [("12345", _FAKE_PEM, "98765")]


def test_fresh_cached_token_skips_refresh() -> None:
    """A cached token within the 50-min rotation window does not trigger refresh."""
    fixed_now = [1_000_000.0]
    calls: list[None] = []

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        calls.append(None)
        return "freshly-rotated-token", fixed_now[0] + 3600

    token = _token(now_fn=lambda: fixed_now[0], token_exchanger=fake_exchanger)
    # First call rotates; should call exchanger exactly once.
    token.headers()
    assert len(calls) == 1
    # Second call within the rotation budget should NOT trigger refresh.
    fixed_now[0] += 600  # 10 min elapsed — still within 50-min budget
    headers = token.headers()
    assert headers == {"Authorization": "Bearer freshly-rotated-token"}
    assert len(calls) == 1  # no second refresh


def test_token_rotates_at_50min_mark() -> None:
    """After 50 min elapsed since cache, the next headers() call rotates."""
    fixed_now = [1_000_000.0]
    yielded: list[str] = ["first-token", "second-token"]

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        token = yielded.pop(0) if yielded else "exhausted"
        return token, fixed_now[0] + 3600

    token = _token(now_fn=lambda: fixed_now[0], token_exchanger=fake_exchanger)
    assert token.headers()["Authorization"].endswith("first-token")
    # Advance ~51 min — past the 50-min rotation budget.
    fixed_now[0] += 51 * 60
    assert token.is_expired()
    assert token.headers()["Authorization"].endswith("second-token")


def test_refresh_failure_raises_typed_with_f21_hints() -> None:
    """A token_exchanger raising surfaces RefreshUnavailableError with F21 markers."""

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        raise ConnectionError("github API unreachable")

    token = _token(token_exchanger=fake_exchanger)
    with pytest.raises(RefreshUnavailableError) as exc_info:
        token.refresh()
    msg = str(exc_info.value)
    assert "fix:" in msg and "next:" in msg and "run:" in msg, f"expected F21 markers, got: {msg!r}"
    assert "GitHub App" in msg or "installation-token" in msg


def test_refresh_resets_cached_token_on_failure_path() -> None:
    """Even if refresh fails, the cached-token slot is not corrupted halfway."""

    call_count = [0]

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        call_count[0] += 1
        if call_count[0] == 1:
            return "first-good-token", 9999999999.0
        raise ConnectionError("transient failure")

    token = _token(token_exchanger=fake_exchanger)
    headers = token.headers()
    assert headers == {"Authorization": "Bearer first-good-token"}
    # Second forced refresh raises but doesn't corrupt cache
    with pytest.raises(RefreshUnavailableError):
        token.refresh()
    # The previous good token is still surfaced if cache wasn't reset
    # (this asserts the production guarantee — no half-state).
    assert token.headers() == {"Authorization": "Bearer first-good-token"}


def test_default_refresh_path_raises_when_pyjwt_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default refresh path (no injection) wraps ImportError as RefreshUnavailableError."""
    import builtins

    # No sys.modules eviction needed: an ``import`` statement always calls
    # ``builtins.__import__`` (patched below), cached module or not.
    original_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jwt":
            raise ImportError("blocked jwt")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    # NO token_exchanger — falls through to _default_github_app_refresh.
    token = _token()
    with pytest.raises(RefreshUnavailableError) as exc_info:
        token.refresh()
    # The library-missing path goes through the public RefreshUnavailableError
    # wrapper — confirms the typed-error contract.
    msg = str(exc_info.value)
    assert "fix:" in msg and "next:" in msg and "run:" in msg


def test_default_refresh_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default path (with stubbed pyjwt + httpx) returns a token via the public surface."""
    import sys
    import types

    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = lambda *_a, **_k: "signed.jwt.value"  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    import httpx

    def transport_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"token": "ghs_real_token_xyz", "expires_at": "2026-06-01T13:00:00Z"},
        )

    mock_transport = httpx.MockTransport(transport_handler)
    original_post = httpx.post

    def patched_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=mock_transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    try:
        # NO token_exchanger injected — drives the default path.
        token = _token()
        token.refresh()
        headers = token.headers()
        assert headers == {"Authorization": "Bearer ghs_real_token_xyz"}
    finally:
        monkeypatch.setattr(httpx, "post", original_post)


def test_satisfies_refreshable_token_protocol() -> None:
    """GitHubAppRefreshableToken satisfies the RefreshableToken Protocol."""
    from kairix.connect.protocols import RefreshableToken

    token = _token()
    assert isinstance(token, RefreshableToken)


def test_is_expired_when_no_token_cached() -> None:
    """is_expired() is True when the cache has never been populated."""
    token = _token()
    assert token.is_expired()


def test_headers_format_pins_bearer_prefix() -> None:
    """The auth scheme is 'Bearer' to match GitHub's API expectations."""

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        return "the-actual-token", 9999999999.0

    token = _token(token_exchanger=fake_exchanger)
    headers = token.headers()
    assert headers["Authorization"].startswith("Bearer ")
    assert "the-actual-token" in headers["Authorization"]


def test_three_consecutive_calls_share_one_refresh() -> None:
    """Calling headers() three times in quick succession triggers exactly one refresh."""
    fixed_now = [1_000_000.0]
    calls: list[None] = []

    def fake_exchanger(_a: str, _p: str, _i: str) -> tuple[str, float]:
        calls.append(None)
        return "tok", fixed_now[0] + 3600

    token = _token(now_fn=lambda: fixed_now[0], token_exchanger=fake_exchanger)
    token.headers()
    token.headers()
    token.headers()
    assert len(calls) == 1


def test_install_id_propagates_to_exchanger() -> None:
    """The installation_id constructor arg threads through to the exchanger call."""
    captured: list[str] = []

    def fake_exchanger(_a: str, _p: str, installation_id: str) -> tuple[str, float]:
        captured.append(installation_id)
        return "x", 9999999999.0

    token = _token(installation_id="999000", token_exchanger=fake_exchanger)
    token.refresh()
    assert captured == ["999000"]
