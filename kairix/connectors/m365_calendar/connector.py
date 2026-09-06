"""``M365CalendarConnector`` — SourceConnector for Microsoft 365 calendars.

Implements :class:`kairix.core.protocols.SourceConnector` for a single
mailbox's calendar in a Microsoft 365 / Azure AD tenant. Change
detection rides Graph's OData delta-query token:

* First sync — no cursor — pulls a date window of
  ``calendarView/delta`` (default 90 days back, 365 days forward).
  Every event surfaces as a ``created`` :class:`ChangeEvent`.
* Subsequent syncs — cursor is the persisted ``@odata.deltaLink`` —
  pulls only the incremental delta since the last tick. New events
  surface as ``created`` / ``modified`` (based on whether the orchestrator
  has seen the event id before); tombstoned events surface as
  ``deleted``. Cancelled events with ``isCancelled: true`` ALSO surface
  as ``deleted`` so downstream timeline-update logic stays uniform.

Auth shares its Azure AD app registration with the
``m365_email_headers`` sibling connector (KP-2): both use the same
tenant id + client id + client secret triple, with Calendar.Read +
Mail.Read application permissions granted at the AD app level.

Per F35, this module only imports from
``kairix.connectors.m365_calendar.*`` (same plugin) and ``kairix.core.*``
(the Protocol surface). No reach into other connectors, no reach into
the extractor layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from kairix.connectors.m365_calendar.auth import (
    DEFAULT_GRAPH_SCOPE,
    OAuth2ClientCredsAuth,
    OAuth2Config,
)
from kairix.connectors.m365_calendar.graph_client import (
    CalendarDeltaPage,
    CalendarEventRecord,
    M365GraphCalendarClient,
    iter_pages,
)
from kairix.core.protocols import (
    ChangeEvent,
    Container,
    Cursor,
    HierarchyNode,
    RawArtefact,
    Sensitivity,
    SourceMetadata,
)
from kairix.secrets.loader import SecretsLoader, SecretsResolver
from kairix.transport.errors import GraphDeltaExpiredError

logger = logging.getLogger(__name__)

CONNECTOR_NAME = "m365_calendar"

# Default date window for the initial sync. Operators can override via
# ``make_connector`` config keys ``window_days_back`` / ``window_days_forward``.
#
# Microsoft Graph ``calendarView/delta`` caps the total window at 13 months
# (≈395 days). Configs that exceed the cap fail at request time with HTTP
# 400 (#382). ``MAX_WINDOW_TOTAL_DAYS`` enforces the cap at config-construction
# time with an affordance pointing the operator at the legal range; default
# ``window_days_back + window_days_forward`` sits at 360 (90 + 270) with a
# 30-day buffer for operators who want to lift ``window_days_back`` without
# retuning ``window_days_forward``.
DEFAULT_WINDOW_DAYS_BACK = 90
DEFAULT_WINDOW_DAYS_FORWARD = 270
MAX_WINDOW_TOTAL_DAYS = 390

# Source-link URI scheme. Outlook web URLs are deeplink-able by event
# id; the orchestrator wraps the connector's source_link result in a
# clickable affordance for the operator.
_OUTLOOK_WEB_URL = "https://outlook.office.com/calendar/item/{event_id}"

# Hierarchy root node id for the calendar tree. Each configured calendar
# (one per UPN) becomes a child FOLDER node under this root.
_HIERARCHY_ROOT_ID = "m365-calendar"


@dataclass(frozen=True)
class M365CalendarConfig:
    """Configuration for an :class:`M365CalendarConnector`.

    Per ADR-031, credential leaves default to empty strings so the
    operator's YAML can omit them and let the connector's injected
    :class:`SecretsResolver` resolve the canonical M365 triple at
    construction time. Operators with a dedicated per-connector AAD app
    can still pin specific values inline (the connector treats those as
    advisory overrides that win over the loader-resolved value).

    Per F15, the dataclass field names carry the ``client_secret`` /
    ``tenant_id`` suffix shape so the secret-logging gate flags any
    plaintext interpolation outside the boundary modules.
    """

    user_id: str
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    sensitivity: Sensitivity = "internal"
    scope: str = DEFAULT_GRAPH_SCOPE
    window_days_back: int = DEFAULT_WINDOW_DAYS_BACK
    window_days_forward: int = DEFAULT_WINDOW_DAYS_FORWARD
    # Wave E topology — multi-calendar support. When the operator
    # declares ``user_ids`` (list of UPNs) the connector emits one
    # :class:`Container` per UPN with that UPN as the ``container_id``.
    # When unset, the connector falls back to a singleton built from
    # ``user_id`` so existing single-mailbox deployments keep working
    # bit-for-bit. The legacy ``list_changes`` path remains the OFF-branch
    # behaviour; the per-container delta-cursor isolation is the ON-branch
    # value-add gated by the ``topology_m365_calendar`` flag.
    user_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        total = self.window_days_back + self.window_days_forward
        if total > MAX_WINDOW_TOTAL_DAYS:
            raise ValueError(
                f"m365_calendar window {self.window_days_back}+{self.window_days_forward}={total} "
                f"days exceeds Graph calendarView/delta 13-month cap (max {MAX_WINDOW_TOTAL_DAYS} total). "
                f"fix: reduce window_days_back or window_days_forward so the sum is <= {MAX_WINDOW_TOTAL_DAYS}. "
                f"next: see kairix/connectors/m365_calendar/connector.py MAX_WINDOW_TOTAL_DAYS for the constant. "
                f"run: kairix config validate"
            )


@dataclass
class _SyncBatch:
    """Mutable accumulator for one ``list_changes`` call's results."""

    events: list[ChangeEvent] = field(default_factory=list)
    delta_link: str | None = None


