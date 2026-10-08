"""Unit-level coverage for kairix.connect.oauth2.github_app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.connect.oauth2.github_app import (
    GITHUB_API_BASE,
    GITHUB_APP_DEFAULT_SCOPES,
    GITHUB_APP_INSTALL_URL_TEMPLATE,
    GITHUB_APP_SERVICE_AREA,
    GITHUB_JWT_AUDIENCE_TOKEN_URI,
    JWT_ALGORITHM,
    JWT_LIFETIME_S,
    GitHubAppFlow,
)
from kairix.connect.protocols import (
    CallbackResult,
    CapturedTokens,
    ClientCredentials,
)
from tests.fakes import FakeBrowserLauncher, FakeCallbackListener

pytestmark = pytest.mark.unit


# Minimal-but-realistic PEM body — looks like a PEM private key for the
# basic-shape validation (BEGIN + PRIVATE KEY markers). Tests that need
# real RS256 signing inject a token_exchanger instead.
_FAKE_PEM_BODY = (  # pragma: allowlist secret
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
    "FAKE-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def _write_pem(path: Path) -> None:
    path.write_text(_FAKE_PEM_BODY)


def _flow(path: Path, **kwargs: Any) -> GitHubAppFlow:
    return GitHubAppFlow(app_id="123", private_key_path=path, **kwargs)


def test_module_constants_are_distinct_and_well_formed() -> None:
    """The module-level URL + service-area constants are stable + non-empty."""
    assert GITHUB_API_BASE.startswith("https://")
    assert GITHUB_APP_INSTALL_URL_TEMPLATE.startswith("https://github.com/apps/")
    assert GITHUB_JWT_AUDIENCE_TOKEN_URI.startswith("https://")
    assert GITHUB_APP_SERVICE_AREA == "github"
    assert JWT_ALGORITHM == "RS256"
    assert JWT_LIFETIME_S > 0
    assert GITHUB_APP_DEFAULT_SCOPES == ()


def test_discover_reads_pem_and_returns_typed_credentials(tmp_path: Path) -> None:
    """discover_client_credentials returns ClientCredentials with app_id + PEM."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    flow = _flow(pem_path)
    creds = flow.discover_client_credentials()
    assert creds.client_id == "123"
    assert "BEGIN" in creds.client_secret
    assert "PRIVATE KEY" in creds.client_secret


def test_missing_private_key_raises_with_f21_hint(tmp_path: Path) -> None:
    """A missing PEM file raises FileNotFoundError with the F21 markers."""
    pem_path = tmp_path / "missing.pem"
    flow = _flow(pem_path)
    with pytest.raises(FileNotFoundError) as exc_info:
        flow.discover_client_credentials()
    msg = str(exc_info.value)
    assert "fix:" in msg and "next:" in msg and "run:" in msg, (
        f"expected F21 fix/next/run markers in error, got: {msg!r}"
    )
    assert "github.com/settings/apps" in msg
    assert ".pem" in msg or "Private keys" in msg


def test_malformed_pem_raises_typed_error(tmp_path: Path) -> None:
    """A file without BEGIN/PRIVATE KEY markers raises ValueError with F21 hints."""
    pem_path = tmp_path / "junk.pem"
    pem_path.write_text("not a PEM file at all\n")
    flow = _flow(pem_path)
    with pytest.raises(ValueError) as exc_info:
        flow.discover_client_credentials()
    msg = str(exc_info.value)
    assert "fix:" in msg and "next:" in msg and "run:" in msg, f"expected F21 markers, got: {msg!r}"
    assert "PEM" in msg or "private key" in msg.lower()


