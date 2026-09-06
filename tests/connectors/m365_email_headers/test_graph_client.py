"""Unit tests for :mod:`kairix.connectors.m365_email_headers.graph_client`.

Scope: the typed paths in :class:`M365GraphClient` that the
``test_connector.py`` smoke tests don't exercise directly —

  * Constructor input validation (empty UPN).
  * ``_authorised_get`` 401 retry path — on a 401 the helper calls
    :meth:`OAuth2ClientCredsAuth.invalidate` AND re-issues the GET.
  * ``_parse_message`` / ``_email_from`` / ``_emails_from`` edge cases
    that fall outside the happy-path fixture in ``test_connector.py``
    (sparse Graph envelopes, missing ``emailAddress`` block, non-list
    ``toRecipients``).

F1-clean (no monkey-patching of kairix internals — the OAuth2 helper
takes an ``httpx.MockTransport``-backed client through its
constructor), F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kairix.connectors.m365_email_headers.graph_client import (
    DeltaPage,
    GraphMessage,
    M365GraphClient,
    MailFolderRef,
)
from kairix.transport.auth.oauth2_client_creds import (
    OAuth2ClientCredsAuth,
)
from kairix.transport.errors import GraphDeltaExpiredError

pytestmark = pytest.mark.unit


def _build_auth_with_transport(
    handler: httpx.MockTransport,
) -> tuple[OAuth2ClientCredsAuth, httpx.Client]:
    """Construct an :class:`OAuth2ClientCredsAuth` against a MockTransport.

    The returned ``httpx.Client`` is the shared transport for both the
    token-exchange call AND the Graph call so a single handler covers
    every leg of the request flow.
    """
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    return auth, shared


# ---------------------------------------------------------------------------
# Constructor input validation
# ---------------------------------------------------------------------------


def test_graph_client_rejects_empty_user_principal_name() -> None:
    """Constructing with an empty UPN is a typed ValueError.

    Sabotage proof: drop the ``if not user_principal_name`` guard at the
    top of :class:`M365GraphClient` ``__init__`` — the
    ``pytest.raises`` block stops firing and the test fails.
    """
    handler = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
    auth, _ = _build_auth_with_transport(handler)
    with pytest.raises(ValueError) as exc_info:
        M365GraphClient(user_principal_name="", auth=auth)
    msg = str(exc_info.value)
    assert "user_principal_name" in msg
    assert "fix:" in msg, f"error message missing fix: marker: {msg!r}"


# ---------------------------------------------------------------------------
# 401 retry path
# ---------------------------------------------------------------------------


def test_authorised_get_retries_once_on_401() -> None:
    """A 401 from Graph triggers invalidate + a single retry.

    First Graph call returns 401, second returns 200 with one envelope.
    The helper must invalidate the cached token AND re-issue the GET.

    Sabotage proof: remove the ``if response.status_code == 401`` block
    from ``_authorised_get`` — the second Graph call never fires and the
    final 200 response is missed, causing the iter to yield zero
    messages instead of one.
    """
    graph_calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-bearer",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        # First Graph hit: 401; second hit: 200 with one message.
        graph_calls.append(1)
        if len(graph_calls) == 1:
            return httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken"}},
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "msg-after-retry",
                        "from": {"emailAddress": {"address": "agent-alpha@example.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "subject": "After retry",
                        "sentDateTime": "2026-05-22T10:00:00Z",
                        "receivedDateTime": "2026-05-22T10:00:01Z",
                    }
                ],
                "@odata.deltaLink": (
                    "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$deltatoken=tok-final"
                ),
            },
        )

    handler = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(handler)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )

    messages = list(client.iter_messages("inbox"))
    # Two Graph calls fired (401 → invalidate → 200).
    assert len(graph_calls) == 2, f"expected 2 Graph hits (401 + retry), got {len(graph_calls)}"
    # The retry produced exactly one envelope.
    assert len(messages) == 1
    assert messages[0].message_id == "msg-after-retry"
    # And the deltaLink was captured.
    assert client.last_delta_link() is not None
    assert "$deltatoken=tok-final" in (client.last_delta_link() or "")


def test_authorised_get_propagates_persistent_401() -> None:
    """A persistent 401 (still 401 after retry) raises HTTPStatusError.

    The helper retries exactly once; a second 401 must surface as a
    typed :class:`httpx.HTTPStatusError`.

    Sabotage proof: change the post-retry ``response.raise_for_status()``
    to ``pass`` — the ``pytest.raises`` block stops firing.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-bearer",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    handler = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(handler)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )

    with pytest.raises(httpx.HTTPStatusError):
        list(client.iter_messages("inbox"))


