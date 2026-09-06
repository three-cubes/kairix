"""Unit tests for :mod:`kairix.connectors.m365_calendar.graph_client`.

Drives the wrapper against ``httpx.MockTransport`` so the Graph
payload-parsing logic is exercised end-to-end (request shape +
response decoding) without any real network I/O.

F1-clean (no monkey-patching), F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kairix.connectors.m365_calendar.auth import OAuth2ClientCredsAuth, OAuth2Config
from kairix.connectors.m365_calendar.graph_client import (
    GRAPH_BASE_URL,
    CalendarDeltaPage,
    CalendarEventRecord,
    M365GraphCalendarClient,
    iter_pages,
)
from kairix.transport.errors import GraphDeltaExpiredError


def _stub_auth() -> OAuth2ClientCredsAuth:
    """Build an auth instance that returns a scripted token, no network."""
    return OAuth2ClientCredsAuth(
        OAuth2Config(
            tenant_id="placeholder-tenant",
            client_id="placeholder-client",
            client_secret="placeholder-secret",  # pragma: allowlist secret
        ),
        token_fetcher=lambda _c: ("scripted-token", 3600.0),
        clock=lambda: 0.0,
    )


def _client_with_handler(handler: Any) -> M365GraphCalendarClient:
    """Build a client whose underlying httpx.Client uses MockTransport."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, auth=_stub_auth())
    return M365GraphCalendarClient(user_id="operator@example.com", auth=_stub_auth(), http_client=http)


_INITIAL_PAYLOAD: dict[str, Any] = {
    "value": [
        {
            "id": "event-alpha",
            "subject": "Team sync",
            "start": {"dateTime": "2026-05-25T09:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-25T10:00:00Z", "timeZone": "UTC"},
            "location": {"displayName": "Conference room"},
            "attendees": [
                {"emailAddress": {"address": "alpha@example.com", "name": "Alpha"}},
                {"emailAddress": {"address": "beta@example.com", "name": "Beta"}},
            ],
            "organizer": {"emailAddress": {"address": "organiser@example.com", "name": "Organiser"}},
            "isCancelled": False,
            "lastModifiedDateTime": "2026-05-25T08:00:00Z",
        },
    ],
    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/.../$deltatoken=initial",
}


@pytest.mark.unit
def test_fetch_initial_delta_targets_calendarview_delta() -> None:
    """The initial-delta call hits /users/<id>/calendar/calendarView/delta.

    Sabotage-proof: change the endpoint string to ``/events``; this
    test fails because the captured URL no longer contains
    ``calendarView/delta``.
    """
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_INITIAL_PAYLOAD)

    client = _client_with_handler(_handler)
    page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")

    assert isinstance(page, CalendarDeltaPage)
    assert len(captured) == 1
    url = str(captured[0].url)
    assert url.startswith(f"{GRAPH_BASE_URL}/users/operator@example.com/calendar/calendarView/delta")
    assert "startDateTime=2026-05-01T00%3A00%3A00Z" in url
    assert "endDateTime=2026-06-01T00%3A00%3A00Z" in url


