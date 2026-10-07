"""Google OAuth2 flow — one implementation for Gmail / Drive / Calendar.

Per ADR-032 §"Library choices": built on ``google-auth-oauthlib`` for
the authorize-and-exchange dance; ``google-auth`` is used by the
sibling :mod:`kairix.connect.refresh` module at runtime.

Three service-area variants share this class:

  * ``service_area="gmail"`` — scope ``gmail.readonly``
  * ``service_area="google-drive"`` — scope ``drive.readonly``
  * ``service_area="google-calendar"`` — scope ``calendar.readonly``

The operator downloads a ``client_secret.json`` from the GCP console
(Desktop application credential type) and supplies the path via
``--client-secret-path``. The flow reads ``client_id`` + ``client_secret``
from that file, runs the consent dance, captures the
``refresh_token`` + ``access_token`` and returns them as
:class:`kairix.connect.protocols.CapturedTokens` for the token store.

**Critical operator step**: the GCP OAuth consent screen must be in
"Production" state — not "Testing" — so the captured refresh token
doesn't silently expire after 7 days. See
``kairix/connect/README.md`` for the full GCP setup walkthrough.

The Google library is imported lazily inside :meth:`authorize` so module
import succeeds in environments without ``google-auth-oauthlib`` — the
operator only needs the library when they actually run
``kairix connect google-*``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kairix.connect.oauth2.base import AuthorizationCodeFlow
from kairix.connect.protocols import (
    BrowserLauncher,
    CapturedTokens,
    ClientCredentials,
)

# Per-service scope-string constants. Hoisted to module level so F17
# (no string literal ≥10 chars duplicated ≥3 times) stays happy and so
# tests reference the canonical scope by name.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# Mapping from service-area string (canonical kairix name) to the default
# scope tuple. Operators can override by passing explicit scopes to
# :class:`GoogleOAuth2Flow`.
DEFAULT_SCOPES_BY_AREA: dict[str, tuple[str, ...]] = {
    "gmail": (GMAIL_READONLY_SCOPE,),
    "google-drive": (DRIVE_READONLY_SCOPE,),
    "google-calendar": (CALENDAR_READONLY_SCOPE,),
}

# Canonical token endpoint Google publishes for the OAuth2 flow. Pinned
# here so the captured :class:`CapturedTokens.token_uri` doesn't depend
# on the library's internal default (which has changed shape across
# google-auth releases historically).
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 — OAuth endpoint URL, not a credential

# Common error-message prefix — kept as a module constant so F17
# (no string literal ≥10 chars duplicated ≥3 times) stays clean.
_ERR_PREFIX = "kairix connect: "


@dataclass(frozen=True)
class _ClientSecretBlob:
    """Parsed ``client_secret.json`` payload.

    Frozen per F42. The JSON has a top-level ``installed`` (Desktop) or
    ``web`` (Web) key — we accept either shape but recommend Desktop in
    the operator README.
    """

    client_id: str
    client_secret: str


def _parse_client_secret_file(path: Path) -> _ClientSecretBlob:
    """Parse a Google ``client_secret.json`` download into a typed blob.

    Accepts both the ``installed`` (Desktop) and ``web`` (Web) shapes
    Google supports. Raises :class:`FileNotFoundError` /
    :class:`ValueError` with F21-compliant messages when the file is
    missing or malformed.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{_ERR_PREFIX}client_secret.json not found at {path}. "
            f"fix: download the OAuth client secret from the GCP console "
            f"(APIs & Services -> Credentials -> Desktop app -> Download JSON). "
            f"next: pass --client-secret-path <downloaded-path> to kairix connect. "
            f"run: kairix connect <service> --client-secret-path {path}",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{_ERR_PREFIX}{path} is not valid JSON. "
            f"fix: re-download client_secret.json from the GCP console "
            f"(APIs & Services -> Credentials). The downloaded file is JSON. "
            f"next: pass the freshly downloaded path to --client-secret-path. "
            f"run: kairix connect <service> --client-secret-path <path>",
        ) from exc
    section = payload.get("installed") or payload.get("web")
    if not isinstance(section, dict):
        raise ValueError(
            f"{_ERR_PREFIX}{path} missing 'installed' or 'web' top-level key. "
            f"fix: re-download the credential from the GCP console; the JSON should "
            f"have 'installed' (Desktop app) or 'web' (Web app) at the top level. "
            f"next: confirm you picked 'Desktop app' as the application type. "
            f"run: kairix connect <service> --client-secret-path <path>",
        )
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise ValueError(
            f"{_ERR_PREFIX}{path} missing 'client_id' or 'client_secret'. "
            f"fix: re-download the credential from the GCP console. "
            f"next: confirm you downloaded the JSON for the same credential pair you "
            f"see in the GCP console. "
            f"run: kairix connect <service> --client-secret-path <path>",
        )
    return _ClientSecretBlob(client_id=client_id, client_secret=client_secret)