def test_empty_app_id_rejected_at_construction(tmp_path: Path) -> None:
    """Constructing GitHubAppFlow with empty app_id raises ValueError + F21."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    with pytest.raises(ValueError) as exc_info:
        GitHubAppFlow(app_id="", private_key_path=pem_path)
    msg = str(exc_info.value)
    assert "--app-id" in msg
    assert "fix:" in msg and "next:" in msg and "run:" in msg


def test_authorize_full_happy_path_with_injected_exchanger(tmp_path: Path) -> None:
    """authorize() composes browser + listener + JWT exchanger and returns tokens."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(
            code="ignored",
            state=None,
            params={"installation_id": "98765", "setup_action": "install"},
        ),
    )
    captured_exchanger_args: list[tuple[str, str, str]] = []

    def fake_exchanger(app_id: str, pem: str, installation_id: str) -> str:
        captured_exchanger_args.append((app_id, pem, installation_id))
        return "fake-installation-token-abc"

    flow = GitHubAppFlow(
        app_id="123",
        private_key_path=pem_path,
        browser=browser,
        token_exchanger=fake_exchanger,
    )
    tokens = flow.authorize(listener=listener)
    # Captured tokens carry the installation token + metadata
    assert tokens.access_token == "fake-installation-token-abc"
    assert tokens.refresh_token == ""  # GitHub App has no refresh_token
    assert tokens.token_uri == GITHUB_JWT_AUDIENCE_TOKEN_URI
    assert tokens.metadata == {"installation-id": "98765"}
    # Browser was opened to the install URL
    assert len(browser.opened) == 1
    assert "github.com/apps/" in browser.opened[0]
    assert "kairix-bot" in browser.opened[0]
    # Exchanger received the correct triple
    assert captured_exchanger_args == [("123", flow.discover_client_credentials().client_secret, "98765")]


def test_authorize_uses_configured_app_slug(tmp_path: Path) -> None:
    """The --app-slug kwarg drives the install URL."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(code="x", state=None, params={"installation_id": "1"}),
    )
    flow = GitHubAppFlow(
        app_id="42",
        private_key_path=pem_path,
        app_slug="my-custom-app",
        browser=browser,
        token_exchanger=lambda *_: "tok",
    )
    flow.authorize(listener=listener)
    assert browser.opened == ["https://github.com/apps/my-custom-app/installations/new"]


def test_authorize_url_builder_injection(tmp_path: Path) -> None:
    """Custom install_url_builder overrides the default URL construction."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(code="x", state=None, params={"installation_id": "1"}),
    )

    def builder(slug: str) -> str:
        return f"custom://install?slug={slug}"

    flow = GitHubAppFlow(
        app_id="42",
        private_key_path=pem_path,
        app_slug="kairix-bot",
        browser=browser,
        install_url_builder=builder,
        token_exchanger=lambda *_: "tok",
    )
    flow.authorize(listener=listener)
    assert browser.opened == ["custom://install?slug=kairix-bot"]


def test_authorize_raises_when_callback_missing_installation_id(tmp_path: Path) -> None:
    """If the listener returns a callback with neither code nor installation_id, raise."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    # CallbackResult with empty code + empty params — the broken-callback shape.
    listener = FakeCallbackListener(
        callback=CallbackResult(code="", state=None, params={}),
    )
    flow = GitHubAppFlow(
        app_id="123",
        private_key_path=pem_path,
        browser=browser,
        token_exchanger=lambda *_: "tok",
    )
    with pytest.raises(ValueError) as exc_info:
        flow.authorize(listener=listener)
    msg = str(exc_info.value)
    assert "installation_id" in msg
    assert "fix:" in msg and "next:" in msg and "run:" in msg


def test_authorize_falls_back_to_code_when_params_missing(tmp_path: Path) -> None:
    """If params don't carry installation_id, fall back to ``code`` (listener compatibility)."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    # Code-only callback shape — params is empty so the flow reads code.
    listener = FakeCallbackListener(
        callback=CallbackResult(code="55555", state=None, params={}),
    )
    captured: list[tuple[str, str, str]] = []

    def fake_exchanger(app_id: str, pem: str, installation_id: str) -> str:
        captured.append((app_id, pem, installation_id))
        return "tok"

    flow = GitHubAppFlow(
        app_id="123",
        private_key_path=pem_path,
        browser=browser,
        token_exchanger=fake_exchanger,
    )
    tokens = flow.authorize(listener=listener)
    assert tokens.metadata == {"installation-id": "55555"}
    assert captured[0][2] == "55555"


def test_flow_satisfies_oauth2_flow_protocol(tmp_path: Path) -> None:
    """GitHubAppFlow satisfies the OAuth2Flow Protocol structurally."""
    from kairix.connect.protocols import OAuth2Flow

    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    flow = _flow(pem_path)
    assert isinstance(flow, OAuth2Flow)