@pytest.mark.unit
def test_fetch_initial_delta_parses_event_fields() -> None:
    """Graph payload fields land on the typed :class:`CalendarEventRecord`.

    Sabotage-proof: drop one of the field accessors in
    :func:`_record_from_graph_event`; this test fails on the
    corresponding assertion below.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_INITIAL_PAYLOAD)

    client = _client_with_handler(_handler)
    page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")

    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_id == "event-alpha"
    assert event.subject == "Team sync"
    assert event.start_iso == "2026-05-25T09:00:00Z"
    assert event.end_iso == "2026-05-25T10:00:00Z"
    assert event.location == "Conference room"
    assert event.attendees == ("alpha@example.com", "beta@example.com")
    assert event.organiser == "organiser@example.com"
    assert event.cancelled is False
    assert event.removed is False
    assert page.delta_link == "https://graph.microsoft.com/v1.0/.../$deltatoken=initial"
    assert page.next_link is None


@pytest.mark.unit
def test_cancelled_event_carries_cancelled_flag() -> None:
    """``isCancelled: true`` lands on :attr:`CalendarEventRecord.cancelled`.

    Sabotage-proof: hard-code ``cancelled=False``; this test fails.
    """
    payload = {
        "value": [
            {
                "id": "event-zulu",
                "subject": "Cancelled meeting",
                "start": {"dateTime": "2026-05-25T09:00:00Z"},
                "end": {"dateTime": "2026-05-25T10:00:00Z"},
                "location": {"displayName": "Conference room"},
                "attendees": [],
                "organizer": {"emailAddress": {"address": "organiser@example.com"}},
                "isCancelled": True,
                "lastModifiedDateTime": "2026-05-25T08:00:00Z",
            }
        ],
        "@odata.deltaLink": "delta-link",
    }

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_handler(_handler)
    page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")

    assert page.events[0].cancelled is True


@pytest.mark.unit
def test_removed_tombstone_surfaces_as_removed_record() -> None:
    """OData ``@removed`` tombstones land on :attr:`CalendarEventRecord.removed`.

    Sabotage-proof: drop the ``@removed`` branch in
    :func:`_record_from_graph_event`; this test fails because the
    tombstone is then decoded as a full (empty) event.
    """
    payload = {
        "value": [
            {"id": "event-tombstone", "@removed": {"reason": "deleted"}},
        ],
        "@odata.deltaLink": "delta-link",
    }

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_handler(_handler)
    page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")

    record = page.events[0]
    assert record.removed is True
    assert record.event_id == "event-tombstone"


@pytest.mark.unit
def test_next_link_walks_subsequent_pages() -> None:
    """A ``@odata.nextLink`` walk pulls the follow-up page.

    Sabotage-proof: drop the ``next_link`` branch in
    :func:`_parse_delta_response`; this test fails because the
    iterator then stops on page 1.
    """
    page_one: dict[str, Any] = {
        "value": [{"id": "ev-1"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
    }
    page_two: dict[str, Any] = {
        "value": [{"id": "ev-2"}],
        "@odata.deltaLink": "final-delta-link",
    }
    responses = iter([page_one, page_two])

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    client = _client_with_handler(_handler)
    first = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")
    pages = list(iter_pages(client, first))

    assert len(pages) == 2
    assert [r.event_id for r in pages[0].events] == ["ev-1"]
    assert [r.event_id for r in pages[1].events] == ["ev-2"]
    assert pages[-1].delta_link == "final-delta-link"


@pytest.mark.unit
def test_fetch_delta_page_uses_provided_url() -> None:
    """The delta-page entry point dispatches against the supplied link.

    Sabotage-proof: hard-code the URL inside ``fetch_delta_page``; this
    test fails because the captured URL no longer matches the input.
    """
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": "final"})

    client = _client_with_handler(_handler)
    follow_up = "https://graph.microsoft.com/v1.0/users/u/calendar/calendarView/delta?$skiptoken=abc"
    client.fetch_delta_page(follow_up)

    assert captured == [follow_up]


@pytest.mark.unit
def test_client_close_idempotent() -> None:
    """Calling :meth:`close` is safe and idempotent.

    Sabotage-proof: raise on double-close; this test fails on the
    second call.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    client = _client_with_handler(_handler)
    client.close()
    client.close()


@pytest.mark.unit
def test_client_context_manager_closes() -> None:
    """The context-manager protocol closes the underlying client on exit.

    Sabotage-proof: stub :meth:`__exit__` to no-op; this test fails
    because the second exit-then-call below would still succeed.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    with _client_with_handler(_handler) as client:
        page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")
        assert isinstance(page, CalendarDeltaPage)


@pytest.mark.unit
def test_missing_optional_fields_default_to_empty() -> None:
    """Missing optional fields decode to the typed defaults.

    Graph payload schemas evolve over time; the parser must tolerate
    missing optional fields without raising.

    Sabotage-proof: drop one of the ``.get(...)`` calls and crash on
    KeyError; this test then fails on the parsing call.
    """
    payload = {
        "value": [
            {
                "id": "minimal-event",
                # subject, start, end, location, attendees, organizer all absent
            }
        ],
        "@odata.deltaLink": "delta-link",
    }

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_handler(_handler)
    page = client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")

    assert isinstance(page.events[0], CalendarEventRecord)
    assert page.events[0].event_id == "minimal-event"
    assert page.events[0].subject == ""
    assert page.events[0].attendees == ()
    assert page.events[0].organiser == ""
    assert page.events[0].location == ""


@pytest.mark.unit
def test_non_2xx_response_raises_httpx_error() -> None:
    """Graph returning 4xx / 5xx raises via raise_for_status.

    Sabotage-proof: drop the raise_for_status() call; this test fails
    because the call returns a malformed page instead of raising.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "Unauthorized"}})

    client = _client_with_handler(_handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_initial_delta("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z")


@pytest.mark.unit
def test_410_response_raises_typed_delta_expired_error() -> None:
    """A Graph 410 is distinguishable from every other HTTP failure."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            410,
            request=request,
            json={"error": {"code": "syncStateNotFound"}},
        )

    client = _client_with_handler(_handler)
    with pytest.raises(GraphDeltaExpiredError) as raised:
        client.fetch_delta_page("https://graph.microsoft.com/v1.0/expired-calendar-delta")

    assert raised.value.response.status_code == 410
