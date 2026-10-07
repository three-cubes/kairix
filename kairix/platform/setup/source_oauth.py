"""Wizard-origin OAuth machinery for the web setup wizard's source step (#489).

The decided architecture (recorded on issue #489): the consent redirect
rides the SAME origin the operator already reaches the wizard on — a
``GET /setup/oauth/callback`` route on the wizard Starlette app. No new
ports, no compose change; pip installs, local Docker, and remote VMs
through an SSH tunnel all work identically.

This module owns the pieces below the :class:`SetupService` boundary:

- :class:`WizardCallbackListener` — implements the existing
  :class:`kairix.connect.protocols.CallbackListener` Protocol. Instead
  of binding a localhost socket (the ``kairix connect`` CLI shape), it
  blocks on a :class:`threading.Event` that the wizard's callback route
  sets when the provider redirect arrives.
- :class:`CapturingBrowser` — a :class:`BrowserLauncher` that records
  the authorize URL instead of opening a server-side browser (there is
  no browser inside the container; the OPERATOR's browser is sent to
  the consent screen by the wizard's status poll).
- :func:`build_source_flow` — production flow factory: provider name +
  operator-typed fields → a constructed ``kairix.connect`` flow with
  the wizard's state-carrying authorize-URL builder injected.
- :func:`source_secret_leaves` — canonical secret names + values for
  one captured credential set (assertable by name; values never logged
  — F15).
- :func:`discover_source_units_live` — production unit discovery
  (Slack channels / GitHub repos) against the just-captured tokens.
- :func:`topology_updates_for_source` — the ``topology`` config
  entries for the picked units, merge-ready for the overlay-aware
  ``write_config_updates`` path (#485).

F15: authorization codes, tokens, client secrets, and PEM keys are
never logged or interpolated into error strings by this module.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from kairix.config import normalize_topology_key
from kairix.connect.oauth2.github_app import (
    GITHUB_INSTALLATION_ID_METADATA_KEY as GITHUB_INSTALLATION_ID_KEY,
)
from kairix.connect.protocols import (
    CallbackDeniedError,
    CallbackResult,
    CallbackTimeoutError,
    CapturedTokens,
    ClientCredentials,
)
from kairix.connect.store.leaves import leaf_pairs
from kairix.platform.setup.service import SourceOption, SourceUnit
from kairix.secrets.naming import canonical_secret_name

# Path (relative to the server root) the OAuth provider redirects to.
# Part of the wizard's operator-facing contract: the provider app
# registration must list ``<wizard-origin>/setup/oauth/callback``.
OAUTH_CALLBACK_PATH = "/setup/oauth/callback"

# Provider keys the wizard's source step can connect over OAuth. The
# three google areas share one flow class (per service-area instance).
PROVIDER_SLACK = "slack"
PROVIDER_GITHUB = "github"
PROVIDER_GOOGLE_DRIVE = "google-drive"
PROVIDER_GMAIL = "gmail"
PROVIDER_GOOGLE_CALENDAR = "google-calendar"

OAUTH_SOURCE_PROVIDERS: tuple[str, ...] = (
    PROVIDER_SLACK,
    PROVIDER_GITHUB,
    PROVIDER_GOOGLE_DRIVE,
    PROVIDER_GMAIL,
    PROVIDER_GOOGLE_CALENDAR,
)

# The google service areas (one GoogleOAuth2Flow class, three instances).
_GOOGLE_AREAS: tuple[str, ...] = (PROVIDER_GMAIL, PROVIDER_GOOGLE_DRIVE, PROVIDER_GOOGLE_CALENDAR)

# Providers whose pickers offer sub-units to choose. Google Drive has no
# folder/drive listing call in kairix yet (the Drive client is a
# changes-feed client), so Drive joins Gmail/Calendar on the
# confirm-only path; a Drive folder picker is KFEAT-022 territory.
PICKABLE_PROVIDERS: tuple[str, ...] = (PROVIDER_SLACK, PROVIDER_GITHUB)

# Google's OAuth2 authorize endpoint. Mirrors the canonical URL the
# default builder in kairix/connect/oauth2/google.py uses — pinned here
# because the wizard injects its own builder (to carry the ``state``
# nonce) and must produce the same consent screen.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# Default source cards the wizard's source step shows. ``oauth=False``
# rows route to existing screens (the folder step); oauth rows start
# the wizard-origin OAuth flow.
DEFAULT_SOURCE_OPTIONS: tuple[SourceOption, ...] = (
    SourceOption(
        key="folder",
        label="Folder",
        description="Index documents from a folder on this machine.",
        oauth=False,
    ),
    SourceOption(
        key=PROVIDER_SLACK,
        label="Slack",
        description="Index messages from the channels you pick in a Slack workspace.",
        oauth=True,
    ),
    SourceOption(
        key=PROVIDER_GITHUB,
        label="GitHub",
        description="Index code and issues from the repositories you pick.",
        oauth=True,
    ),
    SourceOption(
        key=PROVIDER_GOOGLE_DRIVE,
        label="Google Drive",
        description="Index the files this Google account can see in Drive.",
        oauth=True,
    ),
    SourceOption(
        key=PROVIDER_GMAIL,
        label="Gmail",
        description="Index email from a Google mailbox.",
        oauth=True,
    ),
    SourceOption(
        key=PROVIDER_GOOGLE_CALENDAR,
        label="Google Calendar",
        description="Index events from a Google calendar.",
        oauth=True,
    ),
)

# Shared F21 run hint for source-connect failures.
_RETRY_HINT = "next: go back to the source step and start the connection again."

#: Upper bound for pasted credential material (review L7). Real Google
#: client_secret.json files are ~1 KB and PEM keys a few KB — anything
#: past this is a paste mistake, never a credential.
SECRET_MATERIAL_MAX_BYTES = 64 * 1024

# Default F39 tier for OAuth-connected sources whose content is private
# by default (GitHub repos, Google mail/files/events). Slack overrides
# to internal because the connector itself routes per channel kind.
_SENSITIVITY_CONFIDENTIAL = "client-confidential"


class WizardCallbackListener:
    """``CallbackListener`` fulfilled by the wizard's own callback route.

    The ``kairix connect`` CLI binds a localhost socket; the wizard
    cannot (the provider redirect must reach the wizard's published
    origin, not a fresh port inside the container). So this listener
    derives ``redirect_uri`` from the origin of the request that
    STARTED the flow and blocks on an event the
    ``GET /setup/oauth/callback`` route sets via :meth:`deliver`.

    Args:
      origin: Scheme + host + port the operator's browser used to reach
        the wizard (e.g. ``http://localhost:8080``). Captured from the
        live request — never hardcoded — so localhost, SSH-tunnel, and
        reverse-proxy origins all work.
      expected_state: The single-use nonce the flow's authorize URL
        carries in its ``state`` param, or ``None`` for flows whose
        callback has no state (the GitHub App install redirect).
        Verified by the service before :meth:`deliver` is called.
    """

    def __init__(self, *, origin: str, expected_state: str | None) -> None:
        self._origin = origin.rstrip("/")
        self.expected_state = expected_state
        self._event = threading.Event()
        self._params: dict[str, str] = {}
        self._closed = False

    @property
    def redirect_uri(self) -> str:
        return f"{self._origin}{OAUTH_CALLBACK_PATH}"

    def deliver(self, params: Mapping[str, str]) -> None:
        """Hand the provider redirect's query params to the waiting flow.

        Called by the service when the callback route receives the
        provider redirect (after nonce verification). Values are held
        in memory only and never logged (F15).
        """
        self._params = dict(params)
        self._event.set()

    def wait_for_callback(self, timeout_s: float = 600.0) -> CallbackResult:
        """Block until :meth:`deliver` (or :meth:`close`) fires.

        The default timeout is longer than the CLI listener's 120s —
        the operator is walking a provider consent screen in another
        tab and may take a few minutes.
        """
        completed = self._event.wait(timeout=timeout_s)
        if self._closed:
            raise CallbackTimeoutError(
                "The source connection was cancelled before the sign-in finished. "
                f"fix: only one source connection can wait at a time. {_RETRY_HINT}",
            )
        if not completed:
            raise CallbackTimeoutError(
                f"No sign-in response arrived within {timeout_s:.0f}s. "
                f"fix: finish the provider's consent screen in the tab that opened. {_RETRY_HINT}",
            )
        params = self._params
        error = params.get("error")
        if error == "access_denied":
            raise CallbackDeniedError(
                "The sign-in was cancelled on the provider's consent screen. "
                f"fix: approve the consent screen so kairix can read this source. {_RETRY_HINT}",
            )
        if error:
            raise CallbackDeniedError(
                f"The provider reported an error during sign-in: {error}. "
                "fix: check the provider app's redirect URL matches the address the wizard showed. "
                f"{_RETRY_HINT}",
            )
        code = params.get("code") or params.get("installation_id") or ""
        if not code:
            raise CallbackDeniedError(
                "The provider's sign-in response carried no authorization code. "
                "fix: check the provider app's redirect URL matches the address the wizard showed. "
                f"{_RETRY_HINT}",
            )
        return CallbackResult(code=code, state=params.get("state"), params=dict(params))

    def close(self) -> None:
        """Cancel a pending wait. Idempotent."""
        self._closed = True
        self._event.set()


class CapturingBrowser:
    """``BrowserLauncher`` that records the authorize URL instead of opening.

    The wizard runs server-side (often in a container with no browser);
    the OPERATOR's browser must visit the consent screen. The flow's
    ``authorize`` calls ``browser.open(url)`` — this implementation
    records the URL so the wizard's status poll can redirect the
    operator's browser to it.
    """

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, url: str) -> bool:
        self.urls.append(url)
        return True

    @property
    def authorize_url(self) -> str | None:
        """The first captured authorize URL, or ``None`` before capture."""
        return self.urls[0] if self.urls else None


@dataclass(frozen=True)
class SourceFlowRequest:
    """Everything the flow factory needs to construct one provider flow.

    ``fields`` carries the operator-typed connect-form values (client
    ids, secrets, pasted JSON/PEM — F15: never log this mapping).
    ``nonce`` is the single-use ``state`` value for flows that support
    it (Slack, Google); the GitHub App install redirect carries no
    state, so its flow ignores it. ``browser`` is the capturing
    launcher whose recorded URL the status poll surfaces.
    """

    provider: str
    fields: Mapping[str, str]
    nonce: str
    browser: Any


def slack_authorize_url(
    client: ClientCredentials,
    redirect_uri: str,
    scopes: tuple[str, ...],
    *,
    state: str,
) -> str:
    """Slack OAuth v2 authorize URL carrying the wizard's ``state`` nonce.

    Mirrors the default builder in ``kairix/connect/oauth2/slack.py``
    (same params, same endpoint) plus the ``state`` param the wizard
    uses for pending-flow correlation. Injected through the flow's
    ``authorize_url_builder`` seam.
    """
    from kairix.connect.oauth2.slack import SLACK_AUTHORIZE_URL

    params = {
        "client_id": client.client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "user_scope": "",
        "state": state,
    }
    return f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}"


def google_authorize_url(
    client: ClientCredentials,
    redirect_uri: str,
    scopes: tuple[str, ...],
    *,
    state: str,
) -> str:
    """Google OAuth2 authorize URL carrying the wizard's ``state`` nonce.

    Mirrors the default builder in ``kairix/connect/oauth2/google.py``
    — including ``access_type=offline`` + ``prompt=consent`` so Google
    grants a refresh token — plus the ``state`` param.
    """
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def write_secret_material(text: str, *, suffix: str) -> Path:
    """Write pasted secret material (client_secret.json / PEM) to a 0600 file.

    The connect flows read credential material from disk paths
    (``client_secret_path`` / ``private_key_path``); operators driving
    the wizard from a browser paste the content instead. ``mkstemp``
    creates the file 0600 so no other local user can read it. The
    content is never logged (F15).

    Pastes past :data:`SECRET_MATERIAL_MAX_BYTES` are rejected with a
    ``ValueError`` (review L7): real client secrets are ~1 KB and PEM
    keys a few KB, so an oversized paste is a mistake, not a
    credential — and it must not land on disk.
    """
    if len(text.encode("utf-8")) > SECRET_MATERIAL_MAX_BYTES:
        raise ValueError(
            "The pasted credential is too large to be a client secret or key (over 64 KB)."
            f" fix: paste only the credential file's content. {_RETRY_HINT}"
        )
    fd, raw_path = tempfile.mkstemp(prefix="kairix-wizard-", suffix=suffix)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return Path(raw_path)


def _credential_material_path(
    fields: Mapping[str, str],
    *,
    pasted_key: str,
    path_key: str,
    suffix: str,
    what: str,
) -> Path:
    """Resolve pasted-or-path credential material to a readable file path."""
    pasted = (fields.get(pasted_key) or "").strip()
    if pasted:
        return write_secret_material(pasted, suffix=suffix)
    declared = (fields.get(path_key) or "").strip()
    if declared:
        return Path(declared).expanduser()
    raise ValueError(
        f"The {what} is required. "
        f"fix: paste the {what} content into the form, or enter a server path to it. "
        f"{_RETRY_HINT}",
    )


def _build_slack_flow(request: SourceFlowRequest) -> Any:
    from kairix.connect.oauth2.slack import SlackOAuth2Flow

    nonce = request.nonce
    return SlackOAuth2Flow(
        workspace=(request.fields.get("workspace") or "").strip(),
        client_id=(request.fields.get("client_id") or "").strip(),
        client_secret=(request.fields.get("client_secret") or "").strip(),
        browser=request.browser,
        authorize_url_builder=lambda client, redirect_uri, scopes: slack_authorize_url(
            client, redirect_uri, scopes, state=nonce
        ),
    )


def _build_google_flow(request: SourceFlowRequest) -> Any:
    from kairix.connect.oauth2.google import GoogleOAuth2Flow

    nonce = request.nonce
    secret_path = _credential_material_path(
        request.fields,
        pasted_key="client_secret_json",
        path_key="client_secret_path",
        suffix=".json",
        what="Google client_secret.json",
    )
    return GoogleOAuth2Flow(
        service_area=request.provider,
        client_secret_path=secret_path,
        browser=request.browser,
        authorize_url_builder=lambda client, redirect_uri, scopes: google_authorize_url(
            client, redirect_uri, scopes, state=nonce
        ),
    )


def _build_github_flow(request: SourceFlowRequest) -> Any:
    from kairix.connect.oauth2.github_app import GitHubAppFlow

    key_path = _credential_material_path(
        request.fields,
        pasted_key="private_key_pem",
        path_key="private_key_path",
        suffix=".pem",
        what="GitHub App private key (.pem)",
    )
    return GitHubAppFlow(
        app_id=(request.fields.get("app_id") or "").strip(),
        private_key_path=key_path,
        app_slug=(request.fields.get("app_slug") or "").strip() or "kairix-bot",
        browser=request.browser,
        # No authorize_url_builder: the GitHub App install redirect
        # carries no ``state`` param — single-slot correlation covers it
        # (the service accepts a state-less callback only for a pending
        # GitHub flow).
    )


def build_source_flow(request: SourceFlowRequest) -> Any:
    """Production flow factory — provider key + typed fields → connect flow.

    Raises :class:`ValueError` with F21 guidance for unknown providers
    or missing credential material; the service surfaces the message on
    the connect form instead of starting a doomed background thread.
    """
    if request.provider == PROVIDER_SLACK:
        return _build_slack_flow(request)
    if request.provider == PROVIDER_GITHUB:
        return _build_github_flow(request)
    if request.provider in _GOOGLE_AREAS:
        return _build_google_flow(request)
    known = ", ".join(OAUTH_SOURCE_PROVIDERS)
    raise ValueError(
        f"Unknown source provider {request.provider!r}. fix: pick one of {known}. {_RETRY_HINT}",
    )


def source_secret_leaves(
    provider: str,
    instance: str | None,
    client: ClientCredentials,
    tokens: CapturedTokens,
) -> tuple[tuple[str, str], ...]:
    """Canonical ``(secret-name, value)`` pairs for one captured credential set.

    Names follow ADR-031 (``kairix-connector-<area>[-<instance>]-<leaf>``).
    Every provider derives leaves through the shared
    :func:`kairix.connect.store.leaves.leaf_pairs` walk — the same
    derivation the ``kairix connect`` CLI stores use, so the wizard and
    the CLI write identical name sets by construction. GitHub's
    repurposed ``client_id`` / ``client_secret`` slots are remapped to
    ``app-id`` / ``app-private-key`` by ``SERVICE_LEAF_OVERRIDES``
    inside ``leaf_pairs`` (the names the connector's credential
    resolver reads).

    F15: callers persist these values through the secrets store; this
    function (and its tests) only ever assert the NAMES.
    """
    pairs = leaf_pairs(client, tokens, service_area=provider)
    return tuple((canonical_secret_name("connector", provider, instance or None, leaf), value) for leaf, value in pairs)


def _default_slack_client(bot_token: str) -> Any:
    from kairix.connectors.slack.web_client import SlackWebClient

    return SlackWebClient(token=bot_token)


def _default_github_client(
    client: ClientCredentials,
    tokens: CapturedTokens,
) -> Any:
    from kairix.connect.refresh import GitHubAppRefreshableToken
    from kairix.connectors.github.api_client import GitHubApiClient

    refreshable = GitHubAppRefreshableToken(
        app_id=client.client_id,
        private_key_pem=client.client_secret,
        installation_id=tokens.metadata.get(GITHUB_INSTALLATION_ID_KEY, ""),
    )
    return GitHubApiClient(refreshable_token=refreshable)


def _slack_units(web: Any) -> tuple[SourceUnit, ...]:
    """Map Slack conversations to picker rows (archived channels skipped)."""
    units: list[SourceUnit] = []
    for channel in web.conversations_list(types=("public_channel", "private_channel")):
        if channel.is_archived:
            continue
        is_public = channel.kind == "public_channel"
        detail = "public channel" if is_public else "private channel"
        if not channel.is_member:
            detail += " — invite the app to this channel so its messages can be read"
        units.append(SourceUnit(unit_id=channel.channel_id, name=f"#{channel.name}", detail=detail))
    return tuple(units)


def _github_units(api: Any) -> tuple[SourceUnit, ...]:
    """Map installation-visible repos to picker rows."""
    units: list[SourceUnit] = []
    for repo in api.list_installation_repositories():
        detail = f"{repo.visibility} — default branch {repo.default_branch}"
        if repo.archived:
            detail += " — archived"
        units.append(SourceUnit(unit_id=repo.full_name, name=repo.full_name, detail=detail))
    return tuple(units)


def discover_source_units_live(
    provider: str,
    client: ClientCredentials,
    tokens: CapturedTokens,
    *,
    slack_client_factory: Callable[[str], Any] = _default_slack_client,
    github_client_factory: Callable[[ClientCredentials, CapturedTokens], Any] = _default_github_client,
) -> tuple[SourceUnit, ...]:
    """Enumerate pickable units against the just-captured credentials.

    Slack → channels via ``conversations_list``; GitHub → repos via
    ``GET /installation/repositories``. The Google areas return an
    empty tuple — they take the confirm-only path (no sub-unit listing
    surface exists for them yet; see :data:`PICKABLE_PROVIDERS`).

    The client factories are F6-clean seams with real production
    defaults; tests pass recording fakes so no HTTP egress happens.
    """
    if provider == PROVIDER_SLACK:
        return _slack_units(slack_client_factory(tokens.bot_token))
    if provider == PROVIDER_GITHUB:
        return _github_units(github_client_factory(client, tokens))
    return ()


# ---------------------------------------------------------------------------
# topology config emission
# ---------------------------------------------------------------------------

# Block names within topology keyed by "id" (collections key on "name").
_ID_KEYED_BLOCKS = ("connectors", "credentials", "cc_pairs")


def _collection_source(pair_id: str, path_filter: str) -> dict[str, str]:
    """One (cc_pair, path_filter) source row inside a collection."""
    return {"cc_pair": pair_id, "path_filter": path_filter}


def _topology_entry_set(
    *,
    base_id: str,
    kind: str,
    display_name: str,
    specific: Mapping[str, Any],
    secret_name: str,
    credential_kind: str,
    pair_name: str,
    collection_name: str,
    sources: list[dict[str, str]],
    sensitivity: str,
) -> dict[str, dict[str, Any]]:
    """One connector + credential + cc_pair + collection entry quartet.

    Single assembly site so every block's field names appear exactly
    once (F17) and all four shapes stay consistent across providers.
    Cross-block ids derive from ``base_id`` (``<base>-conn`` /
    ``<base>-credential`` / ``<base>-pair``) — callers building
    per-unit collection sources use the same ``<base>-pair`` id.
    """
    conn_id = f"{base_id}-conn"
    credential_id = f"{base_id}-credential"
    return {
        "connectors": {
            "id": conn_id,
            "kind": kind,
            "name": display_name,
            "default_sensitivity": sensitivity,
            "connector_specific_config": dict(specific),
        },
        "credentials": {
            "id": credential_id,
            "kind": credential_kind,
            "secret_name": secret_name,
            "admin_public": False,
        },
        "cc_pairs": {
            "id": f"{base_id}-pair",
            "connector": conn_id,
            "credential": credential_id,
            "name": pair_name,
            "access_type": "PRIVATE",
        },
        "collections": {
            "name": collection_name,
            "sources": sources,
        },
    }


def _slack_topology(instance: str, picks: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    base_id = f"slack-{instance}"
    # Slack chunk paths are slack://channel/<id>/p<ts> deep links, so
    # one path_filter per picked channel scopes the collection to
    # exactly the picked channels.
    sources = [_collection_source(f"{base_id}-pair", f"slack://channel/{channel_id}/*") for channel_id in picks]
    return _topology_entry_set(
        base_id=base_id,
        kind="slack",
        display_name=f"Slack workspace {instance}",
        specific={"workspace": instance},
        secret_name=f"connector-slack-{instance}",
        credential_kind="bearer_token",
        pair_name=base_id,
        collection_name=base_id,
        sources=sources,
        sensitivity="internal",
    )


def _github_topology(picks: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return _topology_entry_set(
        base_id="github-app",
        kind="github",
        display_name="GitHub repositories",
        # repos_allowlist is the connector's own selection key — only
        # the picked owner/repo slugs drain.
        specific={"repos_allowlist": list(picks)},
        secret_name="connector-github",  # noqa: S106 — logical secret NAME for the resolver, not a value  # pragma: allowlist secret
        credential_kind="github_dual_path",
        pair_name="github-app",
        collection_name="github-repos",
        sources=[_collection_source("github-app-pair", "*")],
        sensitivity=_SENSITIVITY_CONFIDENTIAL,
    )


def _google_topology(provider: str, instance: str) -> dict[str, dict[str, Any]]:
    specific: dict[str, Any]
    if provider == PROVIDER_GMAIL:
        specific = {"user_email": instance}
    elif provider == PROVIDER_GOOGLE_CALENDAR:
        specific = {"calendar_id": instance or "primary"}
    else:
        specific = {"corpora": [instance or "my-drive"]}
    return _topology_entry_set(
        base_id=provider,
        kind=provider.replace("-", "_"),  # google-drive → google_drive plugin dir
        display_name=provider.replace("-", " ").title(),
        specific=specific,
        secret_name=f"connector-{provider}",
        credential_kind="oauth2_refresh_token",
        pair_name=f"{provider}-default",
        collection_name=f"{provider}-all",
        sources=[_collection_source(f"{provider}-pair", "*")],
        sensitivity=_SENSITIVITY_CONFIDENTIAL,
    )


def _source_topology_entries(provider: str, instance: str, picks: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """One entry per topology block for the picked source."""
    if provider == PROVIDER_SLACK:
        return _slack_topology(instance, picks)
    if provider == PROVIDER_GITHUB:
        return _github_topology(picks)
    return _google_topology(provider, instance)


def topology_updates_for_source(
    provider: str,
    instance: str,
    picks: tuple[str, ...],
    existing: Mapping[str, Any],
) -> dict[str, Any]:
    """The full ``topology`` block with this source's entries upserted.

    ``existing`` is the current content of the wizard's write-target
    config file (overlay-aware — see ``write_config_updates``). Entries
    are upserted by ``id`` (collections by ``name``) so re-saving the
    same source replaces its rows instead of duplicating them, and a
    second source (connect Slack, then GitHub) appends without
    clobbering the first.

    A config file written before the rename that still carries the legacy topology key is
    normalized to ``topology`` on read via :func:`normalize_topology_key`
    (PLA-287) so re-running the wizard upserts onto the operator's existing
    sources instead of orphaning them under the old key; the wizard always
    writes the canonical ``topology`` key going forward.
    """
    normalized = normalize_topology_key(existing) if isinstance(existing, Mapping) else {}
    raw_topology = normalized.get("topology")
    topology: dict[str, Any] = {}
    if isinstance(raw_topology, Mapping):
        topology = {key: list(value) if isinstance(value, list) else value for key, value in raw_topology.items()}
    entries = _source_topology_entries(provider, instance, picks)
    for block, item in entries.items():
        key = "id" if block in _ID_KEYED_BLOCKS else "name"
        rows = [row for row in topology.get(block, []) if isinstance(row, dict) and row.get(key) != item[key]]
        rows.append(item)
        topology[block] = rows
    return {"topology": topology}


__all__ = [
    "DEFAULT_SOURCE_OPTIONS",
    "GITHUB_INSTALLATION_ID_KEY",
    "GOOGLE_AUTHORIZE_URL",
    "OAUTH_CALLBACK_PATH",
    "OAUTH_SOURCE_PROVIDERS",
    "PICKABLE_PROVIDERS",
    "PROVIDER_GITHUB",
    "PROVIDER_GMAIL",
    "PROVIDER_GOOGLE_CALENDAR",
    "PROVIDER_GOOGLE_DRIVE",
    "PROVIDER_SLACK",
    "SECRET_MATERIAL_MAX_BYTES",
    "CapturingBrowser",
    "SourceFlowRequest",
    "WizardCallbackListener",
    "build_source_flow",
    "discover_source_units_live",
    "google_authorize_url",
    "slack_authorize_url",
    "source_secret_leaves",
    "topology_updates_for_source",
    "write_secret_material",
]