# Type aliases for the DI seams. ``ClientFactory`` builds the Graph
# client from the connector's config; the default uses the production
# httpx + OAuth2 path. Tests inject a factory that returns a stand-in
# wired against an httpx.MockTransport.
ClientFactory = Callable[[M365CalendarConfig], M365GraphCalendarClient]
# ``PerUserClientFactory`` builds a Graph client scoped to one specific
# UPN — used by the Wave E ON branch so each Container's
# ``container_id`` (the UPN) drives a distinct
# ``/users/{upn}/calendar/calendarView/delta`` request, proving
# per-calendar isolation.
PerUserClientFactory = Callable[[M365CalendarConfig, str], M365GraphCalendarClient]


def _default_client_factory(config: M365CalendarConfig) -> M365GraphCalendarClient:
    """Production client factory — builds OAuth2 auth + Graph client."""
    auth = OAuth2ClientCredsAuth(
        OAuth2Config(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            scope=config.scope,
        )
    )
    return M365GraphCalendarClient(user_id=config.user_id, auth=auth)


def _default_per_user_client_factory(config: M365CalendarConfig, user_id: str) -> M365GraphCalendarClient:
    """Production per-UPN client factory — Wave E ON branch.

    Builds a Graph client bound to the explicit ``user_id`` so the
    per-container delta query targets that mailbox's calendar
    specifically. Same OAuth2 app-only triple as the legacy factory —
    one Azure AD app registration covers every calendar in the tenant
    per ADR-019 (shared with the ``m365_email_headers`` sibling).
    """
    auth = OAuth2ClientCredsAuth(
        OAuth2Config(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            scope=config.scope,
        )
    )
    return M365GraphCalendarClient(user_id=user_id, auth=auth)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # Graph ``calendarView/delta`` rejects ISO timestamps that carry
    # fractional seconds in ``startDateTime`` / ``endDateTime`` query
    # parameters with HTTP 400, even when the rest of the request is
    # well-formed (#382 part-2). Strip microseconds before serialising
    # so the connector's window-bound formatter is wire-safe.
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_config_credentials(
    config: M365CalendarConfig,
    secrets: SecretsResolver,
) -> M365CalendarConfig:
    """Fill missing tenant / client / secret leaves from the secrets resolver.

    Inline values on the incoming :class:`M365CalendarConfig` win — the
    operator who pins a per-connector AAD app in YAML sees that value
    flow through unchanged. Empty leaves trigger
    :meth:`SecretsResolver.require` against the canonical
    ``(connector, m365, None, <leaf>)`` identity. Same canonical triple
    as the :mod:`kairix.connectors.m365_email_headers` sibling per KP-2.

    Returns a new :class:`M365CalendarConfig` with the three credential
    fields fully populated. Raises
    :class:`kairix.secrets.SecretNotFoundError` from the loader when a
    leaf is unset both inline AND on the resolver — the loader's message
    already carries the F21 ``fix:`` / ``next:`` / ``run:`` markers.

    Constructs the return value field-by-field rather than via
    :func:`dataclasses.replace` so the inferred return type stays the
    concrete :class:`M365CalendarConfig` (Sonar python:S5886) — matches
    the ``_replace_document_root`` pattern in ``kairix/knowledge/wikilinks/cli.py``.
    """
    tenant_id = config.tenant_id or secrets.require("connector", "m365", None, "tenant-id")
    client_id = config.client_id or secrets.require("connector", "m365", None, "client-id")
    client_secret = config.client_secret or secrets.require("connector", "m365", None, "client-secret")
    return M365CalendarConfig(
        user_id=config.user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        sensitivity=config.sensitivity,
        scope=config.scope,
        window_days_back=config.window_days_back,
        window_days_forward=config.window_days_forward,
        user_ids=config.user_ids,
    )


