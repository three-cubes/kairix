"""``GmailConnector`` — SourceConnector + Wave-E capability mix-ins for Gmail.

Implements :class:`kairix.core.protocols.SourceConnector` plus the
capability mix-ins per the Onyx Gmail design pattern:

  * :class:`SourceConnector` (base) — list_changes / fetch / source_link
    / sensitivity_for / next_cursor / metadata_for
  * :class:`PollConnector` — per-mailbox container poll (Wave E shim
    until per-label routing lands)
  * :class:`CheckpointedConnector` — opaque per-batch checkpoint blob

Cursor model:

  * First sync (``cursor is None``) — call
    :meth:`GmailClient.get_profile_history_id` to seed the cursor at
    the live mailbox tip. Gmail's History API rejects ``startHistoryId``
    values older than ~7 days, so we never start a backfill from t=0;
    bulk backfill is a separate concern handled via the Messages API
    (out of scope for v1; tracked in GH #356 alongside the credential
    provisioning).

  * Subsequent ticks — pass the persisted historyId to
    :meth:`GmailClient.iter_history_message_ids`; the History endpoint
    returns only events strictly after the cursor.

``fetch`` returns the message body bytes with the chosen mime
(``text/plain``). The orchestration layer hands the bytes off to
Bronze + the extractor registry (passthrough/markitdown handle
plaintext directly).

Per F35 the module only imports from itself plus ``kairix.core.*``
(Protocol surface) and stdlib. No reach into other connectors, no
reach into the extractor layer.

Per F44, no Postgres / asyncpg / psycopg imports anywhere in this tree
— cursor state lives in the connector_cursors SQLite table managed by
``kairix.core.connectors.cursor_store``.

F15-clean: OAuth tokens never appear in ``logger.*`` / ``print`` /
``raise X(...)`` calls. Tokens flow from secret resolution into
:class:`GmailClient` via a callable; the callable is never logged.

See ``tests/bdd/features/connector_gmail.feature`` for the behaviour
spec this plugin pins.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from kairix.connectors.gmail.client import GmailClient, GmailMessage
from kairix.core.protocols import (
    ChangeEvent,
    Container,
    Cursor,
    HierarchyNode,
    RawArtefact,
    Sensitivity,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

CONNECTOR_NAME = "gmail"

# Per the Gmail spec brief: email defaults to a tighter tier than docs.
# Operators can override via config to a more permissive tier when
# their Gmail content is explicitly low-sensitivity (rare).
DEFAULT_SENSITIVITY: Sensitivity = "client-confidential"

# Valid sensitivity overrides — guards the make_connector factory.
_VALID_SENSITIVITY_TIERS: tuple[str, ...] = ("public", "internal", "client-confidential", "personal")

# F17 — extract the ``"sensitivity"`` metadata key (used in legacy +
# Wave E ChangeEvent emission + make_connector config validation) so
# the literal lives in one place.
_SENSITIVITY_METADATA_KEY = "sensitivity"

# Stable identifier for the synthetic root hierarchy node.
_HIERARCHY_ROOT_ID = "gmail"

# Gmail web inbox URL prefix — Gmail accepts the message id directly
# in the URL fragment so source_link round-trips operators back to the
# original message in the web UI.
_GMAIL_WEB_BASE = "https://mail.google.com/mail/u/0/#inbox/"

# Header names we lift onto SourceMetadata.properties — case-insensitive
# match because Gmail surfaces canonical headers like "Subject" but
# tests + servers may differ.
_HEADER_SUBJECT = "subject"
_HEADER_FROM = "from"
_HEADER_TO = "to"
_HEADER_CC = "cc"
_HEADER_BCC = "bcc"
_HEADER_DATE = "date"

# Mime hint for the fetched body — when the message body is empty (e.g.
# attachment-only message) we still emit the artefact with text/plain
# so the extractor registry has a deterministic shape.
_DEFAULT_BODY_MIME = "text/plain"


def _now_iso() -> str:
    """Return a current ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GmailCredentials:
    """Resolved OAuth credential blob for one Gmail cc_pair.

    Frozen per F42. The connector's constructor accepts this dataclass
    via the ``credentials`` kwarg in tests; production resolves through
    :func:`_resolve_credentials_from_secrets`.

    The ``access_token`` and ``refresh_token`` fields are F15-sensitive:
    they live in this dataclass only to round-trip from secret
    resolution into the client; they are never logged.
    """

    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str | None = None
    token_uri: str = "https://oauth2.googleapis.com/token"  # noqa: S105 — Google OAuth endpoint URL, not a token value


