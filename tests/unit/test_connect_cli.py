"""Unit-level coverage for kairix.connect.cli.

Exercises the dispatcher via :func:`main` with ``ConnectDeps`` injected
— no subprocess, no real HTTP listener, no real OAuth provider. The
integration + E2E tests cover the subprocess path.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from kairix.connect.cli import (
    ConnectDeps,
    main,
)
from kairix.connect.oauth2.google import GOOGLE_TOKEN_URI, GoogleOAuth2Flow
from kairix.connect.protocols import (
    CapturedTokens,
    ClientCredentials,
    TokenStoreUnauthorizedError,
)
from kairix.connect.store.azure_kv_store import AzureKeyVaultTokenStore
from kairix.connect.store.file_store import FileTokenStore
from kairix.connect.store.stdout_store import StdoutTokenStore
from tests.fakes import (
    FakeBrowserLauncher,
    FakeCallbackListener,
    FakeTokenStore,
)

pytestmark = pytest.mark.unit


def _make_client_secret(path: Path) -> Path:
    payload = {
        "installed": {
            "client_id": "test-cid",
            "client_secret": "test-csec",  # pragma: allowlist secret
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": GOOGLE_TOKEN_URI,
        },
    }
    path.write_text(json.dumps(payload))
    return path


class _FakeFlow:
    """Pinned flow that skips the listener/browser plumbing entirely."""

    def __init__(self, *, tokens: CapturedTokens, client: ClientCredentials) -> None:
        self.service_area = "gmail"
        self.scopes = ()
        self._tokens = tokens
        self._client = client

    def discover_client_credentials(self) -> ClientCredentials:
        return self._client

    def authorize(self, *, listener: Any, timeout_s: float = 120.0) -> CapturedTokens:
        return self._tokens


def _ok_deps(
    *,
    listener: Any | None = None,
    flow: Any | None = None,
    store: Any | None = None,
    stdout: io.StringIO | None = None,
    stderr: io.StringIO | None = None,
) -> ConnectDeps:
    """Construct a ConnectDeps wired to the supplied fakes."""
    real_stdout = stdout if stdout is not None else io.StringIO()
    real_stderr = stderr if stderr is not None else io.StringIO()
    real_listener = listener if listener is not None else FakeCallbackListener()
    real_store = store if store is not None else FakeTokenStore()
    if flow is None:
        flow = _FakeFlow(
            tokens=CapturedTokens(
                refresh_token="rt",
                access_token="at",
                token_uri=GOOGLE_TOKEN_URI,
            ),
            client=ClientCredentials(client_id="cid", client_secret="csec"),
        )
    return ConnectDeps(
        listener_factory=lambda _h, _p: real_listener,
        oauth2_flow_factory=lambda _args: flow,
        token_store_factory=lambda _spec: real_store,
        stdout=real_stdout,
        stderr=real_stderr,
    )


def test_cli_happy_path_returns_zero(tmp_path: Path) -> None:
    """End-to-end success — exit code 0, summary printed to stdout."""
    cs = _make_client_secret(tmp_path / "cs.json")
    stdout = io.StringIO()
    store = FakeTokenStore()
    deps = _ok_deps(store=store, stdout=stdout)
    rc = main(
        ["google-gmail", "--client-secret-path", str(cs)],
        deps=deps,
    )
    assert rc == 0
    assert "ok" in stdout.getvalue()
    assert "KAIRIX_CONNECTOR_GMAIL_REFRESH_TOKEN" in stdout.getvalue()
    assert len(store.writes) == 1
    assert store.writes[0]["area"] == "gmail"


def test_cli_consent_denied_returns_nonzero(tmp_path: Path) -> None:
    """A denied consent surfaces as exit 1 with the F21 hint on stderr."""
    cs = _make_client_secret(tmp_path / "cs.json")
    # Use the fake's default denied_message ("consent denied") so the
    # strengthened assertion below can confirm the rationale lands in
    # stderr verbatim. Customising the message in this CLI-level test
    # would make the assertion test the customisation, not the prod path.
    listener = FakeCallbackListener(denied=True)
    stderr = io.StringIO()

    # Override the flow with one that DOES call the listener so the
    # CallbackDeniedError propagates.
    class _RealCallFlow:
        service_area = "gmail"
        scopes = ()

        def discover_client_credentials(self) -> ClientCredentials:
            return ClientCredentials(client_id="x", client_secret="y")

        def authorize(self, *, listener: Any, timeout_s: float = 120.0) -> CapturedTokens:
            listener.wait_for_callback(timeout_s=timeout_s)  # raises
            raise AssertionError("should not reach here")

    deps_real = ConnectDeps(
        listener_factory=lambda _h, _p: listener,
        oauth2_flow_factory=lambda _args: _RealCallFlow(),
        token_store_factory=lambda _spec: FakeTokenStore(),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    rc = main(["google-gmail", "--client-secret-path", str(cs)], deps=deps_real)
    assert rc == 1
    # Strengthened: must mention BOTH "consent" AND "denied" so a
    # regression that drops the rationale half is caught.
    err = stderr.getvalue().lower()
    assert "consent" in err and "denied" in err, f"expected both 'consent' and 'denied' in stderr, got: {err!r}"
    assert "fix:" in err and "next:" in err and "run:" in err, (
        f"expected F21 fix/next/run markers in stderr, got: {err!r}"
    )


def test_cli_timeout_returns_nonzero(tmp_path: Path) -> None:
    """Listener timeout surfaces as exit 1."""
    cs = _make_client_secret(tmp_path / "cs.json")
    listener = FakeCallbackListener(timeout=True)
    stderr = io.StringIO()

    class _RealCallFlow:
        service_area = "gmail"
        scopes = ()

        def discover_client_credentials(self) -> ClientCredentials:
            return ClientCredentials(client_id="x", client_secret="y")

        def authorize(self, *, listener: Any, timeout_s: float = 120.0) -> CapturedTokens:
            listener.wait_for_callback(timeout_s=timeout_s)
            raise AssertionError("should not reach")

    deps = ConnectDeps(
        listener_factory=lambda _h, _p: listener,
        oauth2_flow_factory=lambda _args: _RealCallFlow(),
        token_store_factory=lambda _spec: FakeTokenStore(),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    rc = main(["google-gmail", "--client-secret-path", str(cs)], deps=deps)
    assert rc == 1
    # Strengthened: must mention "callback" (the operator-visible noun)
    # AND a timing word (timeout / within ...s).
    err = stderr.getvalue().lower()
    assert "callback" in err, f"expected 'callback' in stderr, got: {err!r}"
    assert "timeout" in err or "within" in err, f"expected time-out indication in stderr, got: {err!r}"
    assert "fix:" in err and "next:" in err and "run:" in err, (
        f"expected F21 fix/next/run markers in stderr, got: {err!r}"
    )


def test_cli_timeout_flag_threads_into_wait_for_callback(tmp_path: Path) -> None:
    """``--timeout N`` is honoured: it reaches ``listener.wait_for_callback``.

    Regression for #498 — the flag was parsed but silently ignored. Drives
    the production :class:`GoogleOAuth2Flow.authorize` path (browser +
    authorize-url-builder + token-exchanger injected via the constructor
    seams) so the assertion proves the CLI threads ``args.timeout`` →
    ``flow.authorize(timeout_s=...)`` → ``listener.wait_for_callback(timeout_s=...)``.

    The :class:`FakeCallbackListener` records every ``timeout_s`` it was
    called with in ``wait_calls``; an operator-supplied ``--timeout 7``
    must land there as ``7.0``.

    Sabotage-proof: revert ``kairix/connect/cli.py`` to
    ``flow.authorize(listener=listener)`` (dropping the ``timeout_s=``
    argument) — the default 120.0 flows through instead and
    ``wait_calls == [120.0] != [7.0]`` fails the assertion.
    """
    cs = _make_client_secret(tmp_path / "cs.json")
    listener = FakeCallbackListener()
    flow = GoogleOAuth2Flow(
        service_area="gmail",
        client_secret_path=cs,
        browser=FakeBrowserLauncher(),
        authorize_url_builder=lambda _client, _redirect, _scopes: "https://accounts.example/auth",
        token_exchanger=lambda _client, _code, _redirect: CapturedTokens(
            refresh_token="rt",
            access_token="at",
            token_uri=GOOGLE_TOKEN_URI,
        ),
    )
    deps = ConnectDeps(
        listener_factory=lambda _h, _p: listener,
        oauth2_flow_factory=lambda _args: flow,
        token_store_factory=lambda _spec: FakeTokenStore(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    rc = main(
        ["google-gmail", "--client-secret-path", str(cs), "--timeout", "7"],
        deps=deps,
    )
    assert rc == 0
    assert listener.wait_calls == [7.0], (
        f"expected the operator --timeout to thread into wait_for_callback as [7.0], got {listener.wait_calls!r}"
    )


def test_cli_missing_client_secret_returns_nonzero(tmp_path: Path) -> None:
    """A nonexistent client_secret_path surfaces as exit 1."""
    stderr = io.StringIO()
    missing = tmp_path / "nope.json"

    # Need a flow that actually opens the file (the default factory does
    # via GoogleOAuth2Flow). Use it here.
    deps = ConnectDeps(
        listener_factory=lambda _h, _p: FakeCallbackListener(),
        token_store_factory=lambda _spec: FakeTokenStore(),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    rc = main(["google-gmail", "--client-secret-path", str(missing)], deps=deps)
    assert rc == 1
    assert "not found" in stderr.getvalue()


def _capture_store_from_main(tmp_path: Path, store_arg: str) -> tuple[int, object | None]:
    """Run main() with a recording wrapper that captures the resolved TokenStore.

    Drives the production ``token_store_factory`` slot via the
    ConnectDeps default — but wraps it so the test can read back the
    actual concrete store class chosen. F1-clean: no monkeypatching of
    kairix modules; the deps dataclass IS the public injection seam.
    """
    cs = _make_client_secret(tmp_path / "cs.json")
    captured: dict[str, object] = {}
    # Use the production default token_store_factory by reading it off
    # a fresh ConnectDeps() — that's the documented public seam.
    default_deps = ConnectDeps()
    real_factory = default_deps.token_store_factory

    def recording_factory(spec: str) -> object:
        try:
            store = real_factory(spec)
        except ValueError as exc:
            captured["error"] = exc
            raise
        captured["store"] = store
        # Record the routed store but write to a fake: the real FileTokenStore
        # targets the session-shared default bundle, and tokens persisted
        # there were later hydrated into os.environ by an unrelated test's
        # factory bootstrap (a KAIRIX_* env leak across tests).
        return FakeTokenStore()

    listener = FakeCallbackListener()
    deps = ConnectDeps(
        listener_factory=lambda _h, _p: listener,
        oauth2_flow_factory=lambda _args: _FakeFlow(
            tokens=CapturedTokens(
                refresh_token="rt",
                access_token="at",
                token_uri=GOOGLE_TOKEN_URI,
            ),
            client=ClientCredentials(client_id="cid", client_secret="csec"),
        ),
        token_store_factory=recording_factory,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    # For the file store we need a writable target — override via
    # explicit --store=file is the production shape.
    rc = main(
        ["google-gmail", "--client-secret-path", str(cs), "--store", store_arg],
        deps=deps,
    )
    return rc, captured.get("store")


def test_cli_store_factory_routes_file(tmp_path: Path) -> None:
    """``--store=file`` builds a FileTokenStore (production default factory)."""
    rc, store = _capture_store_from_main(tmp_path, "file")
    assert rc == 0
    assert isinstance(store, FileTokenStore)


def test_cli_store_factory_routes_stdout(tmp_path: Path) -> None:
    rc, store = _capture_store_from_main(tmp_path, "stdout")
    assert rc == 0
    assert isinstance(store, StdoutTokenStore)


def test_cli_store_factory_routes_azure_kv_short(tmp_path: Path) -> None:
    # The Azure store fails when it tries to call Azure SDK without real
    # credentials; we only verify the factory routes to the right class.
    rc, store = _capture_store_from_main(tmp_path, "azure-kv:my-vault")
    assert isinstance(store, AzureKeyVaultTokenStore)
    # rc may be non-zero because the SDK lazy-import path raises; the
    # store-class assertion above is what we're pinning.
    _ = rc


def test_cli_store_factory_routes_azure_kv_url(tmp_path: Path) -> None:
    rc, store = _capture_store_from_main(
        tmp_path,
        "azure-kv:https://my-vault.vault.azure.net/",
    )
    assert isinstance(store, AzureKeyVaultTokenStore)
    _ = rc


# Bare ``--store=azure-kv`` (no suffix) reads ``$KAIRIX_KV_NAME`` —
# covered by tests/unit/test_connect_store_azure_kv.py::test_env_var_used_when_no_explicit
# via the constructor's ``env=`` injection seam (F2-clean). The
# CLI-side routing for that variant is exercised in
# test_cli_store_factory_routes_azure_kv_short above.


def test_cli_store_factory_unknown_returns_error_code(tmp_path: Path) -> None:
    """Unknown ``--store=`` value exits non-zero with F21 error."""
    stderr = io.StringIO()
    cs = _make_client_secret(tmp_path / "cs.json")
    deps = _ok_deps(stderr=stderr)
    # Override token_store_factory with a non-failing default and add
    # the bad spec via argv to trip the default factory's error path.
    deps_real = ConnectDeps(
        listener_factory=deps.listener_factory,
        oauth2_flow_factory=deps.oauth2_flow_factory,
        # use production default so the bad spec hits the real ValueError
        stdout=deps.stdout,
        stderr=stderr,
    )
    rc = main(
        ["google-gmail", "--client-secret-path", str(cs), "--store", "dropbox-secrets"],
        deps=deps_real,
    )
    assert rc == 1
    assert "unknown --store" in stderr.getvalue()


def test_cli_build_azure_kv_malformed_form_returns_error_code(tmp_path: Path) -> None:
    """``azure-kvgarbage`` (no colon) returns non-zero with F21 error via the CLI."""
    stderr = io.StringIO()
    cs = _make_client_secret(tmp_path / "cs.json")
    deps_real = ConnectDeps(
        listener_factory=lambda _h, _p: FakeCallbackListener(),
        oauth2_flow_factory=lambda _args: _FakeFlow(
            tokens=CapturedTokens(refresh_token="rt", access_token="at", token_uri=GOOGLE_TOKEN_URI),
            client=ClientCredentials(client_id="cid", client_secret="csec"),
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    rc = main(
        ["google-gmail", "--client-secret-path", str(cs), "--store", "azure-kvbroken"],
        deps=deps_real,
    )
    assert rc == 1
    assert "malformed --store" in stderr.getvalue()


def test_cli_token_store_failure_returns_nonzero(tmp_path: Path) -> None:
    cs = _make_client_secret(tmp_path / "cs.json")
    failing_store = FakeTokenStore(raises=TokenStoreUnauthorizedError("kv denied"))
    stderr = io.StringIO()
    deps = _ok_deps(store=failing_store, stderr=stderr)
    rc = main(["google-gmail", "--client-secret-path", str(cs)], deps=deps)
    assert rc == 1
    assert "kv denied" in stderr.getvalue()


def test_cli_listener_factory_oserror_returns_nonzero() -> None:
    stderr = io.StringIO()

    def boom(_host: str, _port: int) -> Any:
        raise OSError(
            "kairix connect: no free port. "
            "fix: stop port-blocker. next: lsof. run: kairix connect --port 9090 --client-secret-path <p>",
        )

    deps = ConnectDeps(
        listener_factory=boom,
        token_store_factory=lambda _spec: FakeTokenStore(),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    rc = main(["google-gmail", "--client-secret-path", "/tmp/x"], deps=deps)
    assert rc == 1
    assert "no free port" in stderr.getvalue()


def test_cli_subcommand_options_routing(tmp_path: Path) -> None:
    """Each Google subcommand routes to the matching ``service_area``."""
    cs = _make_client_secret(tmp_path / "cs.json")
    for cmd, area in (
        ("google-gmail", "gmail"),
        ("google-drive", "google-drive"),
        ("google-calendar", "google-calendar"),
    ):
        store = FakeTokenStore()
        deps = _ok_deps(store=store)
        rc = main([cmd, "--client-secret-path", str(cs)], deps=deps)
        assert rc == 0
        assert store.writes[0]["area"] == area


def test_cli_main_default_deps_does_not_raise_on_help(capsys: Any) -> None:
    """Calling main(['--help']) prints help and exits via argparse SystemExit."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    # argparse exits 0 for --help.
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# python:S5886 — flow-builder return type widens to OAuth2Flow
# ---------------------------------------------------------------------------