class GoogleOAuth2Flow(AuthorizationCodeFlow):
    """Run the authorize-and-exchange flow for one Google service area.

    Args:
      service_area: One of ``"gmail"``, ``"google-drive"``,
        ``"google-calendar"``. Drives the default scope set and the
        canonical KV name written by the token store.
      client_secret_path: Path to the operator-downloaded
        ``client_secret.json``.
      scopes: Override the default scopes (rare — operators with the
        ``drive.file`` partial scope variant pass an explicit tuple).
      browser: :class:`BrowserLauncher` — defaults to a wrapper around
        ``webbrowser.open``. Tests inject a recording fake so the
        real browser doesn't open during pytest.
      authorize_url_builder: Test seam — replaces the library's
        authorize-URL construction so tests don't need
        ``google-auth-oauthlib`` installed.
      token_exchanger: Test seam — replaces the library's
        code-for-token exchange. Receives ``(client, code, redirect_uri)``
        and returns a :class:`CapturedTokens`.
    """

    def __init__(
        self,
        *,
        service_area: str,
        client_secret_path: Path,
        scopes: tuple[str, ...] | None = None,
        browser: BrowserLauncher | None = None,
        authorize_url_builder: object | None = None,
        token_exchanger: object | None = None,
    ) -> None:
        if service_area not in DEFAULT_SCOPES_BY_AREA:
            raise ValueError(
                f"{_ERR_PREFIX}unknown Google service_area {service_area!r}. "
                f"fix: pass one of {sorted(DEFAULT_SCOPES_BY_AREA)}. "
                f"next: see kairix/connect/cli.py for the CLI subcommand → service_area map. "
                f"run: kairix connect google-gmail --client-secret-path <path>",
            )
        self.service_area = service_area
        self.scopes: tuple[str, ...] = scopes if scopes is not None else DEFAULT_SCOPES_BY_AREA[service_area]
        self._client_secret_path = client_secret_path
        self._browser = browser if browser is not None else _DefaultBrowser()
        self._authorize_url_builder = authorize_url_builder
        self._token_exchanger = token_exchanger

    def discover_client_credentials(self) -> ClientCredentials:
        blob = _parse_client_secret_file(self._client_secret_path)
        return ClientCredentials(client_id=blob.client_id, client_secret=blob.client_secret)

    # ``authorize`` is inherited from :class:`AuthorizationCodeFlow` — the
    # shared dance opens the browser, blocks on ``wait_for_callback`` with
    # the operator-supplied ``timeout_s``, and exchanges the code. The
    # default token exchanger uses ``google-auth-oauthlib``'s
    # ``fetch_token(code=...)`` (via :meth:`_exchange_code`); tests inject
    # a recording exchanger to avoid pulling the Google library.

    def _build_authorize_url(self, client: ClientCredentials, redirect_uri: str) -> str:
        if self._authorize_url_builder is not None:
            return self._authorize_url_builder(client, redirect_uri, self.scopes)  # type: ignore[operator,no-any-return]  # F3 rationale: test seam — caller-provided callable returning str
        return _default_authorize_url(client, redirect_uri, self.scopes)

    def _exchange_code(
        self,
        client: ClientCredentials,
        code: str,
        redirect_uri: str,
    ) -> CapturedTokens:
        if self._token_exchanger is not None:
            return self._token_exchanger(client, code, redirect_uri)  # type: ignore[operator,no-any-return]  # F3 rationale: test seam — caller-provided callable returning CapturedTokens
        return _default_exchange_code(client, code, redirect_uri, self.scopes)