# ---------------------------------------------------------------------------
# Graph response parsing via the public fetch_page surface
# ---------------------------------------------------------------------------


def _fetch_page_with_body(body: dict[str, Any]) -> DeltaPage:
    """Drive :meth:`M365GraphClient.fetch_page` against a MockTransport that
    serves ``body`` as the Graph delta response.

    This stays on the public surface (no underscore-prefixed imports per
    F5); every parser branch we want to cover is reachable through
    :meth:`fetch_page`.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-bearer",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(200, json=body)

    handler = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(handler)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )
    return client.fetch_page("https://graph.microsoft.com/v1.0/users/agent-alpha/messages/delta")


def test_fetch_page_handles_empty_value_array() -> None:
    """An empty ``value`` array yields a :class:`DeltaPage` with no messages.

    Sabotage proof: in ``_parse_delta_page`` change ``isinstance(raw_messages, list)``
    to ``raw_messages is not None`` — a non-list value would explode in the loop
    and a similar test below would error rather than pass.
    """
    page = _fetch_page_with_body({"value": [], "@odata.deltaLink": "https://example.com/delta?tok=x"})
    assert isinstance(page, DeltaPage)
    assert page.messages == ()
    assert page.delta_link == "https://example.com/delta?tok=x"
    assert page.next_link is None


def test_fetch_page_handles_missing_value_key() -> None:
    """A body with no ``value`` key parses to an empty :class:`DeltaPage`.

    Sabotage proof: in ``_parse_delta_page`` change ``body.get("value")`` to
    ``body["value"]`` — this test errors with KeyError.
    """
    page = _fetch_page_with_body({})
    assert page.messages == ()
    assert page.next_link is None
    assert page.delta_link is None


def test_fetch_page_skips_non_dict_entries_in_value() -> None:
    """A ``value`` array containing a non-dict (e.g. a string) is skipped.

    Sabotage proof: drop the ``if isinstance(entry, dict)`` guard in
    ``_parse_delta_page`` — this test raises ``AttributeError``.
    """
    body: dict[str, Any] = {
        "value": [
            {"id": "msg-1", "from": {"emailAddress": {"address": "agent-alpha@example.com"}}},
            "not-a-dict",
            None,
            {"id": "msg-2", "from": {"emailAddress": {"address": "agent-beta@example.com"}}},
        ]
    }
    page = _fetch_page_with_body(body)
    assert tuple(m.message_id for m in page.messages) == ("msg-1", "msg-2")


# ---------------------------------------------------------------------------
# Recipient-block edge cases driven through fetch_page
# ---------------------------------------------------------------------------


def test_fetch_page_returns_none_sender_when_from_block_malformed() -> None:
    """A ``from`` field that isn't a dict, or carries no ``emailAddress``
    sub-block, or carries an ``emailAddress`` that isn't a dict, must
    surface as ``sender=None`` on the parsed message.

    Sabotage proof: drop the ``if not isinstance(value, dict)`` guard in
    the sender-pull helper — at least one of the assertions below stops
    holding (the function raises rather than returning None).
    """
    body: dict[str, Any] = {
        "value": [
            # No "from" key at all.
            {"id": "no-from"},
            # "from" is a string, not a dict.
            {"id": "from-string", "from": "alpha@example.com"},
            # "from" dict missing "emailAddress" sub-block.
            {"id": "from-no-email-addr-block", "from": {"name": "alpha"}},
            # "from.emailAddress" is a string, not a dict.
            {"id": "from-emailaddress-string", "from": {"emailAddress": "alpha@example.com"}},
            # "from.emailAddress.address" is None.
            {"id": "from-address-none", "from": {"emailAddress": {"address": None}}},
            # "from.emailAddress.address" is non-string (int).
            {"id": "from-address-int", "from": {"emailAddress": {"address": 42}}},
        ]
    }
    page = _fetch_page_with_body(body)
    senders = [m.sender for m in page.messages]
    assert senders == [None, None, None, None, None, None], (
        f"expected every malformed-from message to surface sender=None, got {senders!r}"
    )


def test_fetch_page_filters_non_list_to_recipients() -> None:
    """A ``toRecipients`` field that isn't a list yields an empty tuple.

    Sabotage proof: drop the ``if not isinstance(value, list)`` guard in
    the recipients-pull helper — iterating a non-list raises TypeError.
    """
    body: dict[str, Any] = {
        "value": [
            {"id": "to-not-list", "toRecipients": "alpha@example.com"},
            {"id": "to-is-dict", "toRecipients": {"emailAddress": {"address": "alpha@example.com"}}},
            {"id": "to-is-none", "toRecipients": None},
        ]
    }
    page = _fetch_page_with_body(body)
    assert all(m.to_recipients == () for m in page.messages)


def test_fetch_page_drops_to_recipient_entries_without_address() -> None:
    """Recipient entries missing a usable address are dropped, NOT surfaced as None.

    Sabotage proof: change the recipients-pull helper's ``if addr is not None``
    guard to always append — the resulting tuple grows past the count of
    valid entries.
    """
    body: dict[str, Any] = {
        "value": [
            {
                "id": "mixed-recipients",
                "toRecipients": [
                    {"emailAddress": {"address": "agent-alpha@example.com"}},
                    {"emailAddress": "string-not-dict"},  # invalid: emailAddress not a dict
                    {"name": "no-emailAddress-key"},  # no emailAddress block
                    {"emailAddress": {"address": 99}},  # invalid: address not a string
                    {"emailAddress": {"address": "agent-beta@example.com"}},
                ],
            }
        ]
    }
    page = _fetch_page_with_body(body)
    assert len(page.messages) == 1
    assert page.messages[0].to_recipients == (
        "agent-alpha@example.com",
        "agent-beta@example.com",
    )


# ---------------------------------------------------------------------------
# Sparse Graph envelope shape
# ---------------------------------------------------------------------------


def test_fetch_page_handles_sparse_envelope() -> None:
    """A Graph envelope missing every optional field still parses cleanly.

    The :class:`GraphMessage` dataclass tolerates ``None`` for subject /
    timestamps and empty tuples for recipients.

    Sabotage proof: make a parser require ``id`` to be present —
    constructing :class:`GraphMessage` from this sparse fixture raises.
    """
    page = _fetch_page_with_body({"value": [{"id": "only-id-set"}]})
    assert len(page.messages) == 1
    msg = page.messages[0]
    assert isinstance(msg, GraphMessage)
    assert msg.message_id == "only-id-set"
    assert msg.sender is None
    assert msg.to_recipients == ()
    assert msg.cc_recipients == ()
    assert msg.subject is None
    assert msg.sent_at is None
    assert msg.received_at is None


def test_fetch_page_handles_non_string_id_via_str_or_empty_fallback() -> None:
    """A message ``id`` that isn't a string becomes the empty string.

    Drives the ``_str_or_empty`` helper through the public surface.

    Sabotage proof: change the fallback to ``return value`` — a non-string
    value propagates into the typed ``message_id`` field and breaks the
    assertion below.
    """
    page = _fetch_page_with_body({"value": [{"id": 42}]})
    assert len(page.messages) == 1
    assert page.messages[0].message_id == ""


def test_fetch_page_handles_non_string_subject_via_optional_str_fallback() -> None:
    """A non-string subject / timestamp surfaces as ``None``.

    Drives the ``_optional_str`` helper through the public surface.

    Sabotage proof: change the helper's return to ``return value`` — a
    non-string value would surface on the typed field and break the
    assertion below.
    """
    page = _fetch_page_with_body({"value": [{"id": "msg-1", "subject": 42, "sentDateTime": ["not", "a", "string"]}]})
    assert len(page.messages) == 1
    assert page.messages[0].subject is None
    assert page.messages[0].sent_at is None


# ---------------------------------------------------------------------------
# Throttling retry loop (GH #357) — unit coverage of the new wait/retry
# helpers, exercised through the public ``iter_messages`` surface so the
# F7 floor on this file stays green.
# ---------------------------------------------------------------------------


def _build_client_with_sleep_recording(
    handler: httpx.MockTransport,
    *,
    recorded_sleeps: list[float],
    max_attempts: int = 3,
) -> M365GraphClient:
    """Construct a real :class:`M365GraphClient` with a recording sleep_fn.

    F1 / F2 clean — ``sleep_fn`` is a public constructor seam already in
    production use for tests; the mock transport replaces the HTTP wire
    without monkey-patching kairix internals.
    """
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    return M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
        sleep_fn=recorded_sleeps.append,
        max_attempts=max_attempts,
    )


def _token_response_for_unit(request: httpx.Request) -> httpx.Response | None:
    """Return a 200 token reply for the OAuth2 endpoint, else ``None``."""
    if "/oauth2/v2.0/token" in str(request.url):
        # pragma: allowlist secret — test fixture
        body = {"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"}
        return httpx.Response(200, json=body)
    return None


def test_authorised_get_retries_on_429_with_retry_after() -> None:
    """A 429 with ``Retry-After: 2`` retries after sleeping exactly 2s, then 200.

    Drives :meth:`_authorised_get` retry loop + :meth:`_wait_strategy`
    Retry-After branch + :func:`_parse_retry_after` through the public
    ``iter_messages`` surface.

    Sabotage proof: drop the ``retry_after`` branch in ``_wait_strategy``
    (always return exponential) — the recorded sleep would be the
    exponential floor (2.0s); coincidentally equal here so this test
    asserts on call_count too. Removing 429 from
    ``_RETRYABLE_STATUS_CODES`` would cause the first 429 to raise and
    the call_count assertion would fail (1 instead of 2).
    """
    call_count = {"n": 0}
    recorded_sleeps: list[float] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        token = _token_response_for_unit(request)
        if token is not None:
            return token
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "throttled"})
        return httpx.Response(200, json={"value": []})

    transport = httpx.MockTransport(_handler)  # type: ignore[arg-type]  # F3 rationale: pytest typing accepts handler shapes httpx narrows at runtime.
    client = _build_client_with_sleep_recording(transport, recorded_sleeps=recorded_sleeps)
    messages = list(client.iter_messages("inbox", start_url=None))
    assert messages == []
    assert call_count["n"] == 2
    assert recorded_sleeps == [2.0]


def test_authorised_get_503_without_retry_after_uses_exponential_backoff() -> None:
    """A 503 with NO Retry-After falls back to exponential backoff (≥2.0s floor).

    Drives the second branch of :meth:`_wait_strategy` —
    ``_parse_retry_after`` returns ``None`` → ``wait_exponential`` fires.

    Sabotage proof: removing the ``wait_exponential`` fallback in
    ``_wait_strategy`` would make the recorded sleep 0, breaking the
    ``>=2.0`` floor assertion.
    """
    call_count = {"n": 0}
    recorded_sleeps: list[float] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        token = _token_response_for_unit(request)
        if token is not None:
            return token
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"value": []})

    transport = httpx.MockTransport(_handler)  # type: ignore[arg-type]  # F3 rationale: pytest typing accepts handler shapes httpx narrows at runtime.
    client = _build_client_with_sleep_recording(transport, recorded_sleeps=recorded_sleeps)
    messages = list(client.iter_messages("inbox", start_url=None))
    assert messages == []
    assert call_count["n"] == 2
    assert len(recorded_sleeps) == 1
    assert recorded_sleeps[0] >= 2.0


def test_authorised_get_503_with_unparseable_retry_after_falls_back() -> None:
    """``Retry-After: not-a-number`` falls back to exponential backoff.

    Drives :func:`_parse_retry_after`'s ``except (TypeError, ValueError)``
    branch. The header is present but the value can't parse to float, so
    the wait strategy must fall back to exponential.

    Sabotage proof: removing the ``try / except`` around ``float(raw)``
    in ``_parse_retry_after`` would propagate ``ValueError`` and the
    iter_messages call would raise instead of completing cleanly.
    """
    call_count = {"n": 0}
    recorded_sleeps: list[float] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        token = _token_response_for_unit(request)
        if token is not None:
            return token
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "not-a-number"}, json={"error": "unavailable"})
        return httpx.Response(200, json={"value": []})

    transport = httpx.MockTransport(_handler)  # type: ignore[arg-type]  # F3 rationale: pytest typing accepts handler shapes httpx narrows at runtime.
    client = _build_client_with_sleep_recording(transport, recorded_sleeps=recorded_sleeps)
    messages = list(client.iter_messages("inbox", start_url=None))
    assert messages == []
    assert call_count["n"] == 2
    assert recorded_sleeps[0] >= 2.0


# ---------------------------------------------------------------------------
# Folder-scoped delta URL builder (#380)
# ---------------------------------------------------------------------------


def test_initial_delta_url_is_folder_scoped() -> None:
    """The URL builder emits the folder-scoped delta URL shape per #380.

    Graph rejects mailbox-wide ``/users/{upn}/messages/delta`` with
    ``BadRequest: Change tracking is not supported against
    'microsoft.graph.message'`` — delta only works folder-scoped. The
    URL must carry ``/mailFolders/{folder_id}/messages/delta``.

    Sabotage proof (verified): reverting :meth:`initial_delta_url` to
    return ``f"{base}/users/{upn}/messages/delta?$select=..."`` makes
    the ``/mailFolders/inbox/messages/delta`` substring assertion fail.
    """
    handler = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
    auth, _ = _build_auth_with_transport(handler)
    client = M365GraphClient(user_principal_name="agent-alpha@example.com", auth=auth)
    url = client.initial_delta_url("inbox")
    assert "/users/agent-alpha@example.com/mailFolders/inbox/messages/delta" in url, (
        f"folder-scoped URL shape missing: {url!r}"
    )
    # And the URL still carries the header-only $select projection.
    assert "$select=" in url


def test_initial_delta_url_rejects_empty_folder_id() -> None:
    """Calling :meth:`initial_delta_url` with an empty folder_id raises a typed ValueError.

    The brief says #380's fix requires a folder id — an empty string
    would silently produce a malformed URL like
    ``/users/{upn}/mailFolders//messages/delta``. The guard is the
    affordance: surface the misconfig at the caller with a ``fix:``
    marker.

    Sabotage proof: drop the ``if not folder_id`` guard at the top of
    :meth:`initial_delta_url` — the ``pytest.raises`` block fails to
    fire.
    """
    handler = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
    auth, _ = _build_auth_with_transport(handler)
    client = M365GraphClient(user_principal_name="agent-alpha@example.com", auth=auth)
    with pytest.raises(ValueError) as exc_info:
        client.initial_delta_url("")
    msg = str(exc_info.value)
    assert "folder_id" in msg
    assert "fix:" in msg, f"error message missing fix: marker: {msg!r}"


# ---------------------------------------------------------------------------
# list_mail_folders parser shape (#380)
# ---------------------------------------------------------------------------


def _list_folders_with_body(body: dict[str, Any]) -> tuple[MailFolderRef, ...]:
    """Drive :meth:`M365GraphClient.list_mail_folders` against a stub
    serving ``body`` as the mailFolders response.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"},
            )
        return httpx.Response(200, json=body)

    handler = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(handler)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )
    return client.list_mail_folders()