def test_subcommand_registry_google_flow_widens_to_oauth2_flow_protocol(tmp_path: Path) -> None:
    """The Google subcommand's registry-bound flow-builder returns an OAuth2Flow.

    Pins the python:S5886 fix that widened the concrete
    :class:`GoogleOAuth2Flow` to the :class:`OAuth2Flow` Protocol via a
    typed local binding inside the Google flow-builder. Drives the
    public :data:`SUBCOMMAND_REGISTRY` surface rather than the
    underscore-prefixed builder directly — F5-clean.

    Sabotage-proof: drop the ``flow: OAuth2Flow = ...`` widening and
    return the concrete directly — mypy + Sonar fire S5886 again. The
    runtime ``isinstance(..., OAuth2Flow)`` keeps the Protocol shape
    pinned: any future refactor that returns something missing
    ``discover_client_credentials`` / ``authorize`` breaks this test.
    """
    import argparse as _argparse

    from kairix.connect.cli import SUBCOMMAND_REGISTRY
    from kairix.connect.oauth2.google import GoogleOAuth2Flow
    from kairix.connect.protocols import OAuth2Flow

    cs = _make_client_secret(tmp_path / "cs.json")
    spec = SUBCOMMAND_REGISTRY["google-gmail"]
    args = _argparse.Namespace(client_secret_path=cs)
    flow = spec.flow_builder(args)
    assert isinstance(flow, OAuth2Flow), f"builder must return OAuth2Flow Protocol shape; got {type(flow).__name__}"
    assert isinstance(flow, GoogleOAuth2Flow), (
        f"Google subcommand must yield the GoogleOAuth2Flow concrete; got {type(flow).__name__}"
    )