def test_default_token_exchanger_raises_when_pyjwt_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default exchanger raises RuntimeError with F21 hints when pyjwt is missing.

    This drives the production lazy-import path through the public
    GitHubAppFlow.authorize() surface — confirms the ImportError
    translation honours the F21 contract.
    """
    import builtins

    # No sys.modules eviction needed: an ``import`` statement always calls
    # ``builtins.__import__`` (patched below), cached module or not.
    original_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jwt":
            raise ImportError("blocked jwt")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
    )
    # NO token_exchanger injected — flow falls back to the default path.
    flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
    with pytest.raises(RuntimeError) as exc_info:
        flow.authorize(listener=listener)
    msg = str(exc_info.value)
    assert "pyjwt" in msg.lower()
    assert "fix:" in msg and "next:" in msg and "run:" in msg


def test_default_token_exchanger_handles_jwt_sign_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWT signing failure surfaces as RuntimeError with F21 hints."""
    import sys
    import types

    fake_jwt = types.ModuleType("jwt")

    def encode(_payload: dict[str, Any], _key: str, algorithm: str = "") -> str:
        raise ValueError("malformed key for algorithm " + algorithm)

    fake_jwt.encode = encode  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
    )
    flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
    with pytest.raises(RuntimeError) as exc_info:
        flow.authorize(listener=listener)
    msg = str(exc_info.value)
    assert "signing failed" in msg.lower() or "JWT" in msg
    assert "fix:" in msg and "next:" in msg and "run:" in msg


def test_default_token_exchanger_handles_github_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response from GitHub surfaces as RuntimeError with F21 hints."""
    import sys
    import types

    # Stub jwt to succeed
    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = lambda *_a, **_k: "fake.jwt.token"  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    # Stub httpx with a MockTransport returning 401
    import httpx

    def transport_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    mock_transport = httpx.MockTransport(transport_handler)
    # Monkeypatch httpx.post to use the mock transport
    original_post = httpx.post

    def patched_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=mock_transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    try:
        pem_path = tmp_path / "app.pem"
        _write_pem(pem_path)
        browser = FakeBrowserLauncher()
        listener = FakeCallbackListener(
            callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
        )
        flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
        with pytest.raises(RuntimeError) as exc_info:
            flow.authorize(listener=listener)
        msg = str(exc_info.value)
        assert "GitHub rejected" in msg
        assert "401" in msg
        assert "Bad credentials" in msg
        assert "fix:" in msg and "next:" in msg and "run:" in msg
    finally:
        monkeypatch.setattr(httpx, "post", original_post)


def test_default_token_exchanger_handles_missing_token_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx response with no ``token`` field raises RuntimeError with F21 hints."""
    import sys
    import types

    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = lambda *_a, **_k: "fake.jwt.token"  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    import httpx

    def transport_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected_field": "no token here"})

    mock_transport = httpx.MockTransport(transport_handler)
    original_post = httpx.post

    def patched_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=mock_transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    try:
        pem_path = tmp_path / "app.pem"
        _write_pem(pem_path)
        browser = FakeBrowserLauncher()
        listener = FakeCallbackListener(
            callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
        )
        flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
        with pytest.raises(RuntimeError) as exc_info:
            flow.authorize(listener=listener)
        msg = str(exc_info.value)
        assert "missing 'token'" in msg
        assert "fix:" in msg and "next:" in msg and "run:" in msg
    finally:
        monkeypatch.setattr(httpx, "post", original_post)


def test_default_token_exchanger_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default JWT-sign + token-exchange path returns the GitHub token."""
    import sys
    import types

    captured_jwt: list[dict[str, Any]] = []

    def encode(payload: dict[str, Any], _key: str, algorithm: str = "") -> str:
        captured_jwt.append({"payload": payload, "alg": algorithm})
        return "signed.jwt.value"

    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = encode  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    import httpx

    captured_requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            201,
            json={"token": "ghs_real_installation_token_xyz", "expires_at": "2026-06-01T13:00:00Z"},
        )

    mock_transport = httpx.MockTransport(transport_handler)
    original_post = httpx.post

    def patched_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=mock_transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    try:
        pem_path = tmp_path / "app.pem"
        _write_pem(pem_path)
        browser = FakeBrowserLauncher()
        listener = FakeCallbackListener(
            callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
        )
        flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
        tokens = flow.authorize(listener=listener)
        assert tokens.access_token == "ghs_real_installation_token_xyz"
        assert tokens.metadata == {"installation-id": "999"}
        # JWT payload was signed with the right iss + alg
        assert captured_jwt[0]["payload"]["iss"] == "123"
        assert captured_jwt[0]["alg"] == "RS256"
        # POST went to the right installation URL
        assert "/app/installations/999/access_tokens" in str(captured_requests[0].url)
    finally:
        monkeypatch.setattr(httpx, "post", original_post)


def test_credentials_returned_are_typed_pair(tmp_path: Path) -> None:
    """discover_client_credentials returns a ClientCredentials, not a dict."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    flow = _flow(pem_path)
    creds = flow.discover_client_credentials()
    assert isinstance(creds, ClientCredentials)


