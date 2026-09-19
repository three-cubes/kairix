"""Thin Microsoft Graph client for header-only message retrieval.

A focused wrapper around ``httpx.Client`` for the Microsoft Graph
folder-scoped ``/users/{upn}/mailFolders/{folder_id}/messages/delta``
endpoint. Four commitments:

  1. **Folder-scoped delta** (#380). Graph rejects mailbox-wide delta
     with ``BadRequest: Change tracking is not supported against
     'microsoft.graph.message'`` — delta only works folder-scoped. The
     client builds URLs of the shape
     ``/users/{upn}/mailFolders/{folder_id}/messages/delta`` and
     exposes :meth:`list_mail_folders` so the connector can enumerate
     the folders to drain.

  2. **Header-only retrieval** (ADR-004). Every Graph request carries
     ``$select=from,toRecipients,ccRecipients,subject,sentDateTime,
     receivedDateTime,id``. Body fields (``body``, ``uniqueBody``,
     ``bodyPreview``) are never requested. Tests pin the ``$select``
     string at the query-construction surface.

  3. **OAuth2 client-credentials auth.** Every request adds an
     ``Authorization: Bearer <token>`` header via the injected
     :class:`OAuth2ClientCredsAuth` helper. A 401 triggers a single
     :meth:`invalidate` + retry; persistent 401 propagates.

  4. **Delta-token pagination.** The connector hands an opaque cursor
     between ticks; the Graph response carries either ``@odata.nextLink``
     (more pages now) or ``@odata.deltaLink`` (resume here next tick).
     The client surfaces both as :class:`DeltaPage` so the connector
     can advance cursors without parsing URLs itself. Each folder
     parks its own deltaLink — the connector persists a
     ``{folder_id: deltaLink}`` mapping.

Per F37, ``msgraph_core`` / ``msgraph`` import is allowed only under
``kairix/connectors/<name>/`` — but we deliberately avoid the SDK
(see ADR rationale in the spec brief: the SDK pulls a heavy transitive
set; the delta query is a straightforward REST call). The client uses
raw ``httpx`` and stays under F37's allowed surface (this module lives
at ``kairix/connectors/m365_email_headers/graph_client.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from kairix.transport.auth.oauth2_client_creds import OAuth2ClientCredsAuth
from kairix.transport.errors import raise_for_graph_status

logger = logging.getLogger(__name__)

# Per ADR-004 (Email — Headers Only): the Graph projection that ensures
# we NEVER fetch any body field. Add new fields only if they are
# explicitly envelope (header) data — never body / preview / unique
# body. The constant is exported so tests can pin the projection at
# the assertion layer (the body-content-not-fetched scenario).
HEADER_ONLY_SELECT: Final[str] = "id,from,toRecipients,ccRecipients,subject,sentDateTime,receivedDateTime"

# Default base URL — overrideable for sovereign clouds (e.g. Graph for
# US Government / 21Vianet).
_DEFAULT_GRAPH_BASE: Final[str] = "https://graph.microsoft.com/v1.0"

# Per-request timeout. Graph delta replies typically arrive in <1s;
# 60s covers a cold connection on a paginated reply with a large
# mailbox.
_GRAPH_REQUEST_TIMEOUT_S: Final[float] = 60.0

# Retry tuning for Graph throttling (GH #357). Mirrors the SharePoint
# Graph client's strategy: 429 + 503 honour ``Retry-After`` when present;
# other transient 5xx fall back to bounded exponential backoff. Graph
# documents 429 + 503 as the throttled responses; both carry
# ``Retry-After`` per https://learn.microsoft.com/graph/throttling.
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_BACKOFF_MIN_S: Final[float] = 2.0
_DEFAULT_BACKOFF_MAX_S: Final[float] = 60.0
_RETRY_AFTER_HEADER: Final[str] = "Retry-After"
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_THROTTLED_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 503})


@dataclass(frozen=True)
class GraphMessage:
    """One header-only message envelope as projected from Microsoft Graph.

    Body fields are intentionally absent — the dataclass shape itself
    encodes the ADR-004 constraint. A future contributor adding a
    ``body`` field would break the test that pins the dataclass field
    set, which is the mechanical guard for the no-body-content
    invariant.
    """

    message_id: str
    sender: str | None
    to_recipients: tuple[str, ...]
    cc_recipients: tuple[str, ...]
    subject: str | None
    sent_at: str | None
    received_at: str | None


@dataclass(frozen=True)
class DeltaPage:
    """One page of the ``/messages/delta`` response.

    ``next_link`` is non-``None`` when more pages remain for the
    current sync window; the caller follows it before advancing the
    cursor. ``delta_link`` is non-``None`` on the *final* page —
    that's the opaque token the caller persists as the connector
    cursor for the next worker tick.
    """

    messages: tuple[GraphMessage, ...]
    next_link: str | None
    delta_link: str | None


@dataclass(frozen=True)
class MailFolderRef:
    """One mail folder reference projected from ``GET /users/{upn}/mailFolders``.

    The Graph mailFolders response yields one of these per folder; the
    connector keys per-folder delta cursors by :attr:`folder_id` and
    matches operator-supplied allowlist entries against
    :attr:`well_known_name` (case-insensitive, e.g. ``"inbox"``) and
    :attr:`display_name` (case-insensitive, for custom folders).

    ``well_known_name`` is non-``None`` only for the documented
    Microsoft well-known folder set: ``inbox``, ``sentitems``,
    ``drafts``, ``deleteditems``, ``junkemail``, ``outbox``,
    ``archive``. Custom (user-created) folders surface it as ``None``.
    """

    folder_id: str
    display_name: str
    well_known_name: str | None


class M365GraphClient:
    """Thin Microsoft Graph wrapper for header-only delta queries.

    Args:
        user_principal_name: The mailbox to sync — typically the
            target user's UPN (``alice@contoso.com``). App-only auth
            requires the AAD app to hold the ``Mail.Read`` application
            permission scoped to the target mailbox.
        auth: An initialised :class:`OAuth2ClientCredsAuth` for the
            tenant the mailbox lives in. The client holds a reference
            and re-uses it for every request.
        graph_base: Optional override for sovereign clouds. Defaults
            to the public Microsoft Graph endpoint.
        http_client: Optional ``httpx.Client`` for the request path.
            Tests pass an :class:`httpx.MockTransport`-backed client
            so no real Graph call leaks from the test suite.
        sleep_fn: Optional sleep shim used by the throttling-retry loop.
            Defaults to :func:`time.sleep`; tests pass a recording no-op
            so the suite stays fast without monkey-patching stdlib.
        max_attempts: Total attempt count (initial call + retries) for
            any single Graph request. Defaults to
            :data:`_DEFAULT_MAX_ATTEMPTS` (3) per GH #357.
    """

    def __init__(
        self,
        *,
        user_principal_name: str,
        auth: OAuth2ClientCredsAuth,
        graph_base: str | None = None,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if not user_principal_name:
            raise ValueError(
                "M365GraphClient: user_principal_name is empty. "
                "fix: pass the target mailbox UPN (e.g. alice@contoso.com). "
                "next: see docs/architecture/connector-ingestion-architecture.md §8 "
                "for the M365 connector config shape."
            )
        self._upn = user_principal_name
        self._auth = auth
        self._graph_base = (graph_base or _DEFAULT_GRAPH_BASE).rstrip("/")
        self._http_client = http_client
        self._sleep_fn = sleep_fn
        self._max_attempts = max_attempts

    def initial_delta_url(self, folder_id: str) -> str:
        """Compose the seed folder-scoped delta URL with the header-only projection.

        Graph rejects mailbox-wide ``/users/{upn}/messages/delta`` with
        ``BadRequest: Change tracking is not supported against
        'microsoft.graph.message'`` (#380) — delta only works folder-
        scoped. The URL shape is
        ``/users/{upn}/mailFolders/{folder_id}/messages/delta``.

        The first sync for a folder (no cursor) starts here; subsequent
        syncs hand the previous response's ``deltaLink`` directly to
        :meth:`fetch_page`. Exposed publicly so tests can pin the URL
        shape + the ``$select`` projection without driving a real HTTP
        call.
        """
        if not folder_id:
            raise ValueError(
                "M365GraphClient.initial_delta_url: folder_id is empty. "
                "fix: pass the Graph mailFolder id "
                "(e.g. the 'inbox' well-known name or a folder id from list_mail_folders). "
                "next: see kairix/connectors/m365_email_headers/graph_client.py "
                "docstring for the folder-scoped delta URL shape."
            )
        return (
            f"{self._graph_base}/users/{self._upn}/mailFolders/{folder_id}/messages/delta?$select={HEADER_ONLY_SELECT}"
        )

    def list_mail_folders(self) -> tuple[MailFolderRef, ...]:
        """Enumerate the mailbox's mail folders via ``GET /users/{upn}/mailFolders``.

        Returns one :class:`MailFolderRef` per folder. The connector
        calls this once at cold-start so each folder gets its own
        per-folder delta cursor (#380).

        Graph's mailFolders response is paginated via ``@odata.nextLink``;
        this method follows nextLink to drain every page so the connector
        sees the full folder set in one call.
        """
        url: str | None = f"{self._graph_base}/users/{self._upn}/mailFolders"
        folders: list[MailFolderRef] = []
        while url is not None:
            response = self._authorised_get(url)
            body = response.json()
            folders.extend(_parse_mail_folders(body))
            next_link = body.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else None
        return tuple(folders)

    def fetch_page(self, url: str) -> DeltaPage:
        """Fetch one page from the given Graph URL (delta or nextLink).

        Args:
            url: The full Graph URL — either the seed
                :meth:`initial_delta_url`, a previous response's
                ``@odata.nextLink`` (more pages this run), or a stored
                ``@odata.deltaLink`` cursor (next sync tick).

        Returns:
            A :class:`DeltaPage` carrying parsed header-only messages
            and the next-link / delta-link pointers for the caller's
            pagination loop.

        Raises:
            httpx.HTTPError: On non-2xx response after the single
                401-driven token refresh.
        """
        response = self._authorised_get(url)
        body = response.json()
        return _parse_delta_page(body)

    def iter_messages(
        self,
        folder_id: str,
        start_url: str | None = None,
    ) -> Iterator[GraphMessage]:
        """Iterate header-only messages for one folder across all pages
        until the delta-link is reached.

        Args:
            folder_id: The Graph mailFolder id (well-known name like
                ``"inbox"`` or a server-assigned id). Required because
                Graph rejects mailbox-wide delta (#380).
            start_url: Optional starting URL. ``None`` starts from
                :meth:`initial_delta_url` (full sync for this folder);
                a stored deltaLink starts from the previous cursor.

        Yields:
            One :class:`GraphMessage` per Graph response entry. The
            final page's ``deltaLink`` is accessible via
            :meth:`last_delta_link` after iteration completes.
        """
        url: str | None = start_url or self.initial_delta_url(folder_id)
        self._last_delta: str | None = None
        while url is not None:
            page = self.fetch_page(url)
            yield from page.messages
            self._last_delta = page.delta_link
            url = page.next_link

    def last_delta_link(self) -> str | None:
        """Return the deltaLink from the most recent terminal page.

        Returns ``None`` before any iteration completes. The connector
        persists this string as the cursor advanced past the items it
        consumed this tick.
        """
        return getattr(self, "_last_delta", None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _authorised_get(self, url: str) -> httpx.Response:
        """Issue a GET with the current bearer + retry-with-backoff on throttle.

        Two layered retry behaviours, both intentionally narrow
        (GH #357 — mirrors :class:`SharePointGraphClient._authorised_get`):

          1. **401 once.** A single ``401 Unauthorized`` invalidates the
             cached token and retries the request once with a freshly
             exchanged bearer. A second 401 raises — the credential is
             genuinely bad and no amount of waiting will help.
          2. **429 / 5xx with backoff.** Throttled (429) and Service
             Unavailable (503) responses honour the server's
             ``Retry-After`` header; other 5xx (500, 502, 504) fall back
             to exponential backoff. ``_DEFAULT_MAX_ATTEMPTS`` total
             attempts (initial call + retries). After exhaustion the
             final response is returned to ``raise_for_status`` which
             converts it to :class:`httpx.HTTPStatusError`.

        Other 4xx responses (e.g. 403 Forbidden, 404 Not Found) raise
        immediately — they're permanent for this URL + credential pair.
        """
        retrying = Retrying(
            retry=retry_if_result(_is_retryable_response),
            wait=self._wait_strategy,
            stop=stop_after_attempt(self._max_attempts),
            sleep=self._sleep_fn,
            reraise=True,
        )
        try:
            response = retrying(self._authorised_get_once, url)
        except RetryError as exc:
            # ``retry_if_result`` returns a "successful" outcome from
            # tenacity's perspective, so ``reraise=True`` can't lift an
            # exception. On stop-condition exhaustion tenacity wraps the
            # final attempt's result in :class:`RetryError`; lift the
            # underlying response and convert it via ``raise_for_status``
            # so callers see the same :class:`httpx.HTTPStatusError`
            # shape they did before retry was added.
            final: httpx.Response = exc.last_attempt.result()
            raise_for_graph_status(final)
            return final  # pragma: no cover — raise_for_status above always raises here
        raise_for_graph_status(response)
        return response

    def _authorised_get_once(self, url: str) -> httpx.Response:
        """One bearer-authorised GET with the single 401 refresh step.

        Returns the raw :class:`httpx.Response` (never raises on status
        alone); the retry loop in :meth:`_authorised_get` inspects the
        status code via :func:`_is_retryable_response` and either retries
        or hands the response back to the caller for ``raise_for_status``.
        """
        token = self._auth.get_token()
        response = self._do_get(url, token)
        if response.status_code == 401:
            logger.info("m365 graph: received 401; invalidating token cache and retrying once")
            self._auth.invalidate()
            token = self._auth.get_token()
            response = self._do_get(url, token)
        return response

    def _wait_strategy(self, retry_state: RetryCallState) -> float:
        """Compute the wait between retries (GH #357).

        For 429 / 503 responses honour the server's ``Retry-After`` header
        (seconds OR HTTP-date). For other retryable statuses (or when
        ``Retry-After`` is missing / unparseable) fall back to exponential
        backoff between :data:`_DEFAULT_BACKOFF_MIN_S` and
        :data:`_DEFAULT_BACKOFF_MAX_S`.
        """
        outcome = retry_state.outcome
        if outcome is None or outcome.failed:  # pragma: no cover — exception path bypasses retry_if_result
            return _DEFAULT_BACKOFF_MIN_S
        response = outcome.result()
        retry_after = _parse_retry_after(response) if response.status_code in _THROTTLED_STATUS_CODES else None
        if retry_after is not None:
            logger.warning(
                "m365 graph: %s on attempt %d; honouring Retry-After=%.1fs",
                response.status_code,
                retry_state.attempt_number,
                retry_after,
            )
            return retry_after
        backoff = wait_exponential(multiplier=1, min=_DEFAULT_BACKOFF_MIN_S, max=_DEFAULT_BACKOFF_MAX_S)(retry_state)
        logger.warning(
            "m365 graph: %s on attempt %d; backing off %.1fs",
            response.status_code,
            retry_state.attempt_number,
            backoff,
        )
        return backoff

    def _do_get(self, url: str, token: str) -> httpx.Response:
        """Single HTTP GET — separated for the 401-retry path's
        symmetry. The bearer string is composed into the Authorization
        header here ONLY; never logged, never returned.
        """
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        client = self._http_client
        if client is not None:
            return client.get(url, headers=headers, timeout=_GRAPH_REQUEST_TIMEOUT_S)
        with httpx.Client(timeout=_GRAPH_REQUEST_TIMEOUT_S) as owned:
            return owned.get(url, headers=headers)


def _is_retryable_response(response: httpx.Response) -> bool:
    """``True`` when ``response.status_code`` is in
    :data:`_RETRYABLE_STATUS_CODES` (429 + 5xx subset).

    Used by the :class:`Retrying` loop in
    :meth:`M365GraphClient._authorised_get` to decide whether the
    request gets retried (with the wait dictated by
    :meth:`M365GraphClient._wait_strategy`) or returned to the
    caller for ``raise_for_status``.
    """
    return response.status_code in _RETRYABLE_STATUS_CODES


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Return the ``Retry-After`` header (seconds) as a float, or ``None``.

    Graph emits ``Retry-After`` as an integer second count per
    https://learn.microsoft.com/graph/throttling. The HTTP spec also
    allows an HTTP-date form; this client only parses the seconds form
    and falls back to exponential backoff for anything unparseable.
    """
    raw = response.headers.get(_RETRY_AFTER_HEADER)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_mail_folders(body: dict[str, Any]) -> list[MailFolderRef]:
    """Parse one Graph ``mailFolders`` JSON response page.

    The Graph ``mailFolders`` payload has the shape::

        {
          "value": [
            {"id": "AAMk...", "displayName": "Inbox",
             "wellKnownName": "inbox"},
            {"id": "AAMk...", "displayName": "Q2 Receipts",
             "wellKnownName": null},
            ...
          ],
          "@odata.nextLink": "..." | absent
        }

    Per the Graph docs (https://learn.microsoft.com/graph/api/resources/mailfolder),
    ``wellKnownName`` is non-``None`` only for the documented well-known
    folder set: ``inbox``, ``sentitems``, ``drafts``, ``deleteditems``,
    ``junkemail``, ``outbox``, ``archive``. User-created folders carry
    ``wellKnownName: null``.

    Folders with missing ``id`` are dropped — the id is the URL key,
    so an empty id can't be drained anyway.
    """
    raw_folders = body.get("value")
    folders: list[MailFolderRef] = []
    if not isinstance(raw_folders, list):
        return folders
    for entry in raw_folders:
        if not isinstance(entry, dict):
            continue
        folder_id = entry.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            continue
        display = entry.get("displayName")
        well_known = entry.get("wellKnownName")
        folders.append(
            MailFolderRef(
                folder_id=folder_id,
                display_name=display if isinstance(display, str) else "",
                well_known_name=well_known if isinstance(well_known, str) and well_known else None,
            )
        )
    return folders


def _parse_delta_page(body: dict[str, Any]) -> DeltaPage:
    """Parse one Graph ``messages/delta`` JSON response.

    Tolerates the documented response shape — ``value`` is the array
    of message envelopes, ``@odata.nextLink`` advances within the
    sync window, ``@odata.deltaLink`` is the next-tick cursor. Missing
    fields default to ``None`` / empty tuple so a sparse fixture
    parses cleanly.
    """
    raw_messages = body.get("value")
    messages: list[GraphMessage] = []
    if isinstance(raw_messages, list):
        for entry in raw_messages:
            if isinstance(entry, dict):
                messages.append(_parse_message(entry))
    next_link = body.get("@odata.nextLink")
    delta_link = body.get("@odata.deltaLink")
    return DeltaPage(
        messages=tuple(messages),
        next_link=next_link if isinstance(next_link, str) else None,
        delta_link=delta_link if isinstance(delta_link, str) else None,
    )


def _parse_message(entry: dict[str, Any]) -> GraphMessage:
    """Lift one Graph message envelope into the typed dataclass."""
    return GraphMessage(
        message_id=_str_or_empty(entry.get("id")),
        sender=_email_from(entry.get("from")),
        to_recipients=tuple(_emails_from(entry.get("toRecipients"))),
        cc_recipients=tuple(_emails_from(entry.get("ccRecipients"))),
        subject=_optional_str(entry.get("subject")),
        sent_at=_optional_str(entry.get("sentDateTime")),
        received_at=_optional_str(entry.get("receivedDateTime")),
    )


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _email_from(value: Any) -> str | None:
    """Pull ``emailAddress.address`` from a Graph recipient block."""
    if not isinstance(value, dict):
        return None
    inner = value.get("emailAddress")
    if not isinstance(inner, dict):
        return None
    address = inner.get("address")
    return address if isinstance(address, str) else None


def _emails_from(value: Any) -> list[str]:
    """Pull each recipient's ``emailAddress.address`` from a Graph
    ``toRecipients`` / ``ccRecipients`` list.
    """
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        addr = _email_from(entry)
        if addr is not None:
            out.append(addr)
    return out