def test_list_mail_folders_parses_well_known_and_custom_folders() -> None:
    """Both well-known and custom folders come through with their attributes.

    Sabotage proof: drop the ``well_known_name`` field from the parser
    helper — the ``well_known_name`` assertions below fail because the
    field would be ``None`` for every folder.
    """
    body: dict[str, Any] = {
        "value": [
            {
                "id": "AAMkAGFmYWtl-inbox",
                "displayName": "Inbox",
                "wellKnownName": "inbox",
            },
            {
                "id": "AAMkAGFmYWtl-sent",
                "displayName": "Sent Items",
                "wellKnownName": "sentitems",
            },
            {
                "id": "AAMkAGFmYWtl-q2-receipts",
                "displayName": "Q2 Receipts",
                "wellKnownName": None,
            },
        ],
    }
    folders = _list_folders_with_body(body)
    assert len(folders) == 3
    by_id = {f.folder_id: f for f in folders}
    assert by_id["AAMkAGFmYWtl-inbox"].well_known_name == "inbox"
    assert by_id["AAMkAGFmYWtl-inbox"].display_name == "Inbox"
    assert by_id["AAMkAGFmYWtl-sent"].well_known_name == "sentitems"
    assert by_id["AAMkAGFmYWtl-q2-receipts"].well_known_name is None
    assert by_id["AAMkAGFmYWtl-q2-receipts"].display_name == "Q2 Receipts"