def test_default_token_exchanger_handles_unparseable_error_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx GitHub response whose body isn't valid JSON still raises RuntimeError.

    The ``_default_token_exchanger`` falls back to ``error_code = "unknown"``
    when ``response.json()`` raises (the GitHub error path swallows
    ``ValueError`` + ``TypeError`` so an HTML error page or empty body
    doesn't crash the exchanger with an unrelated traceback).
    """
    import sys
    import types

    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = lambda *_a, **_k: "fake.jwt.token"  # type: ignore[attr-defined]  # F3 rationale: ModuleType has no statically-typed attrs; runtime attribute injection is the documented test pattern
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    import httpx

    # Body is HTML (e.g. an upstream proxy returned a 502 with a static
    # error page) — ``response.json()`` raises ValueError.
    def transport_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>Bad Gateway</html>")

    mock_transport = httpx.MockTransport(transport_handler)
    original_post = httpx.post

    def patched_post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=mock_transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", patched_post)
    try:
        pem_path = tmp_path / "app.pem"
        _write_pem(pem_path)
        browser = FakeBrowserLauncher()
        listener = FakeCallbackListener(
            callback=CallbackResult(code="x", state=None, params={"installation_id": "999"}),
        )
        flow = GitHubAppFlow(app_id="123", private_key_path=pem_path, browser=browser)
        with pytest.raises(RuntimeError) as exc_info:
            flow.authorize(listener=listener)
        msg = str(exc_info.value)
        assert "GitHub rejected" in msg
        assert "502" in msg
        # ``unknown`` is the fallback the exchanger inserts when JSON parsing fails
        assert "unknown" in msg
        assert "fix:" in msg and "next:" in msg and "run:" in msg
    finally:
        monkeypatch.setattr(httpx, "post", original_post)


def test_read_private_key_surfaces_os_read_failure(tmp_path: Path) -> None:
    """An OSError on ``path.read_text`` is wrapped as FileNotFoundError with F21 markers.

    Driving the public ``discover_client_credentials`` surface against a
    PEM file we deliberately make unreadable. The wrap path turns the
    OSError into a FileNotFoundError whose message names the path and
    carries ``fix:`` / ``next:`` / ``run:`` markers — exercising the
    OSError branch in ``_read_private_key``.
    """
    pem_path = tmp_path / "app.pem"
    pem_path.write_text(_FAKE_PEM_BODY)
    # Strip read permission. Skip on platforms (Windows in CI) where
    # chmod doesn't deny reads — the assertion is the contract, not
    # the chmod itself.
    import os
    import stat

    pem_path.chmod(0o000)
    try:
        # On systems where root would still read despite chmod, skip.
        if os.access(pem_path, os.R_OK):
            pytest.skip("filesystem does not honour chmod 0 — cannot drive OSError branch here")
        flow = _flow(pem_path)
        with pytest.raises(FileNotFoundError) as exc_info:
            flow.discover_client_credentials()
        msg = str(exc_info.value)
        assert "cannot read" in msg.lower()
        assert str(pem_path) in msg
        assert "fix:" in msg and "next:" in msg and "run:" in msg
    finally:
        # Restore read+write so tmp_path cleanup works
        pem_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_captured_tokens_metadata_is_serializable(tmp_path: Path) -> None:
    """CapturedTokens.metadata is a plain dict suitable for the store layer."""
    pem_path = tmp_path / "app.pem"
    _write_pem(pem_path)
    browser = FakeBrowserLauncher()
    listener = FakeCallbackListener(
        callback=CallbackResult(code="x", state=None, params={"installation_id": "777"}),
    )
    flow = GitHubAppFlow(
        app_id="123",
        private_key_path=pem_path,
        browser=browser,
        token_exchanger=lambda *_: "tok",
    )
    tokens = flow.authorize(listener=listener)
    assert isinstance(tokens, CapturedTokens)
    # Metadata is a plain dict that can be iterated by the store layer
    assert dict(tokens.metadata) == {"installation-id": "777"}