def test_subcommand_registry_slack_flow_widens_to_oauth2_flow_protocol() -> None:
    """The Slack subcommand's registry-bound flow-builder returns an OAuth2Flow.

    Mirrors the Google test for the Slack call site so the python:S5886
    fix stays sabotage-pinned for both builders. Drives the public
    :data:`SUBCOMMAND_REGISTRY` — F5-clean.
    """
    import argparse as _argparse

    from kairix.connect.cli import SUBCOMMAND_REGISTRY
    from kairix.connect.oauth2.slack import SlackOAuth2Flow
    from kairix.connect.protocols import OAuth2Flow

    spec = SUBCOMMAND_REGISTRY["slack"]
    args = _argparse.Namespace(
        workspace="alpha",
        client_id="cid-001",
        client_secret="csec-001",  # pragma: allowlist secret
    )
    flow = spec.flow_builder(args)
    assert isinstance(flow, OAuth2Flow), f"builder must return OAuth2Flow Protocol shape; got {type(flow).__name__}"
    assert isinstance(flow, SlackOAuth2Flow), (
        f"Slack subcommand must yield the SlackOAuth2Flow concrete; got {type(flow).__name__}"
    )


def test_subcommand_registry_github_app_flow_widens_to_oauth2_flow_protocol(tmp_path: Path) -> None:
    """The GitHub-App subcommand's registry-bound flow-builder returns an OAuth2Flow.

    The brief only required #5/#6 (Google + Slack). Adding GitHub App
    keeps the three sibling builders consistently sabotage-pinned so a
    future refactor that drops the widening on any one fires this test
    first.
    """
    import argparse as _argparse

    from kairix.connect.cli import SUBCOMMAND_REGISTRY
    from kairix.connect.oauth2.github_app import GitHubAppFlow
    from kairix.connect.protocols import OAuth2Flow

    pem = tmp_path / "key.pem"
    fake_pem = "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n"  # pragma: allowlist secret
    pem.write_text(fake_pem)
    spec = SUBCOMMAND_REGISTRY["github-app"]
    args = _argparse.Namespace(
        app_id="42",
        private_key_path=pem,
        app_slug="agent-alpha-bot",
    )
    flow = spec.flow_builder(args)
    assert isinstance(flow, OAuth2Flow), f"builder must return OAuth2Flow Protocol shape; got {type(flow).__name__}"
    assert isinstance(flow, GitHubAppFlow), (
        f"GitHub-App subcommand must yield the GitHubAppFlow concrete; got {type(flow).__name__}"
    )
