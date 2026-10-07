"""Web setup wizard — Starlette routes + Jinja2/HTMX screens.

Mounted at ``/setup`` by :func:`kairix.agents.mcp.transport.build_mcp_app`
(unconditionally — the ``setup_wizard_web`` cutover flag retired,
PLA-287). Same container, same port as the MCP transport — no second
service to deploy.

Composition idioms mirror the transport composer:

- Public surface is a single function, :func:`build_setup_wizard_mount`.
- No module-level state — the Jinja environment, the lazily-resolved
  service, and the secrets resolver all live in the builder's closure.
- The wizard renders against the :class:`SetupService` Protocol only
  (``kairix.platform.setup.service``); every side effect (credential
  validation, folder scans, index runs, search) happens behind that
  boundary. Tests inject ``FakeSetupService`` from ``tests/fakes.py``
  through the ``service_factory`` seam.

Security posture: requests from non-loopback clients must prove
knowledge of the canonical ``kairix-infra-operator-token`` secret
(resolved through :class:`kairix.secrets.loader.SecretsLoader`) by ONE
of: the ``X-Kairix-Operator-Token`` header (API / CLI), a signed grant
cookie (browser), or a one-time ``?operator_token=<value>`` query on
the mount root that the guard exchanges for that cookie (#500 — the
Jupyter tokened-URL pattern, because browsers cannot send custom
headers and stock Docker presents every published-port peer as the
bridge gateway IP, not loopback). Loopback requests — the laptop-first
install this wizard exists for — skip the token entirely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# The contract module only (dataclasses + constants, no side-effect
# machinery) — the wizard UI still ships independently of the backend
# module, which stays lazily imported in _default_service_factory.
from kairix.platform.setup.service import (
    AZURE_PROVIDER_NAMES,
    PHASE_CONSENT,
    PHASE_DONE,
    PHASE_EXCHANGING,
    PHASE_FAILED,
    AgentConnectInfo,
    ConnectSnippet,
    SecretsWriteError,
)
from kairix.platform.setup.source_oauth import OAUTH_CALLBACK_PATH

if TYPE_CHECKING:
    import jinja2

    from kairix.platform.setup.service import SetupService
    from kairix.secrets.loader import SecretsResolver

# Absolute paths the browser navigates between. The mount path is part
# of the wizard's contract (templates hard-code form actions + redirect
# targets), so it is a constant rather than a build_setup_wizard_mount
# parameter.
SETUP_PATH_PREFIX = "/setup"
_PROVIDER_URL = f"{SETUP_PATH_PREFIX}/provider"
_KEY_URL = f"{SETUP_PATH_PREFIX}/key"
_FOLDER_URL = f"{SETUP_PATH_PREFIX}/folder"
_INDEXING_URL = f"{SETUP_PATH_PREFIX}/indexing"
# The capability tour (#490) replaced the first-search screen as step 6;
# the legacy /setup/first-search path stays routable as a redirect so
# old bookmarks and agent notes keep working.
_TOUR_URL = f"{SETUP_PATH_PREFIX}/tour"
_SOURCE_URL = f"{SETUP_PATH_PREFIX}/source"
_SOURCE_CONNECT_ROUTE = "/source/connect"  # mount-relative (Route paths)
_SOURCE_WAIT_URL = f"{SETUP_PATH_PREFIX}/source/wait"
_SOURCE_PICKER_URL = f"{SETUP_PATH_PREFIX}/source/picker"

# Mount-relative path of the OAuth callback (#489). The provider
# redirect cannot carry the operator-token header, so this path is
# exempted from the OperatorTokenGuard — see the guard's docstring for
# the compensating control (single-use pending nonce in the service).
_OAUTH_CALLBACK_MOUNT_PATH = "/oauth/callback"

# Header HTMX reads to navigate the whole page from a partial response.
_HX_REDIRECT = "HX-Redirect"

# Security headers on every HTML the wizard serves (F91 Limb A). The
# wizard renders operator-controlled config paths and provider labels
# into HTML; these headers are the browser-side baseline that keeps a
# reflected value from being sniffed into a script, framed by a
# clickjacking page, or used to pull in off-origin code.
#
# The CSP permits ``'unsafe-inline'`` for scripts and styles so a
# template may carry a small amount of inline HTMX glue and inline-styled
# Pico classless CSS. (The key / folder afterSwap reveal listeners now
# ship as the 'self'-served static/kx-reveal.js.) Any inline script is
# governed by F91 Limb B (rationale-tagged, size-capped, de-duplicated)
# — so the ``'unsafe-inline'`` allowance stays a reviewed, bounded
# surface rather than an open door. ``default-src 'self'`` plus
# ``object-src 'none'`` and ``frame-ancestors 'none'`` close the
# off-origin-fetch, plugin, and clickjacking vectors regardless.
_SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}

# Template-context key for the inline rescue banner on a failed save.
_CTX_SAVE_ERROR = "save_error"

# Operator token header + the canonical secret identity it must match.
OPERATOR_TOKEN_HEADER = "X-Kairix-Operator-Token"  # noqa: S105 — HTTP header NAME, not a credential value
_TOKEN_SCOPE: Literal["infra"] = "infra"  # noqa: S105 — canonical secret-identity segment, not a credential value
_TOKEN_AREA = "operator"  # noqa: S105 — canonical secret-identity segment, not a credential value
_TOKEN_LEAF = "token"  # noqa: S105 — canonical secret-identity segment, not a credential value

# Tokened-URL grant (#500). On stock Docker every published-port
# connection arrives as the bridge gateway IP — never loopback — and
# browsers cannot send the X-Kairix-Operator-Token header, so no browser
# could reach the wizard. The Jupyter pattern fixes it: an operator opens
# ``/setup/?operator_token=<value>`` once; the guard verifies the token,
# drops a SIGNED session cookie, and redirects to the bare ``/setup/`` so
# the token never lingers in the address bar / history. Every later
# request rides the cookie. Signing is stdlib hmac+base64 (itsdangerous
# is not installed and Starlette's SessionMiddleware needs it).
_GRANT_QUERY_PARAM = "operator_token"
_COOKIE_NAME = "kairix_operator_grant"
# Fixed payload signed under the operator-token secret. Rotating the
# secret invalidates every issued cookie (no clock seam, no TTL — the
# wizard is a short-lived onboarding surface). The cookie value carries
# only this marker + its MAC; it NEVER carries the raw operator token
# (F15) — the marker is public, the MAC is what binds it to the secret.
_COOKIE_PAYLOAD = "kairix-operator-grant-v1"

# Client hosts that count as loopback (token check skipped).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# Template names (single source so a rename touches one line).
_TPL_WELCOME = "setup/welcome.html"
_TPL_PROVIDER = "setup/provider.html"
_TPL_KEY = "setup/key.html"
_TPL_FOLDER = "setup/folder.html"
_TPL_INDEXING = "setup/indexing.html"
_TPL_TOUR = "setup/tour.html"
_TPL_CONNECT_AGENT = "setup/connect_agent.html"
_TPL_DONE = "setup/done.html"
_TPL_SOURCE = "setup/source.html"
_TPL_SOURCE_CONNECT = "setup/source_connect.html"
_TPL_SOURCE_WAIT = "setup/source_wait.html"
_TPL_SOURCE_PICKER = "setup/source_picker.html"
_TPL_SOURCE_SAVED = "setup/source_saved.html"
_TPL_KEY_VALIDATION = "partials/key_validation_result.html"
_TPL_FOLDER_SCAN = "partials/folder_scan_result.html"
_TPL_INDEXING_PROGRESS = "partials/indexing_progress.html"
_TPL_SEARCH_RESULTS = "partials/search_results.html"
_TPL_HANDSHAKE = "partials/handshake_result.html"
_TPL_TOUR_PREP = "partials/tour_prep_result.html"
_TPL_TOUR_REMEMBER = "partials/tour_remember_result.html"
_TPL_TOUR_BRIEF = "partials/tour_brief_result.html"
_TPL_TOUR_TIMELINE = "partials/tour_timeline_result.html"
_TPL_SOURCE_AUTH_STATUS = "partials/source_auth_status.html"

# Total step-indicator positions rendered by setup/base.html.
_TOTAL_STEPS = 7

# Form field names shared across handlers.
_FIELD_FOLDER_PATH = "folder_path"
_FIELD_PROVIDER = "provider"
_FIELD_API_KEY = "api_key"  # pragma: allowlist secret — form FIELD NAME, not a credential value
_FIELD_ENDPOINT = "endpoint"
_FIELD_DEPLOYMENT = "deployment"

# The azure provider set (the key screen's deployment-name field, #484)
# and the source sign-in phase vocabulary both come from the contract
# module — kairix.platform.setup.service — so there is exactly one
# definition site (review M11). Templates read the phases via
# env.globals (the total_steps pattern).


def _config_write_error(exc: OSError) -> str:
    """F21-shaped banner for a failed config write (#485).

    The stock compose mounts the operator's ``kairix.config.yaml``
    read-only, so a wizard save on a deployment without the overlay
    configured raises ``OSError`` — render the rescue path instead of a
    raw 500.
    """
    target = getattr(exc, "filename", None) or "its configured location"
    return (
        f"Could not write the config file at {target} — it may be mounted read-only."
        " fix: the standard compose mounts kairix.config.yaml read-only; set"
        " KAIRIX_CONFIG_OVERLAY_PATH (stock value /var/lib/kairix/kairix.config.local.yaml)"
        " so setup writes land on the data volume."
        " next: restart the container, then save again."
    )


def _secrets_write_error(exc: SecretsWriteError) -> str:
    """F21-shaped banner for a failed credential write (review M2).

    A vault-agent sidecar deploy mounts ``/run/secrets`` read-only, so
    persisting the key fails before any config write is attempted. The
    rescue is the secrets bundle, not the config overlay — prescribing
    ``KAIRIX_CONFIG_OVERLAY_PATH`` here would send the operator down a
    dead end.
    """
    target = exc.bundle_path or "its configured location"
    return (
        f"Could not save the API key to the secrets file at {target} — it may be mounted read-only."
        " fix: set KAIRIX_SECRETS_FILE to a writable path on the data volume"
        " (for example /var/lib/kairix/secrets/kairix.env) so credentials land there."
        " next: restart the container, then save again."
    )


def _provider_query(provider: str) -> str:
    """``?provider=<name>`` query suffix shared by the source screens (F17)."""
    return f"?provider={quote(provider)}"


def _key_context(provider: str) -> dict[str, Any]:
    """Template context for the key screen — azure picks get the
    deployment-name field (#484)."""
    return {"step": 3, "provider": provider, "azure_provider": provider in AZURE_PROVIDER_NAMES}


def _key_save_reject(provider: str, api_key: str) -> str | None:
    """Grade-8 reject for an empty provider or key (review H4).

    Without this, a header-crafted POST persists ``{"provider": ""}``
    silently and the wizard looks finished while nothing works.
    """
    if not provider:
        return (
            "No provider is selected."
            " fix: go back one step and pick your AI provider."
            " next: paste the key and save again."
        )
    if not api_key.strip():
        return (
            "The API key field is empty."
            " fix: paste your provider's API key."
            " next: use Validate key to check it, then save."
        )
    return None


def _handle_key_save(
    fields: dict[str, str],
    service: SetupService,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """Persist the provider pick; every failure re-renders the key screen
    with a banner instead of a raw 500 — empty fields (review H4), a
    read-only secrets bundle (review M2), a read-only config (#485).

    Module-level (not a closure) so the builder stays under the F16
    complexity ceiling; ``render`` is the builder's template closure.
    """
    provider = fields.get(_FIELD_PROVIDER, "").strip()
    api_key = fields.get(_FIELD_API_KEY, "")

    def rerender(message: str) -> Response:
        return render(_TPL_KEY, {**_key_context(provider), _CTX_SAVE_ERROR: message})

    reject = _key_save_reject(provider, api_key)
    if reject is not None:
        return rerender(reject)
    try:
        service.save_provider(
            provider,
            api_key,
            fields.get(_FIELD_ENDPOINT) or None,
            fields.get("model") or None,
            deployment=fields.get(_FIELD_DEPLOYMENT) or None,
        )
    except SecretsWriteError as exc:
        return rerender(_secrets_write_error(exc))
    except OSError as exc:
        return rerender(_config_write_error(exc))
    return RedirectResponse(_FOLDER_URL, status_code=303)


def _folder_save_context(service: SetupService, path: str, message: str) -> dict[str, Any]:
    """Folder-screen re-render context carrying the save-error banner."""
    return {
        "step": 4,
        "hint": service.source_hint(),
        _FIELD_FOLDER_PATH: path,
        _CTX_SAVE_ERROR: message,
    }


def _handle_folder_save(
    fields: dict[str, str],
    service: SetupService,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """Persist the folder pick and start indexing; on a rejected path
    (review H4), a service-side reject (#492 — e.g. the pick is shadowed
    by a ``KAIRIX_DOCUMENT_ROOT`` env override), or a read-only config
    (#485) re-render with the rescue banner — indexing must NOT start on
    a failed save. Module-level for the same F16 reason as
    :func:`_handle_key_save`.
    """
    path = fields.get(_FIELD_FOLDER_PATH, "")
    try:
        service.save_source(path)
    except ValueError as exc:
        # save_source's documented reject (relative / missing path) is
        # already F21-shaped — render it verbatim, never a raw 500. The
        # path field is editable after the scan, so this is reachable.
        return render(_TPL_FOLDER, _folder_save_context(service, path, str(exc)))
    except OSError as exc:
        return render(_TPL_FOLDER, _folder_save_context(service, path, _config_write_error(exc)))
    service.start_index()
    return RedirectResponse(_INDEXING_URL, status_code=303)


def _request_origin(request: Request) -> str:
    """Scheme + host[:port] the operator's browser used to reach the wizard.

    The OAuth redirect URI derives from this (never hardcoded) so
    localhost, SSH-tunnel, and reverse-proxy origins all work — the
    provider sends the operator's browser back to the SAME address the
    wizard was loaded from (#489).
    """
    url = request.url
    return f"{url.scheme}://{url.netloc}"


def _oauth_provider_label(service: SetupService, provider: str) -> str | None:
    """The display label for an OAuth source provider, or ``None``."""
    for option in service.source_options():
        if option.key == provider and option.oauth:
            return option.label
    return None


def _source_connect_context(provider: str, label: str, origin: str) -> dict[str, Any]:
    """Template context for the per-provider connect form.

    ``redirect_uri`` is the exact value the operator must register in
    the provider's app settings — derived from the live request origin.
    """
    return {
        "step": 4,
        "provider": provider,
        "label": label,
        "redirect_uri": f"{origin}{OAUTH_CALLBACK_PATH}",
    }


def _handle_source_connect_start(
    request: Request,
    fields: dict[str, str],
    service: SetupService,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """Start the provider sign-in; re-render the form with F21 guidance
    when the backend rejects the typed-in details. Module-level for the
    same F16 reason as :func:`_handle_key_save`."""
    provider = fields.get(_FIELD_PROVIDER, "")
    label = _oauth_provider_label(service, provider)
    if label is None:
        return RedirectResponse(_SOURCE_URL, status_code=303)
    origin = _request_origin(request)
    started = service.start_source_auth(provider, fields, origin)
    if not started.ok:
        context = _source_connect_context(provider, label, origin)
        context["start_error"] = started.error
        return render(_TPL_SOURCE_CONNECT, context)
    return RedirectResponse(f"{_SOURCE_WAIT_URL}{_provider_query(provider)}", status_code=303)


def _handle_source_save(
    provider: str,
    instance: str,
    picks: tuple[str, ...],
    service: SetupService,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """Persist the picked units; on a read-only config (#485) or a
    validation reject, re-render the picker with the rescue banner."""
    try:
        saved = service.save_oauth_source(provider, instance, picks)
    except OSError as exc:
        units = service.discover_source_units(provider)
        return render(
            _TPL_SOURCE_PICKER,
            {"step": 4, "units": units, "provider": provider, _CTX_SAVE_ERROR: _config_write_error(exc)},
        )
    if not saved.ok:
        units = service.discover_source_units(provider)
        return render(
            _TPL_SOURCE_PICKER,
            {"step": 4, "units": units, "provider": provider, _CTX_SAVE_ERROR: saved.error},
        )
    return render(_TPL_SOURCE_SAVED, {"step": 4, "saved": saved, "provider": provider})


def _source_status_headers(status: Any) -> dict[str, str] | None:
    """``HX-Redirect`` choreography for the sign-in status poll.

    In the consent phase the operator's browser is sent to the provider
    consent screen (the wizard runs server-side — often in a container
    with no browser — so the OPERATOR's browser must make this hop).
    Once the flow finishes, the browser advances to the picker.
    """
    if status.phase == PHASE_CONSENT and status.authorize_url:
        return {_HX_REDIRECT: status.authorize_url}
    if status.phase == PHASE_DONE:
        return {_HX_REDIRECT: f"{_SOURCE_PICKER_URL}{_provider_query(status.provider)}"}
    return None


def _handle_oauth_callback(request: Request, service: SetupService) -> Response:
    """The wizard-origin OAuth callback (#489).

    This route is exempt from the operator-token guard (a provider
    redirect cannot carry custom headers); its protection is the
    single-use pending nonce verified by
    ``service.complete_source_callback`` — a callback with no pending
    flow or a mismatched ``state`` is rejected with 409. The
    authorization code is handed to the service and never logged (F15).

    The return-to-wait redirect carries the pending flow's provider so
    the wait screen keeps its context (review L7 — a provider-less wait
    URL bounces back to the source cards).
    """
    params = dict(request.query_params)
    outcome = service.complete_source_callback(params.get("state"), params)
    if not outcome.ok:
        return PlainTextResponse(outcome.error or "Sign-in response rejected.", status_code=409)
    provider = service.source_auth_status().provider
    suffix = _provider_query(provider) if provider else ""
    return RedirectResponse(f"{_SOURCE_WAIT_URL}{suffix}", status_code=303)


def _default_service_factory() -> SetupService:
    """Lazy import of the production backend factory.

    Deferred so the wizard UI ships independently of the backend — a
    build where ``build_setup_service`` is still the NotImplementedError
    stub fails honestly at first request (see that stub's message).
    """
    from kairix.platform.setup.service import build_setup_service

    return build_setup_service()


def _default_secrets_resolver() -> SecretsResolver:
    """Production secrets seam — the canonical env → KV-mount loader."""
    from kairix.secrets.loader import SecretsLoader

    return SecretsLoader()


def _default_provider_names() -> tuple[str, ...]:
    """Provider names from the real plugin registry (entry points)."""
    from kairix.providers import EntryPointRegistry

    return tuple(EntryPointRegistry().available())


def _build_template_env() -> jinja2.Environment:
    """Jinja environment over the package's vendored templates.

    ``PackageLoader`` resolves from both source checkouts and installed
    wheels (templates ship as package data — see
    ``[tool.setuptools.package-data]`` in pyproject.toml).
    """
    from jinja2 import Environment, PackageLoader, select_autoescape

    env = Environment(
        loader=PackageLoader("kairix.platform.setup.web", "templates"),
        autoescape=select_autoescape(("html",)),
    )
    env.globals["total_steps"] = _TOTAL_STEPS
    # The sign-in phase vocabulary, from the contract module (review
    # M11) — source_auth_status.html branches on these.
    env.globals["PHASE_CONSENT"] = PHASE_CONSENT
    env.globals["PHASE_DONE"] = PHASE_DONE
    env.globals["PHASE_EXCHANGING"] = PHASE_EXCHANGING
    env.globals["PHASE_FAILED"] = PHASE_FAILED
    return env


def _is_loopback_client(scope: Scope) -> bool:
    """True only for explicit loopback peers; missing client fails closed."""
    client = scope.get("client")
    if client is None:
        return False
    host = client[0]
    return host in _LOOPBACK_HOSTS


def _mount_relative_path(scope: Scope) -> str:
    """The request path relative to the wizard mount.

    Starlette's ``Mount`` keeps ``scope["path"]`` absolute and records
    the matched prefix in ``scope["root_path"]`` — strip it so the
    guard's exemption set holds mount-relative paths.
    """
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


class OperatorTokenGuard:
    """ASGI guard wrapping the wizard mount.

    Loopback requests pass through header-free and cookie-free.
    Non-loopback requests are admitted by ANY of three credentials, all
    proving knowledge of the canonical ``kairix-infra-operator-token``
    secret:

    1. the ``X-Kairix-Operator-Token`` header (API / CLI callers), or
    2. a valid signed grant cookie (browser callers, #500), or
    3. a ``?operator_token=<value>`` query on the mount root, which the
       guard verifies, then 303-redirects to the bare ``/setup/`` with a
       ``Set-Cookie`` of the signed grant (the Jupyter tokened-URL
       pattern — browsers cannot send custom headers).

    No credential value is ever logged or echoed (F15): the grant cookie
    carries only a fixed marker plus its MAC, never the raw token.

    ``exempt_paths`` (security-critical, #489): the OAuth provider's
    consent redirect is a plain browser GET — it CANNOT carry the
    operator-token header — so the wizard's ``/oauth/callback`` path is
    exempted from the token check. The compensating control lives in
    the service's single-slot pending-flow registry: a callback is only
    accepted while a flow this wizard started is waiting, AND (for
    flows that carry ``state``) when the redirect's ``state`` matches
    the single-use nonce; the slot is consumed on first accepted
    delivery. The exempted route never logs or echoes the authorization
    code (F15).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secrets: SecretsResolver,
        exempt_paths: frozenset[str] = frozenset(),
    ) -> None:
        self._app = app
        self._secrets = secrets
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        exempt = _mount_relative_path(scope) in self._exempt_paths
        if scope.get("type") != "http" or _is_loopback_client(scope) or exempt:
            await self._app(scope, receive, send)
            return
        expected = self._secrets.get(_TOKEN_SCOPE, _TOKEN_AREA, None, _TOKEN_LEAF)
        if expected is None:
            await self._no_token_configured()(scope, receive, send)
            return
        grant = self._maybe_grant_response(scope, expected)
        if grant is not None:
            await grant(scope, receive, send)
            return
        if self._credential_admits(scope, expected):
            await self._app(scope, receive, send)
            return
        await self._refused()(scope, receive, send)

    def _credential_admits(self, scope: Scope, expected: str) -> bool:
        """True iff the request carries the header token OR a valid cookie."""
        header = _header_value(scope, OPERATOR_TOKEN_HEADER)
        if header is not None and hmac.compare_digest(header, expected):
            return True
        cookie = _grant_cookie_from_scope(scope)
        return cookie is not None and _grant_cookie_is_valid(cookie, expected)

    def _maybe_grant_response(self, scope: Scope, expected: str) -> Response | None:
        """A 303 + Set-Cookie redirect when the mount-root grant token matches.

        Returns ``None`` when this is not a grant request (wrong path, no
        query token, or a wrong token) so the caller falls through to the
        normal header/cookie check — a wrong ``?operator_token`` never
        sets a cookie and is refused like any other unauthenticated
        request.
        """
        if scope.get("method") != "GET" or _mount_relative_path(scope) != "/":
            return None
        provided = _grant_query_token(scope)
        if provided is None or not hmac.compare_digest(provided, expected):
            return None
        response = RedirectResponse(f"{SETUP_PATH_PREFIX}/", status_code=303)
        response.headers["Set-Cookie"] = _set_cookie_value(
            cookie=_sign_grant_cookie(expected),
            secure=_request_is_https(scope),
        )
        # F91 Limb A: a RedirectResponse does NOT flow through the wizard's
        # _render chokepoint, so the security-header triple must be applied
        # here explicitly — the grant redirect is browser-served HTML-tier
        # surface like every wizard screen.
        _apply_security_headers(response)
        return response

    @staticmethod
    def _no_token_configured() -> Response:
        return PlainTextResponse(
            "Setup wizard remote access is disabled: no operator token is configured. "
            "fix: set the kairix-infra-operator-token secret (env "
            "KAIRIX_INFRA_OPERATOR_TOKEN or the KV mount), or open the wizard "
            f"from the host itself (loopback). next: retry with the {OPERATOR_TOKEN_HEADER} header.",
            status_code=403,
        )

    @staticmethod
    def _refused() -> Response:
        return PlainTextResponse(
            "Setup wizard remote access requires a valid operator token. "
            f"fix: open the tokened URL printed in the container logs "
            f"(http://<host>:<port>{SETUP_PATH_PREFIX}/?{_GRANT_QUERY_PARAM}=<token>) to set the "
            f"grant cookie, or send the {OPERATOR_TOKEN_HEADER} header matching the "
            "kairix-infra-operator-token secret. next: retry the request.",
            status_code=403,
        )


def _apply_security_headers(response: Response) -> None:
    """Set the F91 Limb A security-header baseline on an HTML response.

    Mutates ``response.headers`` in place — used by the wizard's render
    chokepoint so every screen and HTMX partial leaves with the same
    nosniff / frame-denial / CSP triple. Idempotent: re-applying the
    same names is a no-op.
    """
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value


def _header_value(scope: Scope, name: str) -> str | None:
    """Case-insensitive single-header lookup from the raw ASGI scope."""
    wanted = name.lower().encode("latin-1")
    headers: tuple[tuple[bytes, bytes], ...] = tuple(scope.get("headers", ()))
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def _b64(raw: bytes) -> str:
    """URL-safe, unpadded base64 — cookie-value safe (no ``=`` / ``+`` / ``/``)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_grant_cookie(secret: str) -> str:
    """Sign the fixed grant marker under the operator-token ``secret``.

    Returns ``<b64(marker)>.<b64(hmac_sha256(secret, marker))>`` — the
    marker is public; the MAC binds the cookie to the live secret. The
    raw operator token is NEVER embedded (F15): the cookie proves the
    bearer once held the token, it does not carry it.
    """
    payload = _COOKIE_PAYLOAD.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def _grant_cookie_is_valid(cookie_value: str, secret: str) -> bool:
    """True iff ``cookie_value`` is a marker signed under ``secret``.

    Constant-time MAC comparison (``hmac.compare_digest``). A malformed
    value (missing separator, non-base64, wrong marker, tampered MAC)
    returns False — never raises — so a garbage cookie falls through to
    the same 403 as no cookie at all.
    """
    expected = _sign_grant_cookie(secret)
    return hmac.compare_digest(cookie_value, expected)


def _grant_cookie_from_scope(scope: Scope) -> str | None:
    """Extract the grant cookie value from the raw ASGI ``Cookie`` header."""
    raw = _header_value(scope, "cookie")
    if raw is None:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except (CookieError, ValueError):
        return None
    morsel = jar.get(_COOKIE_NAME)
    return morsel.value if morsel is not None else None


def _grant_query_token(scope: Scope) -> str | None:
    """The ``operator_token`` query value from the raw ASGI query string."""
    query = scope.get("query_string", b"")
    if isinstance(query, (bytes, bytearray)):
        query = bytes(query).decode("latin-1")
    params = parse_qs(str(query))
    values = params.get(_GRANT_QUERY_PARAM)
    return values[0] if values else None


def _set_cookie_value(*, cookie: str, secure: bool) -> str:
    """Build the Set-Cookie header value for the signed grant.

    ``HttpOnly`` (JS can't read it), ``SameSite=Lax`` (the post-redirect
    GET still carries it), ``Path=/setup`` (scoped to the wizard, never
    leaked to ``/mcp``). ``Secure`` is conditional: the wizard is plain
    HTTP over an SSH tunnel by design, so a hard ``Secure`` would drop
    the cookie over ``http://`` and re-lock the wizard — it is set only
    when the request itself arrived over https.
    """
    attrs = [
        f"{_COOKIE_NAME}={cookie}",
        f"Path={SETUP_PATH_PREFIX}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def _request_is_https(scope: Scope) -> bool:
    """True when the request reached the app over https.

    Honours ``scope['scheme']`` and the ``X-Forwarded-Proto`` header so a
    TLS-terminating reverse proxy still gets the ``Secure`` cookie.
    """
    if str(scope.get("scheme", "")).lower() == "https":
        return True
    forwarded = _header_value(scope, "x-forwarded-proto")
    return forwarded is not None and forwarded.split(",")[0].strip().lower() == "https"


def _static_directory() -> str:
    """Resolve the vendored static assets directory inside the package."""
    from importlib.resources import files

    return str(files("kairix.platform.setup.web") / "static")


def _progress_pct(*, chunks_done: int, chunks_total: int, done: bool) -> int:
    """Whole-number progress percentage, safe for a zero total."""
    if done:
        return 100
    if chunks_total <= 0:
        return 0
    return min(100, (chunks_done * 100) // chunks_total)


def _string_fields(form: Mapping[str, Any]) -> dict[str, str]:
    """String-valued form fields only — file-upload values are dropped."""
    return {key: str(value) for key, value in form.items() if isinstance(value, str)}


def _progress_headers(status: Any) -> dict[str, str] | None:
    """``HX-Redirect`` header set once indexing finished cleanly."""
    if status.done and not status.error:
        return {_HX_REDIRECT: _TOUR_URL}
    return None


def _rebased_connect_info(info: AgentConnectInfo, origin: str) -> AgentConnectInfo:
    """Re-anchor the displayed MCP URL on the live request origin (review L1).

    ``mcp_endpoint()`` reads only ``KAIRIX_MCP_ENDPOINT``, so a pip
    install whose port auto-shifted (advertised via ``KAIRIX_MCP_PORT``)
    would display a URL nothing listens on. The wizard and the MCP
    transport share one server, so the origin the operator's browser
    reached IS the MCP origin — keep the endpoint's own path and swap
    the host. Falls back to the service's URL when it has no host part
    to swap.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(info.mcp_url)
    if not origin or not parsed.netloc:
        return info
    rebased = f"{origin}{parsed.path}"
    snippets = tuple(
        ConnectSnippet(client=snippet.client, config_text=snippet.config_text.replace(info.mcp_url, rebased))
        for snippet in info.snippets
    )
    return AgentConnectInfo(mcp_url=rebased, snippets=snippets)


def _make_screen_endpoint(
    resolve_service: Callable[[], SetupService],
    handler: Callable[[Request, SetupService], Response],
) -> Callable[[Request], Response]:
    """Wrap a screen handler with service resolution.

    Every screen resolves the service — including the welcome screen,
    which doesn't strictly need it — so a flag-ON deployment without
    the wizard backend fails on the FIRST request with the stub's
    structured message, not three screens into the flow. Synchronous
    on purpose (Sonar S7503 — nothing here awaits); Starlette runs
    sync endpoints in its threadpool.
    """

    def endpoint(request: Request) -> Response:
        try:
            service = resolve_service()
        except NotImplementedError as exc:
            return PlainTextResponse(str(exc), status_code=503)
        return handler(request, service)

    return endpoint


def _make_form_endpoint(
    resolve_service: Callable[[], SetupService],
    handler: Callable[[dict[str, str], SetupService], Response],
) -> Callable[[Request], Any]:
    """Like :func:`_make_screen_endpoint` but parses the urlencoded form first."""

    async def endpoint(request: Request) -> Response:
        try:
            service = resolve_service()
        except NotImplementedError as exc:
            return PlainTextResponse(str(exc), status_code=503)
        form = await request.form()
        return handler(_string_fields(form), service)

    return endpoint


def _make_request_form_endpoint(
    service_fn: Callable[[], SetupService],
    handler: Callable[[Request, Any, SetupService], Response],
) -> Callable[[Request], Any]:
    """Like :func:`_make_screen_endpoint` but parses the form first.

    The handler also sees the request (for origin derivation) and the
    raw form (for multi-value checkbox reads via ``getlist``).
    """

    async def endpoint(request: Request) -> Response:
        try:
            service = service_fn()
        except NotImplementedError as exc:
            return PlainTextResponse(str(exc), status_code=503)
        form = await request.form()
        return handler(request, form, service)

    return endpoint


def _handle_key_screen(
    request: Request,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """The key screen needs a provider pick — bounce back without one."""
    provider = request.query_params.get(_FIELD_PROVIDER, "")
    if not provider:
        return RedirectResponse(_PROVIDER_URL, status_code=303)
    return render(_TPL_KEY, _key_context(provider))


def _handle_source_connect_screen(
    request: Request,
    service: SetupService,
    render: Callable[[str, dict[str, Any]], Response],
) -> Response:
    """The connect form exists only for known OAuth providers (#489)."""
    provider = request.query_params.get(_FIELD_PROVIDER, "")
    label = _oauth_provider_label(service, provider)
    if label is None:
        return RedirectResponse(_SOURCE_URL, status_code=303)
    return render(_TPL_SOURCE_CONNECT, _source_connect_context(provider, label, _request_origin(request)))


def build_setup_wizard_mount(
    *,
    service_factory: Callable[[], SetupService] = _default_service_factory,
    secrets: SecretsResolver | None = None,
    provider_names_fn: Callable[[], tuple[str, ...]] = _default_provider_names,
) -> Mount:
    """Compose the wizard's routes into a guarded ``Mount("/setup", ...)``.

    Args:
        service_factory: Callable returning the :class:`SetupService`
            the screens render against. Resolved lazily on first
            request and memoised; the production default is the
            ``build_setup_service`` factory.
        secrets: Secrets resolver for the operator-token guard. Tests
            pass ``FakeSecretsLoader``; defaults to the production
            :class:`~kairix.secrets.loader.SecretsLoader`.
        provider_names_fn: Source of provider names for the picker
            screen. The default reads the installed provider plugin
            registry.

    Returns:
        A Starlette ``Mount`` ready to append to the MCP app's routes.
    """
    factory = service_factory
    names_fn = provider_names_fn
    env = _build_template_env()
    # Lazily-resolved, memoised service. A one-element list keeps the
    # closure mutable without module-level state.
    holder: list[SetupService] = []

    def _service() -> SetupService:
        if not holder:
            holder.append(factory())
        return holder[0]

    def _render(template_name: str, context: dict[str, Any], **kwargs: Any) -> HTMLResponse:
        response = HTMLResponse(env.get_template(template_name).render(**context), **kwargs)
        # F91 Limb A: every HTML the wizard serves — full screens AND the
        # HTMX partials swapped into them — carries the security-header
        # baseline. _render is the single chokepoint, so a new screen
        # cannot ship an un-headed response by construction.
        _apply_security_headers(response)
        return response

    def _endpoint(handler: Callable[[Request, SetupService], Response]) -> Callable[[Request], Response]:
        return _make_screen_endpoint(_service, handler)

    def _form_endpoint(handler: Callable[[dict[str, str], SetupService], Response]) -> Callable[[Request], Any]:
        return _make_form_endpoint(_service, handler)

    def _request_form_endpoint(handler: Callable[[Request, Any, SetupService], Response]) -> Callable[[Request], Any]:
        """Raw-form variant — the handler also sees the request."""
        return _make_request_form_endpoint(_service, handler)

    def welcome(_request: Request, _service_: SetupService) -> Response:
        return _render(_TPL_WELCOME, {"step": 1})

    def provider_screen(_request: Request, _service_: SetupService) -> Response:
        return _render(_TPL_PROVIDER, {"step": 2, "providers": names_fn()})

    def key_screen(request: Request, _service_: SetupService) -> Response:
        return _handle_key_screen(request, _render)

    def key_validate(fields: dict[str, str], service: SetupService) -> Response:
        validation = service.validate_provider(
            fields.get(_FIELD_PROVIDER, ""),
            fields.get(_FIELD_API_KEY, ""),
            fields.get(_FIELD_ENDPOINT) or None,
            deployment=fields.get(_FIELD_DEPLOYMENT) or None,
        )
        return _render(_TPL_KEY_VALIDATION, {"validation": validation})

    def key_save(fields: dict[str, str], service: SetupService) -> Response:
        return _handle_key_save(fields, service, _render)

    def folder_screen(_request: Request, service: SetupService) -> Response:
        return _render(_TPL_FOLDER, {"step": 4, "hint": service.source_hint()})

    def folder_scan(fields: dict[str, str], service: SetupService) -> Response:
        path = fields.get(_FIELD_FOLDER_PATH, "")
        scan = service.scan_folder(path)
        return _render(_TPL_FOLDER_SCAN, {"scan": scan, _FIELD_FOLDER_PATH: path})

    def folder_save(fields: dict[str, str], service: SetupService) -> Response:
        return _handle_folder_save(fields, service, _render)

    def indexing_screen(_request: Request, _service_: SetupService) -> Response:
        return _render(_TPL_INDEXING, {"step": 5})

    def indexing_progress(_request: Request, service: SetupService) -> Response:
        status = service.index_status()
        pct = _progress_pct(chunks_done=status.chunks_done, chunks_total=status.chunks_total, done=status.done)
        return _render(_TPL_INDEXING_PROGRESS, {"status": status, "pct": pct}, headers=_progress_headers(status))

    def tour_screen(_request: Request, _service_: SetupService) -> Response:
        return _render(_TPL_TOUR, {"step": 6})

    def first_search_redirect(_request: Request, _service_: SetupService) -> Response:
        # The capability tour (#490) absorbed the first-search step.
        return RedirectResponse(_TOUR_URL, status_code=303)

    def search(fields: dict[str, str], service: SetupService) -> Response:
        query = fields.get("query", "")
        preview = service.first_search(query)
        return _render(_TPL_SEARCH_RESULTS, {"preview": preview, "query": query})

    def tour_prep(fields: dict[str, str], service: SetupService) -> Response:
        return _render(_TPL_TOUR_PREP, {"result": service.tour_prep(fields.get("query", ""))})

    def tour_remember(fields: dict[str, str], service: SetupService) -> Response:
        return _render(_TPL_TOUR_REMEMBER, {"result": service.tour_remember_roundtrip(fields.get("content", ""))})

    def tour_brief(_fields: dict[str, str], service: SetupService) -> Response:
        return _render(_TPL_TOUR_BRIEF, {"result": service.tour_brief()})

    def tour_timeline(fields: dict[str, str], service: SetupService) -> Response:
        return _render(_TPL_TOUR_TIMELINE, {"result": service.tour_timeline(fields.get("query", ""))})

    def connect_agent_screen(request: Request, service: SetupService) -> Response:
        info = _rebased_connect_info(service.agent_connect_info(), _request_origin(request))
        return _render(_TPL_CONNECT_AGENT, {"step": 7, "info": info})

    def connect_agent_verify(_fields: dict[str, str], service: SetupService) -> Response:
        result = service.verify_agent_handshake()
        return _render(_TPL_HANDSHAKE, {"result": result})

    def done_screen(_request: Request, service: SetupService) -> Response:
        status = service.index_status()
        return _render(_TPL_DONE, {"status": status, "config_file": service.config_file_path()})

    def source_screen(_request: Request, service: SetupService) -> Response:
        return _render(_TPL_SOURCE, {"step": 4, "options": service.source_options()})

    def source_connect_screen(request: Request, service: SetupService) -> Response:
        return _handle_source_connect_screen(request, service, _render)

    def source_connect_start(request: Request, form: Any, service: SetupService) -> Response:
        return _handle_source_connect_start(request, _string_fields(form), service, _render)

    def source_wait_screen(request: Request, _service_: SetupService) -> Response:
        provider = request.query_params.get(_FIELD_PROVIDER, "")
        if not provider:
            # No provider context — back to the source cards (review
            # L7, the /setup/key convention).
            return RedirectResponse(_SOURCE_URL, status_code=303)
        return _render(_TPL_SOURCE_WAIT, {"step": 4, "provider": provider})

    def source_auth_status(_request: Request, service: SetupService) -> Response:
        status = service.source_auth_status()
        return _render(_TPL_SOURCE_AUTH_STATUS, {"status": status}, headers=_source_status_headers(status))

    def source_picker_screen(request: Request, service: SetupService) -> Response:
        provider = request.query_params.get(_FIELD_PROVIDER, "")
        if not provider:
            # No provider context — back to the source cards (review
            # L7, the /setup/key convention).
            return RedirectResponse(_SOURCE_URL, status_code=303)
        units = service.discover_source_units(provider)
        return _render(_TPL_SOURCE_PICKER, {"step": 4, "units": units, "provider": provider})

    def source_save(_request: Request, form: Any, service: SetupService) -> Response:
        provider = str(form.get(_FIELD_PROVIDER) or "")
        instance = str(form.get("instance") or "")
        picks = tuple(str(value) for value in form.getlist("unit"))
        return _handle_source_save(provider, instance, picks, service, _render)

    def oauth_callback(request: Request, service: SetupService) -> Response:
        return _handle_oauth_callback(request, service)

    routes: list[BaseRoute] = [
        Route("/", _endpoint(welcome), methods=["GET"]),
        Route("/provider", _endpoint(provider_screen), methods=["GET"]),
        Route("/key", _endpoint(key_screen), methods=["GET"]),
        Route("/key", _form_endpoint(key_save), methods=["POST"]),
        Route("/key/validate", _form_endpoint(key_validate), methods=["POST"]),
        Route("/folder", _endpoint(folder_screen), methods=["GET"]),
        Route("/folder", _form_endpoint(folder_save), methods=["POST"]),
        Route("/folder/scan", _form_endpoint(folder_scan), methods=["POST"]),
        Route("/indexing", _endpoint(indexing_screen), methods=["GET"]),
        Route("/indexing/progress", _endpoint(indexing_progress), methods=["GET"]),
        Route("/tour", _endpoint(tour_screen), methods=["GET"]),
        Route("/tour/prep", _form_endpoint(tour_prep), methods=["POST"]),
        Route("/tour/remember", _form_endpoint(tour_remember), methods=["POST"]),
        Route("/tour/brief", _form_endpoint(tour_brief), methods=["POST"]),
        Route("/tour/timeline", _form_endpoint(tour_timeline), methods=["POST"]),
        Route("/first-search", _endpoint(first_search_redirect), methods=["GET"]),
        Route("/search", _form_endpoint(search), methods=["POST"]),
        Route("/connect-agent", _endpoint(connect_agent_screen), methods=["GET"]),
        Route("/connect-agent/verify", _form_endpoint(connect_agent_verify), methods=["POST"]),
        Route("/done", _endpoint(done_screen), methods=["GET"]),
        Route("/source", _endpoint(source_screen), methods=["GET"]),
        Route(_SOURCE_CONNECT_ROUTE, _endpoint(source_connect_screen), methods=["GET"]),
        Route(_SOURCE_CONNECT_ROUTE, _request_form_endpoint(source_connect_start), methods=["POST"]),
        Route("/source/wait", _endpoint(source_wait_screen), methods=["GET"]),
        Route("/source/auth-status", _endpoint(source_auth_status), methods=["GET"]),
        Route("/source/picker", _endpoint(source_picker_screen), methods=["GET"]),
        Route("/source/save", _request_form_endpoint(source_save), methods=["POST"]),
        Route(_OAUTH_CALLBACK_MOUNT_PATH, _endpoint(oauth_callback), methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=_static_directory()), name="setup-static"),
    ]

    from starlette.routing import Router

    resolved_secrets = secrets if secrets is not None else _default_secrets_resolver()
    # The OAuth callback is the ONLY guard-exempt path — see the guard's
    # docstring for why (provider redirects can't carry headers) and the
    # compensating nonce control.
    guarded = OperatorTokenGuard(
        Router(routes=routes),
        secrets=resolved_secrets,
        exempt_paths=frozenset({_OAUTH_CALLBACK_MOUNT_PATH}),
    )
    return Mount(SETUP_PATH_PREFIX, app=guarded, name="setup-wizard")


__all__ = [
    "OPERATOR_TOKEN_HEADER",
    "SETUP_PATH_PREFIX",
    "OperatorTokenGuard",
    "build_setup_wizard_mount",
]