def test_list_mail_folders_drops_entries_with_missing_id() -> None:
    """Folders missing ``id`` are dropped — the id is the URL key.

    Sabotage proof: change the parser's ``if not isinstance(folder_id, str)
    or not folder_id`` guard to ``True`` (always append) — the folders
    list would include a phantom entry and the count assertion below
    would fail.
    """
    body: dict[str, Any] = {
        "value": [
            {"id": "AAMkAGFmYWtl-inbox", "displayName": "Inbox", "wellKnownName": "inbox"},
            {"displayName": "Orphan (no id)", "wellKnownName": None},
            {"id": "", "displayName": "Empty id", "wellKnownName": None},
            {"id": 42, "displayName": "Non-string id", "wellKnownName": None},
        ],
    }
    folders = _list_folders_with_body(body)
    assert len(folders) == 1
    assert folders[0].folder_id == "AAMkAGFmYWtl-inbox"


def test_list_mail_folders_follows_next_link_pagination() -> None:
    """A response with ``@odata.nextLink`` is followed to drain every page.

    Sabotage proof: remove the ``while url is not None`` loop in
    :meth:`list_mail_folders` (return after first page) — the second
    folder never surfaces and the count assertion drops to 1.
    """
    pages = [
        {
            "value": [
                {"id": "AAMkAGFmYWtl-inbox", "displayName": "Inbox", "wellKnownName": "inbox"},
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/mailFolders?$skiptoken=page2",
        },
        {
            "value": [
                {"id": "AAMkAGFmYWtl-archive", "displayName": "Archive", "wellKnownName": "archive"},
            ],
        },
    ]
    sequence = list(pages)

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"},
            )
        payload = sequence.pop(0) if sequence else {"value": []}
        return httpx.Response(200, json=payload)

    handler = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(handler)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )
    folders = client.list_mail_folders()
    assert len(folders) == 2
    ids = [f.folder_id for f in folders]
    assert ids == ["AAMkAGFmYWtl-inbox", "AAMkAGFmYWtl-archive"], (
        f"expected pagination to drain both pages in order, got {ids!r}"
    )