def _default_flag_reader(name: str) -> bool:
    """Production default for the topology-gmail flag check.

    Delegates to :func:`kairix.core.features.flag` so the production
    path threads through the env-var → config-overlay → registry
    resolution chain. Tests inject a different callable (typically one
    backed by :class:`tests.fakes.FakeFeatureFlagResolver`) so the
    branch under test is pinned without monkey-patching the resolver
    module (F1-clean / F2-clean).

    Lifted to a module-level helper so the connector's signature can
    carry a real callable default (F6-clean) without a per-call
    ``Optional[...] = None`` shape.
    """
    from kairix.core.features import flag as _prod_flag

    return _prod_flag(name)


def _default_client_builder(
    credentials: GmailCredentials,
    user_email: str,
) -> GmailClient:
    """Production default for constructing a per-mailbox Gmail client.

    Wires the credential blob into a token-refresher closure that talks
    to the Google OAuth2 token endpoint. The closure is captured by
    value (the credential dataclass is frozen) so each tick gets a
    fresh bearer when the access_token is None or has expired.

    F15-clean: the closure references the refresh_token / client_secret
    only inside the HTTP call body; the values never enter logs.
    """
    import httpx

    def _refresh_token() -> str:
        # If the credential already carries a live access_token (tests
        # pass a fixed token) use it directly so we never need to call
        # the OAuth endpoint.
        if credentials.access_token:
            return credentials.access_token
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                credentials.token_uri,
                data={
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "refresh_token": credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        response.raise_for_status()
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str):
            raise RuntimeError(
                "gmail: OAuth token-refresh response missing 'access_token'. "
                "fix: confirm the Google Workspace OAuth credentials grant offline_access. "
                "next: see kairix/connectors/gmail/README.md for the credential contract."
            )
        return token

    return GmailClient(user_email=user_email, token_refresher=_refresh_token)


def _resolve_credentials_from_secrets() -> GmailCredentials:
    """Resolve the OAuth credential set via :func:`kairix.secrets.get_secret`.

    Requires four secrets: ``connector-gmail-client-id``,
    ``connector-gmail-client-secret``, ``connector-gmail-refresh-token``,
    and (optionally) ``connector-gmail-access-token``. Lazy import so
    the connector module loads cleanly even when the secret backend is
    mid-bootstrap.
    """
    from kairix.secrets import get_secret

    client_id = get_secret("connector-gmail-client-id", required=True) or ""
    client_secret = get_secret("connector-gmail-client-secret", required=True) or ""
    refresh_token = get_secret("connector-gmail-refresh-token", required=True) or ""
    access_token = get_secret("connector-gmail-access-token", required=False) or None
    return GmailCredentials(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=access_token,
    )


