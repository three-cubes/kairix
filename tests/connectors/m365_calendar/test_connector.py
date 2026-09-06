"""Unit tests for :class:`kairix.connectors.m365_calendar.M365CalendarConnector`.

Scope per the KP-3 brief:

  * First sync without cursor → drives the initial date-window query
    and emits ``created`` ChangeEvents.
  * Subsequent sync with persisted cursor → drives the delta-page
    query and distinguishes created vs modified by known-id tracking.
  * Cancelled event → surfaces as a ``deleted`` ChangeEvent.
  * Tombstoned (``@removed``) event → surfaces as a ``deleted`` ChangeEvent.
  * source_link → returns the Outlook web deep-link URL.
  * fetch → returns the cached Graph payload as a ``RawArtefact``;
    rejects ids never seen by ``list_changes``.
  * make_connector → required-key validation produces a typed error
    with an actionable affordance.
  * Sabotage proof (executed below): mutating the connector's
    ``_record_to_change_event`` mapping confirms the per-event op
    classification is load-bearing.

The Graph client is replaced with a recording stand-in that pulls
:class:`CalendarDeltaPage` instances from an in-memory queue. No
network I/O, no OAuth2 exchange.

F1-clean (no monkey-patching production code), F6-clean (every test
seam is a real callable default), F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from kairix.connectors.m365_calendar import (
    M365CalendarConfig,
    M365CalendarConnector,
    make_connector,
)
from kairix.connectors.m365_calendar.graph_client import (
    CalendarDeltaPage,
    CalendarEventRecord,
    M365GraphCalendarClient,
)
from kairix.core.protocols import ChangeEvent, Container, RawArtefact
from kairix.secrets import SecretNotFoundError
from kairix.transport.errors import GraphDeltaExpiredError
from tests.fakes import FakeSecretsLoader


def _full_loader() -> FakeSecretsLoader:
    """FakeSecretsLoader pre-populated with the canonical M365 triple.

    Pass through ``make_connector(config, secrets_loader=_full_loader())``
    so the factory resolves credentials without hitting any real env
    var, KV mount, or legacy chain.
    """
    return FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "fake-tenant",
            ("connector", "m365", None, "client-id"): "fake-client",
            ("connector", "m365", None, "client-secret"): "fake-secret-value",
        }
    )


def _event(
    event_id: str,
    *,
    cancelled: bool = False,
    removed: bool = False,
    subject: str = "Team sync",
) -> CalendarEventRecord:
    return CalendarEventRecord(
        event_id=event_id,
        subject=subject if not removed else "",
        start_iso="2026-05-25T09:00:00Z" if not removed else "",
        end_iso="2026-05-25T10:00:00Z" if not removed else "",
        location="Conference room" if not removed else "",
        attendees=("alpha@example.com",) if not removed else (),
        organiser="organiser@example.com" if not removed else "",
        last_modified_iso="2026-05-25T08:00:00Z" if not removed else "",
        cancelled=cancelled,
        removed=removed,
        raw_payload=('{"id": "' + event_id + '"}') if not removed else "",
    )


def _page(*events: CalendarEventRecord, delta_link: str = "delta-link-1") -> CalendarDeltaPage:
    return CalendarDeltaPage(events=tuple(events), next_link=None, delta_link=delta_link)


class _RecordingClient(M365GraphCalendarClient):
    """In-memory stand-in. Drains a queue of pre-built pages.

    Tracks each Graph call into ``initial_calls`` / ``delta_calls`` so
    the tests assert which entry point fired without coupling to httpx.
    """

    def __init__(self, pages: list[CalendarDeltaPage]) -> None:
        self._queue = list(pages)
        self._user_id = "operator@example.com"
        self._http = None  # type: ignore[assignment]  # F3 rationale: scripted client never makes HTTP calls
        self._page_size = 50
        self.initial_calls: list[tuple[str, str]] = []
        self.delta_calls: list[str] = []

    def fetch_initial_delta(self, start_iso: str, end_iso: str) -> CalendarDeltaPage:
        self.initial_calls.append((start_iso, end_iso))
        return self._queue.pop(0)

    def fetch_delta_page(self, link: str) -> CalendarDeltaPage:
        self.delta_calls.append(link)
        return self._queue.pop(0)

    def close(self) -> None:
        # Intentionally empty — scripted client owns no resources.
        return None


def _config() -> M365CalendarConfig:
    return M365CalendarConfig(
        user_id="operator@example.com",
        tenant_id="placeholder-tenant",
        client_id="placeholder-client",
        client_secret="placeholder-secret",  # pragma: allowlist secret
    )


class _CapturingFactory:
    """Client factory that captures the constructed _RecordingClient.

    Exposes the constructed client as ``self.client`` so tests can
    assert on the recording state (initial_calls / delta_calls) after
    the connector has driven the factory. Used instead of attaching an
    attribute to a plain function — that pattern breaks mypy because
    function objects don't statically expose attributes.
    """

    def __init__(self, pages: list[CalendarDeltaPage]) -> None:
        self.client = _RecordingClient(pages)

    def __call__(self, _c: M365CalendarConfig) -> _RecordingClient:
        return self.client


def _factory_for(pages: list[CalendarDeltaPage]) -> _CapturingFactory:
    """Build a capturing factory wrapping a fresh _RecordingClient."""
    return _CapturingFactory(pages)


def _fixed_clock() -> datetime:
    """Deterministic clock anchored at 2026-05-22T00:00:00Z.

    Used so the date-window the connector requests is stable across
    runs — the unit tests can then assert exact start/end ISO values.
    """
    return datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)


_INITIAL_RECOVERY_PAYLOAD: dict[str, Any] = {
    "value": [
        {
            "id": "event-recovered",
            "subject": "Recovered",
            "start": {"dateTime": "2026-05-25T09:00:00Z"},
            "end": {"dateTime": "2026-05-25T10:00:00Z"},
            "location": {"displayName": "Remote"},
            "attendees": [],
            "organizer": {"emailAddress": {"address": "operator@example.com"}},
            "isCancelled": False,
            "lastModifiedDateTime": "2026-05-25T08:00:00Z",
        }
    ]
}


def _real_calendar_connector_for_http(handler: Any) -> M365CalendarConnector:
    """Compose the real connector and Graph client at the HTTP boundary."""
    from kairix.connectors.m365_calendar.auth import OAuth2ClientCredsAuth, OAuth2Config

    auth = OAuth2ClientCredsAuth(
        OAuth2Config(
            tenant_id="placeholder-tenant",
            client_id="placeholder-client",
            client_secret="placeholder-secret",  # pragma: allowlist secret
        ),
        token_fetcher=lambda _config: ("scripted-token", 3600.0),
        clock=lambda: 0.0,
    )
    http = httpx.Client(transport=httpx.MockTransport(handler), auth=auth)
    graph = M365GraphCalendarClient(
        user_id="operator@example.com",
        auth=auth,
        http_client=http,
        sleep_fn=lambda _seconds: None,
    )
    return M365CalendarConnector(
        _config(),
        client_factory=lambda _config: graph,
        per_user_client_factory=lambda _config, _upn: graph,
        clock=_fixed_clock,
    )


# ---------------------------------------------------------------------------
# First-sync date-window query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_sync_emits_created_for_each_event() -> None:
    """Empty cursor → connector queries calendarView/delta with date window
    and emits one ``created`` ChangeEvent per scripted event.

    Sabotage-proof: change :meth:`_record_to_change_event` to always
    return None for non-removed events; this test fails because no
    events surface.
    """
    factory = _factory_for([_page(_event("ev-alpha"), _event("ev-bravo"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [
        ("created", "ev-alpha"),
        ("created", "ev-bravo"),
    ]
    # Initial-delta endpoint, not the delta-page endpoint.
    assert factory.client.initial_calls, "expected the initial-delta endpoint to fire on first sync"
    assert factory.client.delta_calls == [], "first sync must not call the delta-page endpoint"


@pytest.mark.unit
def test_first_sync_uses_configured_date_window() -> None:
    """The initial date-window respects window_days_back / window_days_forward.

    Sabotage-proof: change the connector to ignore ``window_days_back``;
    this test fails because the start_iso then no longer matches the
    expected 7-day window.
    """
    config = M365CalendarConfig(
        user_id="operator@example.com",
        tenant_id="placeholder-tenant",
        client_id="placeholder-client",
        client_secret="placeholder-secret",  # pragma: allowlist secret
        window_days_back=7,
        window_days_forward=30,
    )
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(config, client_factory=factory, clock=_fixed_clock)

    list(connector.list_changes(cursor=None))

    start_iso, end_iso = factory.client.initial_calls[0]
    # 2026-05-22T00:00:00Z minus 7 days = 2026-05-15T00:00:00Z
    assert start_iso == "2026-05-15T00:00:00Z", f"unexpected window start: {start_iso!r}"
    # 2026-05-22T00:00:00Z plus 30 days = 2026-06-21T00:00:00Z
    assert end_iso == "2026-06-21T00:00:00Z", f"unexpected window end: {end_iso!r}"


@pytest.mark.unit
def test_first_sync_exposes_persisted_delta_link() -> None:
    """The connector exposes the Graph-returned delta link as the next cursor.

    Sabotage-proof: drop the delta_link capture in :meth:`_drain`;
    this test fails because ``last_delta_link`` stays ``None``.
    """
    factory = _factory_for([_page(_event("ev-alpha"), delta_link="cursor-after-first")])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    list(connector.list_changes(cursor=None))

    assert connector.last_delta_link == "cursor-after-first"


# ---------------------------------------------------------------------------
# Delta-cursor follow-up query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delta_cursor_drives_delta_page_endpoint() -> None:
    """Non-None cursor → connector calls fetch_delta_page, not initial.

    Sabotage-proof: swap the if/else in :meth:`_fetch_first_page`;
    this test fails because the initial-delta endpoint then fires.
    """
    factory = _factory_for([_page(_event("ev-charlie"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    list(connector.list_changes(cursor="cursor-from-previous-tick"))

    assert factory.client.delta_calls == ["cursor-from-previous-tick"]
    assert factory.client.initial_calls == [], "delta-cursor sync must not call the initial-delta endpoint"


@pytest.mark.contract
def test_expired_stored_calendar_cursor_restarts_once_from_initial_window() -> None:
    """Only a stored 410 cursor is discarded; the initial resync completes."""
    requested: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "expired-calendar-delta" in str(request.url):
            return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})
        return httpx.Response(
            200,
            request=request,
            json={**_INITIAL_RECOVERY_PAYLOAD, "@odata.deltaLink": "fresh"},
        )

    connector = _real_calendar_connector_for_http(_handler)
    events = list(connector.list_changes(cursor="https://graph.microsoft.com/v1.0/expired-calendar-delta"))

    assert [event.item_id for event in events] == ["event-recovered"]
    assert len(requested) == 2
    assert "expired-calendar-delta" in requested[0]
    assert "calendarView/delta" in requested[1]
    assert connector.last_delta_link == "fresh"


@pytest.mark.contract
def test_calendar_seed_410_is_not_retried_forever() -> None:
    """A 410 from the initial window is terminal after one request."""
    requested: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})

    connector = _real_calendar_connector_for_http(_handler)
    with pytest.raises(GraphDeltaExpiredError):
        list(connector.list_changes(cursor=None))

    assert len(requested) == 1


@pytest.mark.contract
def test_expired_per_container_calendar_cursor_restarts_only_that_container() -> None:
    """The production per-container path recovers its own expired cursor once."""
    requested: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "expired-container-delta" in str(request.url):
            return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})
        return httpx.Response(
            200,
            request=request,
            json={**_INITIAL_RECOVERY_PAYLOAD, "@odata.deltaLink": "fresh-container"},
        )

    connector = _real_calendar_connector_for_http(_handler)
    container = Container(
        cc_pair_id=7,
        container_id="operator@example.com",
        access_state="ACCESSIBLE",
        cursor_token="https://graph.microsoft.com/v1.0/expired-container-delta",
        last_synced_at=None,
    )

    events = list(connector.list_changes_for_container(container))

    assert [event.item_id for event in events] == ["event-recovered"]
    assert len(requested) == 2
    assert "expired-container-delta" in requested[0]
    assert "calendarView/delta" in requested[1]
    assert connector.next_cursor() == "fresh-container"


@pytest.mark.contract
def test_per_container_calendar_seed_410_fails_after_one_request() -> None:
    """A per-container initial-window 410 is terminal and never loops."""
    requested: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})

    connector = _real_calendar_connector_for_http(_handler)
    container = Container(
        cc_pair_id=7,
        container_id="operator@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )

    with pytest.raises(GraphDeltaExpiredError):
        list(connector.list_changes_for_container(container))

    assert len(requested) == 1


@pytest.mark.unit
def test_repeated_event_id_surfaces_as_modified() -> None:
    """An id the connector has already emitted as ``created`` surfaces as ``modified``.

    Sabotage-proof: drop the ``_known_ids`` membership check; this
    test fails because the second emission is then classified as
    ``created`` instead of ``modified``.
    """
    factory = _factory_for(
        [
            _page(_event("ev-alpha"), delta_link="cursor-1"),
            _page(_event("ev-alpha"), delta_link="cursor-2"),
        ]
    )
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    first = list(connector.list_changes(cursor=None))
    second = list(connector.list_changes(cursor="cursor-1"))

    assert [(e.op, e.item_id) for e in first] == [("created", "ev-alpha")]
    assert [(e.op, e.item_id) for e in second] == [("modified", "ev-alpha")]


@pytest.mark.unit
def test_seed_known_ids_marks_first_emission_as_modified() -> None:
    """The :meth:`seed_known_ids` seam pre-populates the known-id set.

    Sabotage-proof: stub :meth:`seed_known_ids` to a no-op; this test
    fails because the first emission is then classified as ``created``.
    """
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)
    connector.seed_known_ids({"ev-alpha"})

    events = list(connector.list_changes(cursor="resuming"))

    assert [(e.op, e.item_id) for e in events] == [("modified", "ev-alpha")]


# ---------------------------------------------------------------------------
# Cancelled + tombstoned events
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cancelled_event_surfaces_as_deleted() -> None:
    """An event with ``isCancelled: true`` surfaces as a ``deleted`` ChangeEvent.

    Sabotage-proof: drop the cancelled-branch in
    :meth:`_record_to_change_event`; this test fails because the
    event is then classified as ``created`` / ``modified``.
    """
    factory = _factory_for([_page(_event("ev-alpha", cancelled=True))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "ev-alpha")]


@pytest.mark.unit
def test_removed_event_surfaces_as_deleted() -> None:
    """A tombstoned (@removed) event surfaces as a ``deleted`` ChangeEvent.

    Sabotage-proof: drop the removed-branch in
    :meth:`_record_to_change_event`; this test fails because the
    tombstone is then dropped silently.
    """
    factory = _factory_for([_page(_event("ev-alpha", removed=True))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    events = list(connector.list_changes(cursor=None))

    assert [(e.op, e.item_id) for e in events] == [("deleted", "ev-alpha")]


# ---------------------------------------------------------------------------
# source_link, fetch, sensitivity_for
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_source_link_returns_outlook_deep_link() -> None:
    """``source_link`` returns ``https://outlook.office.com/calendar/item/<id>``.

    Sabotage-proof: replace the URL template with ``""``; this test
    fails on the substring assertions below.
    """
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    link = connector.source_link("ev-alpha")

    assert link == "https://outlook.office.com/calendar/item/ev-alpha"


@pytest.mark.unit
def test_fetch_returns_cached_payload_after_list_changes() -> None:
    """``fetch`` returns the Graph payload cached during list_changes.

    Sabotage-proof: drop the payload-caching line in :meth:`_drain`;
    this test fails because ``fetch`` then raises with the
    'no cached payload' message.
    """
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    list(connector.list_changes(cursor=None))
    artefact = connector.fetch("ev-alpha")

    assert isinstance(artefact, RawArtefact)
    assert artefact.mime == "application/json"
    assert b"ev-alpha" in artefact.raw


@pytest.mark.unit
def test_fetch_rejects_unseen_event_id() -> None:
    """``fetch`` raises when called for an id never seen by list_changes.

    Sabotage-proof: drop the cache-miss guard; this test fails because
    ``fetch`` silently returns an empty-payload artefact.
    """
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)
    list(connector.list_changes(cursor=None))

    with pytest.raises(ValueError, match="no cached payload"):
        connector.fetch("ev-not-seen")


@pytest.mark.unit
def test_sensitivity_for_returns_configured_tier() -> None:
    """Constructor's ``sensitivity`` value applies to every item.

    Sabotage-proof: hard-code the return to ``"public"``; this test
    fails because the constructor configured ``"client-confidential"``.
    """
    config = M365CalendarConfig(
        user_id="operator@example.com",
        tenant_id="placeholder-tenant",
        client_id="placeholder-client",
        client_secret="placeholder-secret",  # pragma: allowlist secret
        sensitivity="client-confidential",
    )
    factory = _factory_for([_page(_event("ev-alpha"))])
    connector = M365CalendarConnector(config, client_factory=factory, clock=_fixed_clock)

    assert connector.sensitivity_for("ev-alpha") == "client-confidential"


# ---------------------------------------------------------------------------
# make_connector — config validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_connector_requires_user_id() -> None:
    """``make_connector`` raises ValueError when ``user_id`` is missing.

    Per #378, only ``user_id`` is required in YAML; the OAuth triple
    resolves via the connector's injected :class:`SecretsResolver`.
    Sabotage-proof: drop the ``if not isinstance(user_id, str)`` guard
    at the top of ``make_connector``; this test fails because no
    exception is raised.
    """
    with pytest.raises(ValueError, match="user_id"):
        make_connector({})


@pytest.mark.unit
def test_connector_resolves_secrets_via_injected_loader() -> None:
    """:class:`M365CalendarConnector` resolves tenant_id / client_id /
    client_secret via the injected :class:`SecretsResolver` against the
    canonical ``(connector, m365, None, <leaf>)`` identities — same
    triple as the ``m365_email_headers`` sibling per KP-2.

    The config carries only ``user_id`` (the bug #378 shape: YAML no
    longer required the inline credential triple). The connector's
    ``__init__`` reaches the loader for each empty credential leaf and
    fills the resolved :class:`M365CalendarConfig` in place.

    Sabotage-proof: drop the ``secrets.require(...)`` calls in
    ``_resolve_config_credentials`` and hard-code ``""`` — this test
    then fails because the loader's recorded ``get_calls`` no longer
    contains the three canonical identity tuples.
    """
    loader = _full_loader()
    config = M365CalendarConfig(user_id="operator@example.com")

    connector = M365CalendarConnector(
        config,
        client_factory=_factory_for([_page(_event("ev-alpha"))]),
        secrets=loader,
    )

    assert connector.name == "m365_calendar"
    assert connector.sensitivity_for("any-id") == "internal"
    expected: set[tuple[str, str, str | None, str]] = {
        ("connector", "m365", None, "tenant-id"),
        ("connector", "m365", None, "client-id"),
        ("connector", "m365", None, "client-secret"),
    }
    recorded = set(loader.get_calls)
    assert expected.issubset(recorded), (
        f"connector must call loader.require for each canonical M365 leaf; missing={expected - recorded}"
    )


@pytest.mark.unit
def test_connector_inline_client_secret_overrides_loader_value() -> None:
    """Inline ``client_secret`` on the :class:`M365CalendarConfig` wins
    over the loader-resolved value.

    Operators with a dedicated per-connector M365 AAD app can pin a
    specific client_secret inline; the loader call is then skipped for
    that leaf and the inline override propagates into the resolved
    :class:`M365CalendarConfig`.

    Sabotage-proof: drop the ``config.client_secret or`` short-circuit
    in ``_resolve_config_credentials`` (force every leaf through
    ``secrets.require``) — this test then fails because the resolved
    config carries the loader's ``"loader-secret-value"`` instead of
    the inline ``"inline-override-secret"``.
    """
    loader = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "loader-tenant",
            ("connector", "m365", None, "client-id"): "loader-client",
            ("connector", "m365", None, "client-secret"): "loader-secret-value",
        }
    )
    config = M365CalendarConfig(
        user_id="operator@example.com",
        client_secret="inline-override-secret",  # pragma: allowlist secret
    )

    connector = M365CalendarConnector(
        config,
        client_factory=_factory_for([_page(_event("ev-alpha"))]),
        secrets=loader,
    )

    # Reach the resolved config via a single private-attribute read.
    # The brief explicitly asks for an inline-override-wins test; the
    # resolved client_secret is otherwise observable only at OAuth
    # request time. Keeping the assertion at the config boundary
    # avoids the auth round-trip in a unit test.
    resolved: M365CalendarConfig = connector._config
    assert resolved.client_secret == "inline-override-secret", (  # pragma: allowlist secret — test fixture string
        f"inline override must win over loader value; got {resolved.client_secret!r}"
    )
    # The loader was NOT asked for the secret leaf — only the other two
    # canonical leaves that still defer to the resolver.
    assert ("connector", "m365", None, "client-secret") not in loader.get_calls, (
        f"loader was asked for client-secret despite inline override; calls={loader.get_calls!r}"
    )
    assert ("connector", "m365", None, "tenant-id") in loader.get_calls
    assert ("connector", "m365", None, "client-id") in loader.get_calls


@pytest.mark.unit
def test_connector_raises_when_loader_misses_and_no_inline_override() -> None:
    """A missing canonical leaf with no inline override raises
    :class:`SecretNotFoundError` from the loader's ``require`` call.

    Pins the F68 failure-injection contract: when the
    :class:`SecretsResolver` returns ``None`` for any of the three
    canonical M365 leaves, the connector's ``__init__`` surfaces the
    loader's typed error rather than silently constructing a broken
    connector. The error message already carries the
    ``fix:`` / ``next:`` / ``run:`` F21 markers from
    :meth:`SecretsLoader.require`.

    Sabotage-proof: change ``_resolve_config_credentials`` to fall back
    to an empty string on a loader miss — this test fails because the
    connector then constructs instead of raising.
    """
    # Loader supplies only tenant + client; client-secret is missing.
    partial_loader = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "fake-tenant",
            ("connector", "m365", None, "client-id"): "fake-client",
        }
    )
    config = M365CalendarConfig(user_id="operator@example.com")

    with pytest.raises(SecretNotFoundError) as exc_info:
        M365CalendarConnector(config, secrets=partial_loader)
    msg = str(exc_info.value)
    assert "client-secret" in msg, f"error message must name the missing leaf: {msg!r}"


# ---------------------------------------------------------------------------
# ChangeEvent metadata fidelity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolved_config_is_concrete_m365_calendar_config_subtype() -> None:
    """The connector's resolved config object is a concrete :class:`M365CalendarConfig`.

    Pins the python:S5886 fix that swapped :func:`dataclasses.replace`
    for a field-by-field constructor call inside the credential
    resolver. :func:`replace` returns the broader
    ``DataclassInstance`` (Sonar flagged); the explicit constructor
    returns the concrete subtype callers downcast against.

    Drives the public :class:`M365CalendarConnector` constructor — the
    credential resolver runs during ``__init__`` and stores the result
    on the (existing-pattern-tested) ``_config`` field. F5-clean: no
    import of the underscore-prefixed resolver.

    Sabotage-proof: revert the resolver to use ``replace(config, ...)``;
    this test still passes at runtime (the object IS an
    M365CalendarConfig either way) BUT Sonar fires S5886 on the
    function. The mechanical sabotage proof for the annotation-level
    claim is mypy + Sonar; this test pins the runtime invariant the
    type hint advertises AND that every config field round-trips
    through the field-by-field copy.
    """
    loader = _full_loader()
    incoming = M365CalendarConfig(user_id="operator@example.com")
    connector = M365CalendarConnector(
        incoming,
        client_factory=_factory_for([_page(_event("ev-alpha"))]),
        secrets=loader,
    )
    resolved = connector._config

    assert isinstance(resolved, M365CalendarConfig), (
        f"resolved config must be M365CalendarConfig; got {type(resolved).__name__}"
    )
    # Field-by-field copy preserves every non-credential field unchanged.
    assert resolved.user_id == incoming.user_id
    assert resolved.scope == incoming.scope
    assert resolved.window_days_back == incoming.window_days_back
    assert resolved.window_days_forward == incoming.window_days_forward
    assert resolved.sensitivity == incoming.sensitivity
    assert resolved.user_ids == incoming.user_ids
    # Credential leaves filled from the loader.
    assert resolved.tenant_id == "fake-tenant"
    assert resolved.client_id == "fake-client"
    assert resolved.client_secret  # non-empty


@pytest.mark.unit
def test_change_event_metadata_carries_subject_attendees_location() -> None:
    """The ChangeEvent metadata exposes the fields downstream consumers need.

    Sabotage-proof: drop one of the metadata keys; this test fails on
    the corresponding assertion below.
    """
    factory = _factory_for([_page(_event("ev-alpha", subject="Customer review"))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    events = list(connector.list_changes(cursor=None))
    metadata: dict[str, Any] = dict(events[0].metadata)

    assert metadata["subject"] == "Customer review"
    assert metadata["start"] == "2026-05-25T09:00:00Z"
    assert metadata["end"] == "2026-05-25T10:00:00Z"
    assert metadata["location"] == "Conference room"
    assert metadata["attendees"] == ("alpha@example.com",)
    assert metadata["organiser"] == "organiser@example.com"


# ---------------------------------------------------------------------------
# typed ChangeEvent shape — defends against bare-dict regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_changes_emits_only_typed_change_events() -> None:
    """All emitted values are :class:`ChangeEvent` instances per F42.

    Sabotage-proof: have :meth:`_record_to_change_event` return a
    plain dict; this test fails because the isinstance check rejects
    the dict.
    """
    factory = _factory_for([_page(_event("ev-alpha"), _event("ev-bravo", cancelled=True))])
    connector = M365CalendarConnector(_config(), client_factory=factory, clock=_fixed_clock)

    events = list(connector.list_changes(cursor=None))

    for ev in events:
        assert isinstance(ev, ChangeEvent), f"non-ChangeEvent emitted: {ev!r}"


# ---------------------------------------------------------------------------
# Wave E production-default DI seams — coverage for the prod-only branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_connector_default_production_path_handles_single_calendar() -> None:
    """Drive ``make_connector`` (the public surface) with the canonical
    single-mailbox config and confirm the production-default Wave E
    surface emits exactly one Container per the operator's user_id.

    Sabotage-proof: change ``_configured_upns`` to return ``()`` for the
    single-mailbox case; the assertion below catches the regression.
    Tests the production-default factory paths through the public
    surface only (no internal-name imports per F5).
    """
    config_dict: dict[str, Any] = {
        "user_id": "operator@example.com",
        "tenant_id": "placeholder-tenant",
        "client_id": "placeholder-client",
        "client_secret": "placeholder-secret",  # pragma: allowlist secret
    }
    connector = make_connector(config_dict)
    # iter_containers is a public Wave E method — drives _configured_upns
    # through to the singleton-from-user_id fallback path.
    containers = list(connector.iter_containers(cc_pair_id=7))
    assert [c.container_id for c in containers] == ["operator@example.com"]
    # load_hierarchy is a public Wave E method — drives the production
    # hierarchy emission with the default flag-reader (resolves to False,
    # but the hierarchy emission is unflagged) and confirms the structural
    # shape (root + one calendar child).
    nodes = list(connector.load_hierarchy(cc_pair_id=7))
    assert len(nodes) == 2
    assert nodes[0].raw_node_id == "m365-calendar"
    assert nodes[0].raw_parent_id is None
    assert nodes[1].raw_node_id == "operator@example.com"
    assert nodes[1].raw_parent_id == "m365-calendar"


@pytest.mark.unit
def test_close_releases_every_per_upn_graph_client() -> None:
    """``close()`` releases both the legacy client and every per-UPN client.

    Drives the full lifecycle through public surface: construct the
    connector with Wave E flag ON, drain
    :meth:`list_changes_for_container` for two distinct UPNs (which
    causes the per-UPN factory to build two clients), call
    :meth:`close`, and assert the scripted clients all observed their
    ``close()`` call. Sabotage-proof: remove the ``for client in
    self._per_user_clients`` loop in :meth:`close`; the second-client
    assertion below catches the regression.
    """
    from kairix.core.protocols import Container

    closed: dict[str, bool] = {}

    class _ScriptedClient(M365GraphCalendarClient):
        def __init__(self, upn: str) -> None:
            self._user_id = upn
            self._http = None  # type: ignore[assignment]  # scripted client never makes HTTP calls
            self._page_size = 50
            self._upn = upn
            closed[upn] = False

        def fetch_initial_delta(self, _s: str, _e: str) -> CalendarDeltaPage:
            return CalendarDeltaPage(events=(), next_link=None, delta_link="dl")

        def fetch_delta_page(self, link: str) -> CalendarDeltaPage:
            return CalendarDeltaPage(events=(), next_link=None, delta_link=link)

        def close(self) -> None:
            closed[self._upn] = True

    config = M365CalendarConfig(
        user_id="alice@example.com",
        tenant_id="t",
        client_id="c",
        client_secret="s",  # pragma: allowlist secret
        user_ids=("alice@example.com", "bob@example.com"),
    )
    connector = M365CalendarConnector(
        config,
        per_user_client_factory=lambda _c, upn: _ScriptedClient(upn),
    )
    for upn in ("alice@example.com", "bob@example.com"):
        container = Container(
            cc_pair_id=1,
            container_id=upn,
            access_state="ACCESSIBLE",
            cursor_token=None,
            last_synced_at=None,
        )
        list(connector.list_changes_for_container(container))

    # Pre-close: every UPN's scripted client has fired __init__ but not
    # close() yet.
    assert closed == {"alice@example.com": False, "bob@example.com": False}
    connector.close()
    # Post-close: every UPN's scripted client received its close call.
    assert closed == {"alice@example.com": True, "bob@example.com": True}


@pytest.mark.unit
def test_make_connector_accepts_user_ids_for_multi_calendar() -> None:
    """``make_connector`` threads ``user_ids`` through to the config.

    Sabotage-proof: drop the ``user_ids=`` kwarg from the resolved
    config; this test fails because iter_containers then emits one
    Container instead of two.
    """
    config: dict[str, Any] = {
        "user_id": "alice@example.com",
        "tenant_id": "placeholder-tenant",
        "client_id": "placeholder-client",
        "client_secret": "placeholder-secret",  # pragma: allowlist secret
        "user_ids": ("alice@example.com", "bob@example.com"),
    }
    connector = make_connector(config)
    containers = list(connector.iter_containers(cc_pair_id=1))
    assert [c.container_id for c in containers] == ["alice@example.com", "bob@example.com"]


# ---------------------------------------------------------------------------
# v2 per-container list_changes_for_container — events emit + payloads cached
# Phase C (#132) retired the legacy flag-OFF tests; the existing close-test
# verifies cleanup but never sends events through the per-container loop.
# This pins the events-loop + payload-cache behaviour (lines 754-772).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_changes_for_container_emits_per_event_and_caches_payload() -> None:
    """v2 per-UPN ``list_changes_for_container`` emits one ChangeEvent per
    non-removed record and stores the raw payload on the per-event cache.

    The per-event payload cache (``_event_payload_cache``) is what
    ``fetch`` later reads to construct the RawArtefact — without it, a
    fetched item would have to re-hit Graph for the body. This test
    confirms (a) every non-removed record surfaces as a ChangeEvent
    keyed by the event_id, and (b) the raw_payload is parked in the
    cache for downstream fetch().

    Sabotage-proof: remove the ``if not record.removed:
    self._cache_payload(...)`` block; this test fails because the
    cache stays empty after the events drain.
    """
    from kairix.core.protocols import Container

    class _ScriptedClient(M365GraphCalendarClient):
        def __init__(self, upn: str) -> None:
            self._user_id = upn
            self._http = None  # type: ignore[assignment]  # scripted client never makes HTTP calls
            self._page_size = 50
            self._upn = upn

        def fetch_initial_delta(self, _start: str, _end: str) -> CalendarDeltaPage:
            return CalendarDeltaPage(
                events=(
                    CalendarEventRecord(
                        event_id="evt-keep-1",
                        subject="Sync planning",
                        start_iso="2026-06-10T09:00:00Z",
                        end_iso="2026-06-10T10:00:00Z",
                        location="Online",
                        attendees=("agent-alpha@example.com",),
                        organiser="agent-beta@example.com",
                        last_modified_iso="2026-06-09T12:00:00Z",
                        cancelled=False,
                        removed=False,
                        raw_payload='{"id": "evt-keep-1", "subject": "Sync planning"}',
                    ),
                    CalendarEventRecord(
                        event_id="evt-keep-2",
                        subject="Design review",
                        start_iso="2026-06-11T14:00:00Z",
                        end_iso="2026-06-11T15:00:00Z",
                        location="Room 4",
                        attendees=(),
                        organiser="agent-beta@example.com",
                        last_modified_iso="2026-06-09T13:00:00Z",
                        cancelled=False,
                        removed=False,
                        raw_payload='{"id": "evt-keep-2", "subject": "Design review"}',
                    ),
                ),
                next_link=None,
                delta_link="dl-after-2-events",
            )

        def fetch_delta_page(self, link: str) -> CalendarDeltaPage:  # pragma: no cover  # scripted single-page
            return CalendarDeltaPage(events=(), next_link=None, delta_link=link)

        def close(self) -> None:
            return None

    config = M365CalendarConfig(
        user_id="agent-alpha@example.com",
        tenant_id="t",
        client_id="c",
        client_secret="s",  # pragma: allowlist secret
        user_ids=("agent-alpha@example.com",),
    )
    connector = M365CalendarConnector(
        config,
        per_user_client_factory=lambda _c, upn: _ScriptedClient(upn),
    )
    container = Container(
        cc_pair_id=1,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    events = list(connector.list_changes_for_container(container))
    # Both records emit as ChangeEvent (neither removed nor cancelled).
    event_ids = sorted(e.item_id for e in events)
    assert event_ids == ["evt-keep-1", "evt-keep-2"], (
        f"non-removed records must emit per-event ChangeEvents; got {event_ids!r}"
    )
    # Per-event payload cache populated for fetch() downstream.
    cache = getattr(connector, "_event_payload_cache", {})
    assert "evt-keep-1" in cache, f"raw_payload must be cached; cache keys: {list(cache)!r}"
    assert "evt-keep-2" in cache
    assert '"evt-keep-1"' in cache["evt-keep-1"]


@pytest.mark.unit
def test_list_changes_for_container_delta_link_resume_uses_existing_cursor() -> None:
    """When a Container carries a cursor_token, the connector resumes via
    ``fetch_delta_page(link)`` instead of fetching an initial delta window.

    Pins the v2 per-container cursor-resume semantics — the cursor is
    the operator-persisted handle from the previous sync tick, so
    each per-UPN cc_pair gets its own delta horizon. Without this branch
    the connector would re-emit every event in the last window on every
    tick.

    Sabotage-proof: change ``client.fetch_delta_page(cursor)`` to
    ``client.fetch_initial_delta(...)``; this test fails because the
    scripted client records the wrong call.
    """
    from kairix.core.protocols import Container

    calls: dict[str, list[str]] = {"initial": [], "delta_page": []}

    class _RecordingClient(M365GraphCalendarClient):
        def __init__(self, upn: str) -> None:
            self._user_id = upn
            self._http = None  # type: ignore[assignment]  # scripted client never makes HTTP calls
            self._page_size = 50
            self._upn = upn

        def fetch_initial_delta(self, start: str, end: str) -> CalendarDeltaPage:
            calls["initial"].append(f"{start}|{end}")
            return CalendarDeltaPage(events=(), next_link=None, delta_link="dl-initial")

        def fetch_delta_page(self, link: str) -> CalendarDeltaPage:
            calls["delta_page"].append(link)
            return CalendarDeltaPage(events=(), next_link=None, delta_link="dl-resumed")

        def close(self) -> None:
            return None

    config = M365CalendarConfig(
        user_id="agent-alpha@example.com",
        tenant_id="t",
        client_id="c",
        client_secret="s",  # pragma: allowlist secret
        user_ids=("agent-alpha@example.com",),
    )
    connector = M365CalendarConnector(
        config,
        per_user_client_factory=lambda _c, upn: _RecordingClient(upn),
    )
    container = Container(
        cc_pair_id=1,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token="dl-prior-tick",
        last_synced_at=None,
    )
    list(connector.list_changes_for_container(container))
    assert calls["delta_page"] == ["dl-prior-tick"], f"cursor_token must drive fetch_delta_page resume; got {calls!r}"
    assert calls["initial"] == [], "initial delta must NOT fire when a cursor is present"