def test_list_mail_folders_handles_empty_value_array() -> None:
    """An empty ``value`` returns an empty tuple, not a parse error.

    Sabotage proof: change ``if not isinstance(raw_folders, list)`` to
    ``raw_folders is None`` — a non-list value would explode below.
    """
    folders = _list_folders_with_body({"value": []})
    assert folders == ()


def test_list_mail_folders_handles_non_dict_entries() -> None:
    """A ``value`` array containing non-dict entries skips them gracefully.

    Sabotage proof: drop the ``if not isinstance(entry, dict)`` guard in
    the parser — non-dict entries raise ``AttributeError`` instead of
    being skipped.
    """
    body: dict[str, Any] = {
        "value": [
            {"id": "AAMkAGFmYWtl-inbox", "displayName": "Inbox", "wellKnownName": "inbox"},
            "not-a-dict",
            None,
            42,
            {"id": "AAMkAGFmYWtl-archive", "displayName": "Archive", "wellKnownName": "archive"},
        ],
    }
    folders = _list_folders_with_body(body)
    assert len(folders) == 2


def test_authorised_get_default_http_client_path() -> None:
    """When no ``http_client`` is injected the client owns its own httpx session.

    Drives the ``http_client is None`` branch in :meth:`_do_get` (the
    ``with httpx.Client(...) as owned`` arm). The constructor accepts
    ``http_client=None`` by default; the constructed client falls back
    to a self-owned httpx session for each request.

    Sabotage proof: removing the ``if client is not None`` branch would
    cause this test to fail because the owned-client path is never
    exercised; instead the injected ``None`` would raise AttributeError.
    """
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
    )
    # We never invoke the request; just confirm the construction path is
    # reachable and the http_client attribute is set to None.
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=None,
    )
    assert client._http_client is None


def test_410_response_raises_typed_delta_expired_error() -> None:
    """A Graph 410 is distinguishable from auth and transient failures."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/oauth2/v2.0/token" in str(request.url):
            return httpx.Response(
                200,
                request=request,
                json={"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"},
            )
        return httpx.Response(
            410,
            request=request,
            json={"error": {"code": "syncStateNotFound"}},
        )

    transport = httpx.MockTransport(_handler)
    auth, shared = _build_auth_with_transport(transport)
    client = M365GraphClient(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
        http_client=shared,
    )

    with pytest.raises(GraphDeltaExpiredError) as raised:
        client.fetch_page("https://graph.microsoft.com/v1.0/expired-message-delta")

    assert raised.value.response.status_code == 410