class GmailConnector:
    """SourceConnector for one Gmail mailbox.

    Construction is cheap (no I/O, no token exchange). The first
    :meth:`list_changes` call drives a ``users.getProfile`` call when
    the cursor is None (cold start) or a ``users.history.list`` call
    against the provided ``historyId`` cursor.

    DI seams (all keyword arguments with real defaults — F6-clean):

      * ``credentials`` — :class:`GmailCredentials`. Tests pass a
        literal; production resolves via :func:`_resolve_credentials_from_secrets`.
      * ``client_builder`` — constructs the :class:`GmailClient`.
        Tests pass a builder returning a client backed by an
        :class:`httpx.MockTransport` so no real Gmail call leaks.
      * ``flag_reader`` — :func:`_default_flag_reader`. Tests inject a
        ``FakeFeatureFlagResolver().get`` callable so flag branches
        are pinned without monkey-patching.
      * ``sensitivity`` — per-mailbox F39 tier. Defaults to
        ``client-confidential`` per the Gmail spec brief (email is
        treated as more sensitive than docs by default).
    """

    name: str = CONNECTOR_NAME
    per_tick_max_items: int = 500
    # Gmail bodies + attachments can be large; gate the tick if the
    # disk has under 5 GiB free so we don't fill the Bronze store.
    disk_watermark_min_free_bytes: int | None = 5_000_000_000

    def __init__(
        self,
        user_email: str,
        *,
        credentials: GmailCredentials | None = None,
        client: GmailClient | None = None,
        client_builder: Callable[[GmailCredentials, str], GmailClient] = _default_client_builder,
        sensitivity: Sensitivity = DEFAULT_SENSITIVITY,
        flag_reader: Callable[[str], bool] = _default_flag_reader,
    ) -> None:
        if not user_email:
            raise ValueError(
                "gmail: user_email is empty. "
                "fix: set user_email in the connector config block to the authorised mailbox "
                "(e.g. agent-alpha@example.com). "
                "next: see docs/architecture/connector-ingestion-architecture.md §8."
            )
        self._user = user_email
        self._sensitivity: Sensitivity = sensitivity
        self._flag_reader = flag_reader

        # The Gmail client is the single point of contact with the Gmail
        # REST surface. Tests pass a constructed client directly via
        # ``client``; production wires through the ``client_builder``
        # which closes over the OAuth refresh contract.
        if client is not None:
            self._client = client
        else:
            resolved_credentials = credentials if credentials is not None else _resolve_credentials_from_secrets()
            self._client = client_builder(resolved_credentials, user_email)

        # Per-tick fetch cache — populated by :meth:`list_changes` and
        # read by :meth:`fetch` so the orchestrator can pull the
        # already-acquired message body without a second Gmail call.
        self._cache: dict[str, GmailMessage] = {}
        # The next-tick cursor — populated after a successful
        # :meth:`list_changes` drain.
        self._next_cursor: str | None = None
        # Wave E per-container cursor map (parallel to the M365 email
        # connector). Populated only on the ON branch of the flag.
        self._next_cursor_by_container: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # SourceConnector Protocol surface
    # ------------------------------------------------------------------

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream changes from the Gmail History API since ``cursor``.

        ``cursor`` is the opaque ``historyId`` from the previous tick;
        ``None`` triggers a cold-start call to ``users.getProfile`` to
        seed the cursor at the live mailbox tip. Bulk backfill from
        t=0 is intentionally NOT supported in v1 — Gmail's History API
        rejects values older than ~7 days, so the cold-start path
        starts at "now" and lets subsequent ticks drain forward.
        """
        events: list[ChangeEvent] = []
        if cursor is None:
            # Cold start — seed the cursor at the live tip.
            history_id = self._client.get_profile_history_id()
            self._next_cursor = history_id
            return iter(events)
        for message_id in self._client.iter_history_message_ids(start_history_id=cursor):
            message = self._client.get_message(message_id)
            self._cache[message_id] = message
            events.append(self._build_change_event(message))
        # Don't-clobber contract (SourceConnector.next_cursor): the drain
        # only advanced the cursor if the History API surfaced an
        # advancing historyId. When ``last_history_id()`` is None — the
        # parser defensively collapsed a non-string historyId to None —
        # we MUST return None so the pipeline preserves the prior
        # persisted cursor. Echoing the stale input ``cursor`` would
        # falsely signal an advance to a window we already processed,
        # making the next tick re-query the identical window and re-emit
        # every already-processed message.
        self._next_cursor = self._client.last_history_id()
        return iter(events)

    def fetch(self, item_id: str) -> RawArtefact:
        """Return the cached body bytes for ``item_id``.

        :meth:`list_changes` populates the cache; :meth:`fetch` reads
        it. The body is the decoded plaintext (or stripped HTML when
        no text/plain part exists). When the message body exceeded
        the configured cap the artefact carries empty bytes and the
        metadata path still surfaces the envelope.
        """
        message = self._cache.get(item_id)
        if message is None:
            # Cache miss — fetch the message on demand.
            message = self._client.get_message(item_id)
            self._cache[item_id] = message
        return RawArtefact(
            raw=message.body,
            mime=message.body_mime or _DEFAULT_BODY_MIME,
            fetched_at=_now_iso(),
        )

    def source_link(self, item_id: str) -> str:
        """Return the Gmail web URL for the message."""
        return f"{_GMAIL_WEB_BASE}{quote(item_id, safe='')}"

    def sensitivity_for(self, _item_id: str) -> Sensitivity:
        """Return the configured per-mailbox sensitivity tier.

        Default is ``client-confidential`` per the Gmail spec brief —
        email is more sensitive than docs by default. The operator can
        override via the ``sensitivity`` config key (validated by
        :func:`make_connector`).
        """
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the ``historyId`` the orchestrator should persist after this tick."""
        return self._next_cursor

    def metadata_for(self, item_id: str) -> SourceMetadata:
        """Return the cached envelope metadata for ``item_id``.

        ADR-021: every Gmail message envelope carries Subject + From +
        To/Cc + Date + Thread + Labels before the orchestrator asks for
        :meth:`fetch`. We surface ``From`` as author + author_email,
        ``Date`` as modified_at, ``To`` recipients as tags, and the
        rest (subject / thread_id / labels / cc / bcc) as properties.
        Cache miss collapses to an empty :class:`SourceMetadata`.
        """
        message = self._cache.get(item_id)
        if message is None:
            return SourceMetadata()
        headers = _headers_by_name(message)
        from_addr = headers.get(_HEADER_FROM)
        to_addrs = _split_addresses(headers.get(_HEADER_TO, ""))
        cc_addrs = _split_addresses(headers.get(_HEADER_CC, ""))
        bcc_addrs = _split_addresses(headers.get(_HEADER_BCC, ""))
        subject = headers.get(_HEADER_SUBJECT)
        date = headers.get(_HEADER_DATE)
        properties: dict[str, str] = {}
        if subject:
            properties["subject"] = subject
        if message.thread_id:
            properties["thread_id"] = message.thread_id
        if cc_addrs:
            properties["cc"] = ", ".join(cc_addrs)
        if bcc_addrs:
            properties["bcc"] = ", ".join(bcc_addrs)
        if message.label_ids:
            properties["labels"] = ", ".join(message.label_ids)
        if message.attachments:
            properties["attachments"] = ", ".join(att.filename for att in message.attachments)
        return SourceMetadata(
            modified_at=date,
            created_at=date,
            author=from_addr,
            author_email=_extract_email_address(from_addr) if from_addr else None,
            tags=tuple(to_addrs),
            properties=properties,
        )

    # ------------------------------------------------------------------
    # Topology Wave B capability shims
    # ------------------------------------------------------------------

    def load_from_checkpoint(self, _container: Container, checkpoint: str | None) -> Iterator[ChangeEvent]:
        """CheckpointedConnector shim — delegate to :meth:`list_changes`.

        Gmail's History API works on opaque ``historyId`` strings; the
        shim forwards ``checkpoint`` (or ``None`` for cold-start)
        directly to :meth:`list_changes` so observable behaviour
        matches the v1 path.
        """
        return self.list_changes(checkpoint)

    # ------------------------------------------------------------------
    # Topology Wave E per-mailbox container surface
    # ------------------------------------------------------------------

    def iter_containers(self, cc_pair_id: int) -> Iterator[Container]:
        """Yield one :class:`Container` for this mailbox.

        Gmail is single-mailbox-per-cc_pair in v1 (mirrors the Onyx
        Gmail design — one Container per authorised user); the Wave E
        per-label slice is a future enhancement.
        """
        yield Container(
            cc_pair_id=cc_pair_id,
            container_id=self._user,
            access_state="ACCESSIBLE",
            cursor_token=None,
            last_synced_at=None,
        )

    def list_changes_for_container(self, container: Container) -> Iterator[ChangeEvent]:
        """Stream changes for one mailbox Container.

        Drives a History API query starting from
        ``container.cursor_token`` (the previous tick's historyId for
        this mailbox). Per-container next-cursor is recorded via
        :meth:`next_cursor_for_container`.

        ``topology_gmail`` retired post-cutover (task #132); the
        per-mailbox path is now the only behaviour.
        """
        return self._list_changes_scoped(container)

    def load_hierarchy(self, cc_pair_id: int) -> Iterator[HierarchyNode]:
        """HierarchyConnector — emit one root FOLDER for the mailbox.

        v1 emits a single root FOLDER (the Gmail mailbox). Per-label
        FOLDER children are a Wave E+1 enhancement once label-scoped
        cursor routing is implemented.
        """
        yield HierarchyNode(
            cc_pair_id=cc_pair_id,
            raw_node_id=_HIERARCHY_ROOT_ID,
            raw_parent_id=None,
            display_name=f"Gmail ({self._user})",
            link=_GMAIL_WEB_BASE.removesuffix("/"),
            node_type="FOLDER",
            external_access_json=None,
            sensitivity_hint=None,
        )

    def next_cursor_for_container(self, container_id: str) -> str | None:
        """Return the historyId the framework should persist for this mailbox."""
        return self._next_cursor_by_container.get(container_id)

    def _list_changes_scoped(self, container: Container) -> Iterator[ChangeEvent]:
        """Wave E ON-branch: drain Gmail History against this mailbox only.

        Reads ``container.cursor_token`` as the per-mailbox historyId,
        drives :meth:`GmailClient.iter_history_message_ids`, emits one
        ``created`` ChangeEvent per message, primes the per-tick
        fetch cache, and records the terminal historyId in
        ``_next_cursor_by_container``.
        """
        mailbox = container.container_id
        cursor = container.cursor_token
        events: list[ChangeEvent] = []
        if cursor is None:
            # Cold start under Wave E — seed the live tip per mailbox.
            history_id = self._client.get_profile_history_id()
            self._next_cursor_by_container[mailbox] = history_id
            return iter(events)
        for message_id in self._client.iter_history_message_ids(start_history_id=cursor):
            message = self._client.get_message(message_id)
            self._cache[message_id] = message
            event = self._build_change_event(message, extra_metadata={"mailbox": mailbox})
            events.append(event)
        # Don't-clobber contract (mirrors :meth:`list_changes`): only
        # advance the per-mailbox cursor when the History API surfaced an
        # advancing historyId. A None ``last_history_id()`` means "no
        # advance this tick" — return None so the framework preserves the
        # prior persisted per-mailbox cursor instead of re-asserting a
        # false advance to an already-processed window.
        self._next_cursor_by_container[mailbox] = self._client.last_history_id()
        return iter(events)

    def _build_change_event(
        self,
        message: GmailMessage,
        *,
        extra_metadata: Mapping[str, str] | None = None,
    ) -> ChangeEvent:
        """Lift one :class:`GmailMessage` into a ``created`` :class:`ChangeEvent`.

        ``modified_at`` is the message's ``Date`` header (when present)
        — Gmail's structured timestamp lives in the headers payload.
        Falls back to wall-clock-now when the header is absent.
        """
        headers = _headers_by_name(message)
        modified_at = headers.get(_HEADER_DATE) or _now_iso()
        metadata: dict[str, str] = {_SENSITIVITY_METADATA_KEY: str(self._sensitivity)}
        if extra_metadata:
            metadata.update(extra_metadata)
        return ChangeEvent(
            op="created",
            item_id=message.message_id,
            modified_at=modified_at,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Header / address helpers (module-level so they're testable)
# ---------------------------------------------------------------------------


def _headers_by_name(message: GmailMessage) -> dict[str, str]:
    """Return a case-insensitive lookup of the message headers.

    Gmail header names are canonical (Subject / From / To) but we
    lower-case the keys so the lookup never trips on a casing change.
    """
    return {h.name.lower(): h.value for h in message.headers}


def _split_addresses(value: str) -> Sequence[str]:
    """Split a header value like ``"a@example.com, b@example.com"`` into a list."""
    if not value:
        return ()
    parts = [part.strip() for part in value.split(",")]
    return tuple(part for part in parts if part)


def _extract_email_address(value: str) -> str | None:
    """Extract the ``foo@bar.com`` substring from a ``"Name <foo@bar.com>"`` block.

    Falls back to the value itself when no angle-brackets are present
    and the value contains an ``@`` symbol.
    """
    start = value.find("<")
    end = value.find(">")
    if start != -1 and end != -1 and end > start:
        candidate = value[start + 1 : end].strip()
        if "@" in candidate:
            return candidate
    if "@" in value:
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# make_connector — entry-point factory
# ---------------------------------------------------------------------------


def make_connector(config: Mapping[str, Any]) -> GmailConnector:
    """Construct a :class:`GmailConnector` from a config mapping.

    Expected keys:

      * ``user_email`` (required) — the authorised mailbox.
      * ``sensitivity`` (optional) — must be one of F39's tier values;
        defaults to ``client-confidential``.

    Credentials resolve via :func:`kairix.secrets.get_secret` —
    ``connector-gmail-client-id`` / ``connector-gmail-client-secret`` /
    ``connector-gmail-refresh-token`` must all be set in the Key Vault
    before the connector can run. Tracked under GH #356 for the
    Workspace OAuth provisioning.

    Registered via ``[project.entry-points."kairix.connectors"]`` in
    kairix's ``pyproject.toml`` so the orchestration layer can resolve
    ``gmail`` to this factory by name.
    """
    user_email = config.get("user_email")
    if not isinstance(user_email, str) or not user_email:
        raise ValueError(
            "gmail: config is missing 'user_email'. "
            "fix: add user_email: agent-alpha@example.com under the gmail connector block "
            "in kairix.config.yaml. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8."
        )

    declared_sensitivity = config.get(_SENSITIVITY_METADATA_KEY)
    sensitivity: Sensitivity = DEFAULT_SENSITIVITY
    if declared_sensitivity is not None:
        if not isinstance(declared_sensitivity, str) or declared_sensitivity not in _VALID_SENSITIVITY_TIERS:
            raise ValueError(
                f"gmail: sensitivity must be one of {_VALID_SENSITIVITY_TIERS!r}; "
                f"got {declared_sensitivity!r}. "
                "fix: remove the sensitivity key (defaults to client-confidential) or set "
                "it to one of the valid tiers. "
                "next: see docs/architecture/fitness-functions.md F39."
            )
        # mypy can't narrow a runtime check against a tuple of strings
        # back to the Literal Sensitivity type; the explicit cast below
        # is safe because we just verified set membership.
        sensitivity = declared_sensitivity  # type: ignore[assignment]  # F3 rationale: tuple-membership check above guarantees Literal-compatible value

    return GmailConnector(user_email=user_email, sensitivity=sensitivity)