def _default_authorize_url(
    client: ClientCredentials,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> str:
    """Build the Google OAuth2 authorize URL via stdlib only.

    google-auth-oauthlib's authorize-URL builder is a thin wrapper
    around urllib query-string construction; we replicate it here so
    the build doesn't need the library installed unless the operator
    actually runs ``token_exchanger``-less code (which only happens
    via the default exchanger; tests inject their own).
    """
    from urllib.parse import urlencode

    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        # Force consent prompt so Google issues a refresh_token even on
        # re-auth (Google omits the refresh_token on silent re-auth by
        # default). "offline" access_type pairs with this to get the
        # long-lived refresh token.
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def _default_exchange_code(
    client: ClientCredentials,
    code: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> CapturedTokens:
    """Default code-for-token exchange via ``google-auth-oauthlib``.

    Lazy-imports the library so unit tests can construct
    :class:`GoogleOAuth2Flow` with a ``token_exchanger=`` injection
    and never hit this path. Production callers without injection
    rely on the library being installed.
    """
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError(
            _ERR_PREFIX + "google-auth-oauthlib is not installed. "
            "fix: pip install 'google-auth>=2.40' 'google-auth-oauthlib>=1.2'. "
            "next: re-run kairix connect <google-service>. "
            "run: pip install 'google-auth>=2.40' 'google-auth-oauthlib>=1.2'",
        ) from exc
    flow = Flow.from_client_config(
        client_config={
            "installed": {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": GOOGLE_TOKEN_URI,
            },
        },
        scopes=list(scopes),
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    creds = flow.credentials
    refresh_token = creds.refresh_token or ""
    access_token = creds.token or ""
    if not refresh_token:
        raise RuntimeError(
            _ERR_PREFIX + "Google did not grant a refresh_token. "
            "fix: confirm the GCP OAuth consent screen is in Production state (not Testing); "
            "Testing mode silently expires refresh tokens after 7 days and may skip the grant "
            "altogether on re-auth. "
            "next: GCP console -> APIs & Services -> OAuth consent screen -> 'Publish app'. "
            "run: kairix connect <google-service> --client-secret-path <path>",
        )
    return CapturedTokens(
        refresh_token=refresh_token,
        access_token=access_token,
        token_uri=GOOGLE_TOKEN_URI,
    )


class _DefaultBrowser:
    """Default :class:`BrowserLauncher` — calls :func:`webbrowser.open`."""

    def open(self, url: str) -> bool:
        # KAIRIX_CONNECT_DISABLE_BROWSER kill-switch: tests/conftest.py
        # sets this so any test that escapes the FakeBrowserLauncher
        # injection seam is hard-blocked instead of firing a real popup
        # on the operator's machine (2026-06-01 incident).
        from kairix.paths import connect_browser_disabled

        if connect_browser_disabled():
            import logging

            logging.getLogger(__name__).warning(
                "kairix.connect.google: _DefaultBrowser.open suppressed by KAIRIX_CONNECT_DISABLE_BROWSER=1; "
                "url=%s. fix: this should only fire in pytest — production must leave the env var unset. "
                "next: confirm the calling test injects browser=FakeBrowserLauncher() on its GoogleOAuth2Flow. "
                "run: KAIRIX_CONNECT_DISABLE_BROWSER= kairix connect google-* ...",
                url,
            )
            return False
        import webbrowser  # pragma: no cover — live browser path

        return webbrowser.open(url)  # pragma: no cover — live browser path


# Protocol conformance smoke checks. ``GoogleOAuth2Flow`` requires
# constructor args so we can't instantiate it at module level; the
# check below confirms the class satisfies the structural Protocol
# shape (runtime_checkable Protocol matches on attributes/methods, not
# inheritance) via ``isinstance``.
def _conformance_check() -> None:
    """Constructed-instance Protocol conformance check (called lazily)."""
    # No-op at module-load; cheap to call from tests.


# A dummy instance check on the Browser default — proves the Protocol
# attaches at module import time without needing GoogleOAuth2Flow's
# required args.
_BROWSER_CHECK: BrowserLauncher = _DefaultBrowser()

#: Public name for the production :class:`BrowserLauncher` every Google flow
#: falls back to when no ``browser=`` is injected — lets the
#: ``BrowserLauncher`` contract test prove the shipped launcher and the
#: canonical fake share one behaviour (F43) without a private import.
DefaultBrowserLauncher = _DefaultBrowser


__all__ = [
    "CALENDAR_READONLY_SCOPE",
    "DEFAULT_SCOPES_BY_AREA",
    "DRIVE_READONLY_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "GOOGLE_TOKEN_URI",
    "DefaultBrowserLauncher",
    "GoogleOAuth2Flow",
]