class M365CalendarConnector:
    """SourceConnector for one M365 calendar (one mailbox).

    Construction is cheap (no Graph I/O, no OAuth2 exchange — but it
    DOES resolve the OAuth client-credentials triple via the injected
    :class:`SecretsResolver` so the operator sees a typed loader error
    here rather than at first-fetch time). The first
    :meth:`list_changes` call triggers the token fetch + first Graph
    page.

    DI seams:

    * ``client_factory`` — builds the underlying
      :class:`M365GraphCalendarClient`. Tests pass a factory that
      returns a stand-in client; production uses the OAuth2 + httpx
      path.
    * ``clock`` — returns the current UTC datetime. Tests substitute a
      :class:`tests.fakes.FakeClock`-like callable so the date window
      is deterministic. F6-clean: the default is a real callable.
    * ``secrets`` — :class:`SecretsResolver` used to resolve missing
      ``tenant_id`` / ``client_id`` / ``client_secret`` leaves on the
      :class:`M365CalendarConfig`. Tests pass
      :class:`tests.fakes.FakeSecretsLoader`; production omits the
      kwarg (defaults to :class:`SecretsLoader`). Mirrors the
      ``m365_email_headers`` sibling connector's shape per KP-2.
    """

    name: str = CONNECTOR_NAME
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5 * 1024**3  # 5 GiB — m365 attachments can be large

    def __init__(
        self,
        config: M365CalendarConfig,
        *,
        client_factory: ClientFactory = _default_client_factory,
        clock: Callable[[], datetime] = _utc_now,
        per_user_client_factory: PerUserClientFactory = _default_per_user_client_factory,
        secrets: SecretsResolver | None = None,
    ) -> None:
        self._secrets: SecretsResolver = secrets if secrets is not None else SecretsLoader()
        self._config = _resolve_config_credentials(config, self._secrets)
        self._client_factory = client_factory
        self._clock = clock
        self._per_user_client_factory = per_user_client_factory
        self._client: M365GraphCalendarClient | None = None
        # Track event ids the connector has emitted as ``created`` so a
        # subsequent delta page tagged with the same id is reported as
        # ``modified`` (Graph's delta surface doesn't distinguish the
        # two — it just yields the current state).
        self._known_ids: set[str] = set()
        # Cache of the most recent delta cursor — kept on the
        # connector so :meth:`list_changes` callers without a cursor
        # (e.g. tests, cold-start before persistence) still resume from
        # the last known token within one process lifetime.
        self._last_delta_link: str | None = None
        # Wave E per-container client cache — keyed by UPN. Each
        # configured calendar holds its own :class:`M365GraphCalendarClient`
        # so per-calendar requests don't share connection state.
        self._per_user_clients: dict[str, M365GraphCalendarClient] = {}
        # ADR-021 (Wave E.5): cache per-event envelope metadata so
        # ``metadata_for`` can return organiser + start + categories
        # without re-hitting Graph for an item we already saw on the
        # current tick. Keyed by event_id; populated during ``_drain``.
        self._event_metadata_cache: dict[str, CalendarEventRecord] = {}

    # ------------------------------------------------------------------
    # SourceConnector Protocol surface
    # ------------------------------------------------------------------

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream Graph-observed calendar changes since ``cursor``.

        ``cursor`` is a Graph ``@odata.deltaLink`` URL. ``None`` means
        first sync — pull the configured date window.

        The cursor token to persist for the next call is appended to
        the very end of the iterator as an empty-payload sentinel? No:
        Graph delta links carry the cursor on the page itself, and the
        orchestrator inspects the connector's ``last_delta_link``
        attribute after draining the iterator. Keeping the cursor off
        the :class:`ChangeEvent` payload preserves F42's narrow
        boundary surface.
        """
        client = self._ensure_client()
        try:
            batch = self._drain(client, cursor)
        except GraphDeltaExpiredError:
            if cursor is None:
                raise
            logger.warning("m365 calendar: stored delta cursor expired; restarting from the configured initial window")
            batch = self._drain(client, None)
        self._last_delta_link = batch.delta_link
        return iter(batch.events)

    def fetch(self, item_id: str) -> RawArtefact:
        """Return the raw event payload for ``item_id``.

        The Graph delta query already brought the full event body
        through; rather than re-fetch, the connector caches the most
        recent payload per id during :meth:`list_changes` and returns
        it here. If the orchestrator asks for an id the connector
        hasn't seen this process, raise — there's no point silently
        re-querying when Bronze already has the bytes for that id.
        """
        if item_id not in self._event_payload_cache:
            raise ValueError(
                f"m365_calendar: no cached payload for event id {item_id!r}. "
                "fix: drive fetch only against item_ids emitted by list_changes in this process. "
                "next: see docs/architecture/connector-ingestion-architecture.md §10."
            )
        payload = self._event_payload_cache[item_id]
        return RawArtefact(
            raw=payload.encode("utf-8"),
            mime="application/json",
            fetched_at=_iso(self._clock()),
        )

    def source_link(self, item_id: str) -> str:
        """Outlook web URL deep-link for the given event id.

        The Outlook web app accepts the Graph event id directly in its
        item URL — the connector returns the canonical clickable form
        the operator can follow back to the source calendar entry.
        """
        return _OUTLOOK_WEB_URL.format(event_id=item_id)

    def sensitivity_for(self, _item_id: str) -> Sensitivity:
        """Return the connector's configured sensitivity tier.

        v1 has no per-item overrides. ADR-005 default for calendar
        events is ``internal``; per-event downgrade (e.g. attendees-
        include-externals → ``client-confidential``) is a future PR.
        """
        return self._config.sensitivity

    # ------------------------------------------------------------------
    # Topology Wave B — capability mix-in shims (no behavioural change)
    # ------------------------------------------------------------------
    # The shims below let the connector satisfy the new capability
    # Protocols (CheckpointedConnector, CredentialsConnector,
    # OAuthConnector) by delegating to existing methods OR raising
    # actionable NotImplementedError where the source kind does not
    # support the surface. Production routing through these methods is
    # gated by ``topology_protocol`` (default-off).

    def load_from_checkpoint(self, _container: Container, checkpoint: str | None) -> Iterator[ChangeEvent]:
        """CheckpointedConnector shim — delegate to :meth:`list_changes` using the checkpoint.

        Graph calendar delta works on opaque deltaLink strings; the
        shim forwards ``checkpoint`` directly to :meth:`list_changes`
        so observable behaviour matches the v1 path. ``_container`` is
        accepted for Protocol compliance but the legacy path is
        single-calendar per cc_pair (Wave E activates per-container routing).
        """
        return self.list_changes(checkpoint)

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """CredentialsConnector shim — return the input unchanged.

        Client-credentials flow consumes the operator-supplied tenant /
        client / secret triple as-is; no transformation, no token
        exchange at this surface (the OAuth2 helper exchanges at
        first-fetch time).
        """
        return credentials

    @classmethod
    def oauth_authorization_url(cls, _state: str) -> str:
        """OAuthConnector shim — raise actionable NotImplementedError.

        This connector uses the OAuth2 client-credentials flow (app-only,
        no operator-in-the-loop) per ADR-004 — there is no authorization
        URL to visit. The shim raises so a framework path that mistakenly
        routes to the three-legged flow fails loudly with a fix hint.
        """
        raise NotImplementedError(
            "m365_calendar: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/m365_calendar/connector.py for the credential contract."
        )

    @classmethod
    def oauth_code_to_token(cls, _code: str) -> dict[str, Any]:
        """OAuthConnector shim — raise actionable NotImplementedError.

        Counterpart to :meth:`oauth_authorization_url` — no code-to-token
        exchange because this connector does not surface an OAuth
        consent screen.
        """
        raise NotImplementedError(
            "m365_calendar: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/m365_calendar/connector.py for the credential contract."
        )

    # ------------------------------------------------------------------
    # Topology Wave E — per-connector multi-container pilot
    # ------------------------------------------------------------------
    # Wave B landed shim implementations of the capability Protocols
    # (CheckpointedConnector / CredentialsConnector / OAuthConnector).
    # Wave E adds real implementations behind the
    # ``topology_m365_calendar`` flag:
    #
    #   * :meth:`iter_containers` — one :class:`Container` per configured
    #     calendar (per UPN), each with its own Graph ``@odata.deltaLink``
    #     persisted as the container's ``cursor_token``.
    #   * :meth:`list_changes_for_container` — when flag ON, reads
    #     ``container.cursor_token`` (a per-calendar Graph deltaLink) and
    #     runs the Graph delta query against ``container.container_id``
    #     (the UPN) ONLY. When flag OFF, retains the Wave B shim
    #     behaviour (delegate to legacy :meth:`list_changes`).
    #   * :meth:`load_hierarchy` — emits a root FOLDER node plus one
    #     FOLDER per configured calendar as a child of root, parent-
    #     before-child per F58. Single behaviour on both branches —
    #     the multi-calendar value-add comes from iter_containers +
    #     list_changes_for_container's per-cursor isolation.
    #
    # The flag defaults OFF so existing operators see bit-for-bit
    # current behaviour. The ON branch is the per-container pattern
    # that mirrors the obsidian Wave E pilot.

    def iter_containers(self, cc_pair_id: int) -> Iterator[Container]:
        """Yield one :class:`Container` per configured calendar (per UPN).

        Topology §4: each Container has its own delta cursor — the
        Wave E pilot maps each operator-declared calendar to its own
        Container so the operator can add or remove individual user
        mailboxes without affecting the cursor state of the others.

        Calling convention: the framework's lifecycle layer passes
        ``cc_pair_id`` so the connector can construct the Container
        without reaching back into the cc_pair store.

        ``access_state`` is always ``ACCESSIBLE`` — Graph app-only auth
        either grants or doesn't grant the configured calendars at
        consent time; per-calendar permission drift surfaces as a
        request-time error, not at iteration. ``cursor_token`` and
        ``last_synced_at`` start ``None``; the framework persists
        subsequent values (the Graph ``@odata.deltaLink``) to the
        ``topology_containers`` table.

        Single-calendar fallback: when the config declares only
        ``user_id`` (and no ``user_ids`` list) emit one Container with
        that UPN as ``container_id``. Multi-calendar deployments yield
        one Container per configured UPN.
        """
        for upn in self._configured_upns():
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id=upn,
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )

    def list_changes_for_container(self, container: Container) -> Iterator[ChangeEvent]:
        """Stream calendar events for one Container.

        Builds (or re-uses) a Graph client scoped to
        ``container.container_id`` (the UPN), reads
        ``container.cursor_token`` as the per-calendar Graph
        ``@odata.deltaLink`` (None on first sync), and drains the delta
        pages for THAT calendar only. Per-calendar isolation means
        adding or removing one user's calendar does not affect the
        cursor state of the others.

        ``topology_m365_calendar`` retired post-cutover (task #132);
        the per-calendar path is now the only behaviour.
        """
        return self._list_changes_scoped(container)

    def load_hierarchy(self, cc_pair_id: int) -> Iterator[HierarchyNode]:
        """HierarchyConnector — emit FOLDER nodes parent-before-child.

        Emits a root FOLDER node (``raw_node_id="m365-calendar"``,
        ``raw_parent_id=None``) followed by one child FOLDER node per
        configured calendar, with ``raw_node_id`` set to the UPN and
        ``raw_parent_id`` pointing at the root. Parent-before-child
        per F58.

        Per-calendar sub-folder hierarchy (work / personal categories)
        is a Wave-E+1 enhancement — this slice keeps the hierarchy at
        calendar-as-folder granularity to mirror the dispatch brief.
        """
        yield HierarchyNode(
            cc_pair_id=cc_pair_id,
            raw_node_id=_HIERARCHY_ROOT_ID,
            raw_parent_id=None,
            display_name="M365 Calendars",
            link=None,
            node_type="FOLDER",
            external_access_json=None,
            sensitivity_hint=None,
        )
        for upn in self._configured_upns():
            yield HierarchyNode(
                cc_pair_id=cc_pair_id,
                raw_node_id=upn,
                raw_parent_id=_HIERARCHY_ROOT_ID,
                display_name=upn,
                link=None,
                node_type="FOLDER",
                external_access_json=None,
                sensitivity_hint=None,
            )

    # ------------------------------------------------------------------
    # Cursor + cache accessors (used by the orchestration layer)
    # ------------------------------------------------------------------

    @property
    def last_delta_link(self) -> str | None:
        """The delta-link to persist as the next cursor.

        ``None`` until the first successful :meth:`list_changes` call.
        After draining, the orchestrator reads this and writes it to
        the ``connector_cursors`` row keyed by the connector's name +
        the operator's user_id.
        """
        return self._last_delta_link

    def next_cursor(self) -> str | None:
        """Return the Graph deltaLink to persist after the last drain.

        m365_calendar's cursor IS the opaque Graph ``@odata.deltaLink``
        URL — NOT a per-item ``modified_at`` (that would force a
        full-window resync every tick). Delegates to the
        :attr:`last_delta_link` property which is set on every
        :meth:`list_changes` drain.
        """
        return self._last_delta_link

    def seed_known_ids(self, ids: Iterable[str]) -> None:
        """Pre-populate the known-id set used to distinguish created vs modified.

        Graph's delta endpoint surfaces the current state of every
        event — it doesn't tell the caller whether a given id is new
        or already seen. The connector tracks which ids it has emitted
        as ``created`` so the next delta page surfaces the same id as
        ``modified``. Across process restarts the orchestrator restores
        that state by calling :meth:`seed_known_ids` with the ids
        already persisted in the documents table.
        """
        self._known_ids.update(ids)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying Graph client(s).

        Closes both the legacy single-client (used by
        :meth:`list_changes`) and every per-UPN client built for the
        Wave E ON branch. Idempotent — safe to call from any thread,
        called multiple times.
        """
        if self._client is not None:
            self._client.close()
            self._client = None
        for client in self._per_user_clients.values():
            client.close()
        self._per_user_clients.clear()

    def __enter__(self) -> M365CalendarConnector:
        self._ensure_client()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    _event_payload_cache: dict[str, str]

    def _ensure_client(self) -> M365GraphCalendarClient:
        if self._client is None:
            self._client = self._client_factory(self._config)
            # Lazily attach the payload cache on first client build so
            # the __init__ surface stays simple.
            self._event_payload_cache = {}
        return self._client

    def _drain(self, client: M365GraphCalendarClient, cursor: Cursor | None) -> _SyncBatch:
        """Walk Graph delta pages and convert to :class:`ChangeEvent`."""
        batch = _SyncBatch()
        first_page = self._fetch_first_page(client, cursor)
        for page in iter_pages(client, first_page):
            for record in page.events:
                event = self._record_to_change_event(record)
                if event is not None:
                    batch.events.append(event)
                    if not record.removed:
                        self._event_payload_cache[record.event_id] = record.raw_payload
                        # ADR-021: cache the structured envelope so
                        # ``metadata_for`` can return organiser + start
                        # + categories without re-querying Graph.
                        self._event_metadata_cache[record.event_id] = record
            if page.delta_link is not None:
                batch.delta_link = page.delta_link
        return batch

    def _fetch_first_page(self, client: M365GraphCalendarClient, cursor: Cursor | None) -> CalendarDeltaPage:
        """First page of a sync tick — either initial date-window or delta follow-up."""
        if cursor is not None:
            return client.fetch_delta_page(cursor)
        if self._last_delta_link is not None:
            return client.fetch_delta_page(self._last_delta_link)
        now = self._clock()
        window_start = now - timedelta(days=self._config.window_days_back)
        window_end = now + timedelta(days=self._config.window_days_forward)
        return client.fetch_initial_delta(_iso(window_start), _iso(window_end))

    def _record_to_change_event(self, record: CalendarEventRecord) -> ChangeEvent | None:
        """Translate one :class:`CalendarEventRecord` to a :class:`ChangeEvent`."""
        if record.removed or record.cancelled:
            return ChangeEvent(
                op="deleted",
                item_id=record.event_id,
                modified_at=record.last_modified_iso or _iso(self._clock()),
            )
        if record.event_id in self._known_ids:
            op: Any = "modified"
        else:
            self._known_ids.add(record.event_id)
            op = "created"
        return ChangeEvent(
            op=op,
            item_id=record.event_id,
            modified_at=record.last_modified_iso or _iso(self._clock()),
            metadata={
                "subject": record.subject,
                "start": record.start_iso,
                "end": record.end_iso,
                "location": record.location,
                "attendees": record.attendees,
                "organiser": record.organiser,
            },
        )

    # ------------------------------------------------------------------
    # Wave E ON-branch internals
    # ------------------------------------------------------------------

    def _configured_upns(self) -> tuple[str, ...]:
        """Return the configured calendar UPNs in deterministic order.

        Single-calendar (the historical config shape) emits a tuple of
        one — derived from ``config.user_id``. Multi-calendar
        (``config.user_ids`` populated) returns the operator-declared
        list as-is so emission order is operator-controlled.
        """
        if self._config.user_ids:
            return self._config.user_ids
        return (self._config.user_id,)

    def _ensure_client_for_upn(self, upn: str) -> M365GraphCalendarClient:
        """Get-or-create the Graph client scoped to ``upn``.

        Cached per UPN on the connector — first call builds via
        ``per_user_client_factory``, subsequent calls re-use. The cache
        is cleared on :meth:`close`.
        """
        client = self._per_user_clients.get(upn)
        if client is None:
            client = self._per_user_client_factory(self._config, upn)
            self._per_user_clients[upn] = client
            # Lazily attach the payload cache on first client build so
            # the __init__ surface stays simple — same shape as the
            # legacy ``_ensure_client`` path.
            if not hasattr(self, "_event_payload_cache"):
                self._event_payload_cache = {}
        return client

    def _list_changes_scoped(self, container: Container) -> Iterator[ChangeEvent]:
        """Wave E ON-branch: drain Graph delta for one container's UPN.

        Reads the container's own ``cursor_token`` (the per-calendar
        Graph ``@odata.deltaLink``) and walks delta pages for the
        container's UPN only. Each container's cursor is read
        independently — adding or removing one calendar does not
        disturb another calendar's resume position.

        Bypasses :meth:`_drain` / :meth:`_fetch_first_page` because
        those fall back to the connector-wide ``_last_delta_link``
        when the explicit cursor is ``None``, which would cross-
        pollute one container's resume position with another's. The
        per-container drain reads only ``container.cursor_token``.
        """
        upn = container.container_id
        client = self._ensure_client_for_upn(upn)
        try:
            batch = self._drain_for_container(client, container.cursor_token)
        except GraphDeltaExpiredError:
            if container.cursor_token is None:
                raise
            logger.warning(
                "m365 calendar: stored delta cursor expired for %s; "
                "restarting that calendar from the configured initial window",
                upn,
            )
            batch = self._drain_for_container(client, None)
        self._last_delta_link = batch.delta_link
        return iter(batch.events)

    def _drain_for_container(self, client: M365GraphCalendarClient, cursor: Cursor | None) -> _SyncBatch:
        """Per-container delta drain — never reads ``self._last_delta_link``.

        Wave E isolation: each container's cursor stands alone. ``None``
        means first sync for that container (initial date-window
        query); a string means resume from that container's persisted
        ``@odata.deltaLink``.
        """
        batch = _SyncBatch()
        first_page = self._fetch_first_page_for_container(client, cursor)
        for page in iter_pages(client, first_page):
            self._absorb_page_into_batch(page, batch)
        return batch

    def _absorb_page_into_batch(self, page: CalendarDeltaPage, batch: _SyncBatch) -> None:
        """Translate one Graph delta page into ChangeEvents on ``batch``.

        Extracted out of :meth:`_drain_for_container` to keep the per-
        container drain under the cognitive-complexity ceiling. Updates
        ``batch.delta_link`` when the page carries one.
        """
        for record in page.events:
            event = self._record_to_change_event(record)
            if event is None:
                continue
            batch.events.append(event)
            if not record.removed:
                self._cache_payload(record.event_id, record.raw_payload)
        if page.delta_link is not None:
            batch.delta_link = page.delta_link

    def _cache_payload(self, event_id: str, raw_payload: str) -> None:
        """Lazily attach + write the per-process Graph payload cache.

        Mirrors the lazy-attach shape in :meth:`_ensure_client_for_upn`.
        Lifted to a helper so :meth:`_absorb_page_into_batch` stays a
        flat top-level loop.
        """
        if not hasattr(self, "_event_payload_cache"):
            self._event_payload_cache = {}
        self._event_payload_cache[event_id] = raw_payload

    def _fetch_first_page_for_container(
        self, client: M365GraphCalendarClient, cursor: Cursor | None
    ) -> CalendarDeltaPage:
        """Per-container first-page fetch — does not consult shared state.

        Either resumes from the container's cursor (when set) or runs
        the configured initial date-window query. Distinct from
        :meth:`_fetch_first_page` so the per-container path is
        completely independent of the legacy single-cursor cache.
        """
        if cursor is not None:
            return client.fetch_delta_page(cursor)
        now = self._clock()
        window_start = now - timedelta(days=self._config.window_days_back)
        window_end = now + timedelta(days=self._config.window_days_forward)
        return client.fetch_initial_delta(_iso(window_start), _iso(window_end))

    # ------------------------------------------------------------------
    # ADR-021 (Wave E.5) — per-source envelope metadata
    # ------------------------------------------------------------------

    def metadata_for(self, item_id: str) -> SourceMetadata:
        """Return cached Graph event envelope metadata for ``item_id``.

        ADR-021: Graph events carry organiser address + start time +
        last-modified time on the envelope; categories surface as
        ``tags``. Cache miss collapses to an empty
        :class:`SourceMetadata` so an unseen id never crashes the
        pipeline.
        """
        record = self._event_metadata_cache.get(item_id)
        if record is None:
            return SourceMetadata()
        author = record.organiser.strip() if record.organiser else None
        author_email = author if author and "@" in author else None
        properties: dict[str, str] = {}
        if record.subject:
            properties["subject"] = record.subject
        if record.start_iso:
            properties["start"] = record.start_iso
        if record.location:
            properties["location"] = record.location
        return SourceMetadata(
            modified_at=record.last_modified_iso or None,
            created_at=record.start_iso or None,
            author=author,
            author_email=author_email,
            tags=record.attendees,
            properties=properties,
        )


def make_connector(config: Mapping[str, Any]) -> M365CalendarConnector:
    """Construct an :class:`M365CalendarConnector` from a config mapping.

    Expected keys:

    * ``user_id`` (required) — the mailbox principal (UPN or object id).
    * ``sensitivity`` (optional) — one of the F39 sensitivity literals;
      defaults to ``"internal"``.
    * ``scope`` (optional) — OAuth2 scope; defaults to
      :data:`DEFAULT_GRAPH_SCOPE`.
    * ``window_days_back`` / ``window_days_forward`` (optional) —
      initial-sync date window; defaults to 90 / 365.
    * ``user_ids`` (optional, Wave E) — list/tuple of additional UPNs
      so a single connector covers multiple calendars. When unset, the
      Wave E ON branch falls back to a singleton derived from
      ``user_id``; when set, each entry becomes its own Container with
      its own delta cursor.
    * ``tenant_id`` / ``client_id`` / ``client_secret`` (optional
      advisory overrides) — operators with a dedicated per-connector M365
      AAD app can pin specific credentials inline; when present, the
      override wins over the loader-resolved value. The common case (one
      tenant-wide app shared with ``m365_email_headers``) omits these
      keys and lets the secret resolver supply them.

    Credentials resolve via :class:`kairix.secrets.loader.SecretsLoader`
    against the canonical identities ``(connector, m365, None, tenant-id)``,
    ``(connector, m365, None, client-id)``, and
    ``(connector, m365, None, client-secret)`` — same triple as the
    ``m365_email_headers`` sibling connector (KP-2: one Azure AD app
    grants Calendar.Read + Mail.Read across the tenant). The loader's
    legacy-alias fallback resolves the historical ``CONNECTOR_M365_*`` /
    ``KAIRIX_M365_*`` / ``M365_*`` env vars transparently so existing
    deployments keep working unchanged.

    Tests that want to verify the loader resolution shape construct
    :class:`M365CalendarConnector` directly with ``secrets=`` set to
    :class:`tests.fakes.FakeSecretsLoader` — mirrors the
    ``m365_email_headers`` sibling's test pattern.

    A missing ``user_id`` raises an F21-shaped :class:`ValueError`. A
    missing credential leaf with no inline override raises
    :class:`kairix.secrets.SecretNotFoundError` from the connector's
    constructor; that message already carries the ``fix:`` / ``next:``
    / ``run:`` markers naming the canonical KV secret + env var.

    Registered via ``[project.entry-points."kairix.connectors"]`` in
    kairix's ``pyproject.toml`` so the orchestration layer resolves
    ``m365_calendar`` to this factory by name.
    """
    user_id = config.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError(
            "m365_calendar: config is missing 'user_id'. "
            "fix: add user_id: operator@example.com under the m365_calendar "
            "connector block in kairix.config.yaml. "
            "next: see docs/architecture/connector-ingestion-architecture.md §10."
        )

    resolved = M365CalendarConfig(
        user_id=str(user_id),
        tenant_id=_inline_or_empty(config, "tenant_id"),
        client_id=_inline_or_empty(config, "client_id"),
        client_secret=_inline_or_empty(config, "client_secret"),
        sensitivity=config.get("sensitivity", "internal"),
        scope=str(config.get("scope", DEFAULT_GRAPH_SCOPE)),
        window_days_back=int(config.get("window_days_back", DEFAULT_WINDOW_DAYS_BACK)),
        window_days_forward=int(config.get("window_days_forward", DEFAULT_WINDOW_DAYS_FORWARD)),
        user_ids=tuple(str(u) for u in config.get("user_ids", ())),
    )
    return M365CalendarConnector(resolved)


def _inline_or_empty(config: Mapping[str, Any], key: str) -> str:
    """Return the inline string override for ``key`` or ``""`` when absent.

    Empty string is the sentinel the connector's
    :func:`_resolve_config_credentials` reads to decide whether to ask
    the :class:`SecretsResolver` for the leaf. A truthy string here is
    a per-connector override that wins over the loader value.
    """
    value = config.get(key)
    if isinstance(value, str) and value:
        return value
    return ""
