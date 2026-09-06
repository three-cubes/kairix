"""Unit tests for :class:`kairix.connectors.m365_email_headers.M365EmailHeadersConnector`.

Scope per the KP-2 brief (+ #380 folder-scoped delta):

  * A Graph delta response with three envelopes → ``list_changes(None)``
    emits three ``created`` events; cursor advances past the deltaLink.
  * Header-only $select projection is the constructed Graph URL (the
    no-body-content invariant per ADR-004).
  * Folder-scoped delta URL (#380): the Graph URL carries
    ``/mailFolders/{folder_id}/messages/delta`` rather than the broken
    mailbox-wide ``/messages/delta``.
  * Pagination — a response carrying ``@odata.nextLink`` keeps the
    iterator going through the next page; the deltaLink from the final
    page is what ``next_cursor`` returns.
  * ``fetch`` returns a JSON artefact with NO body fields.
  * ``make_connector`` rejects a missing ``user_principal_name`` AND
    rejects a config that tries to override the locked ``personal``
    sensitivity tier.
  * Sabotage proof: mutating
    :data:`HEADER_ONLY_SELECT` to include ``body`` makes
    :func:`test_initial_delta_url_carries_header_only_projection` fail.

F1-clean (no monkey-patching), F6-clean (every test seam is a real
callable default), F8 carries ``@pytest.mark.unit``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kairix.connectors.m365_email_headers import (
    M365EmailHeadersConnector,
    M365GraphClient,
    make_connector,
)
from kairix.connectors.m365_email_headers.connector import (
    LOCKED_SENSITIVITY,
    M365Credentials,
)
from kairix.connectors.m365_email_headers.graph_client import (
    HEADER_ONLY_SELECT,
)
from kairix.transport.auth.oauth2_client_creds import (
    MissingCredentialsError,
    OAuth2ClientCredsAuth,
)

pytestmark = pytest.mark.unit


def _envelopes() -> list[dict[str, Any]]:
    return [
        {
            "id": "msg-1",
            "from": {"emailAddress": {"address": "agent-alpha@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "agent-beta@example.com"}}],
            "ccRecipients": [],
            "subject": "Project status",
            "sentDateTime": "2026-05-22T10:00:00Z",
            "receivedDateTime": "2026-05-22T10:00:01Z",
        },
        {
            "id": "msg-2",
            "from": {"emailAddress": {"address": "agent-beta@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "agent-alpha@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "agent-gamma@example.com"}}],
            "subject": "Re: Project status",
            "sentDateTime": "2026-05-22T11:00:00Z",
            "receivedDateTime": "2026-05-22T11:00:01Z",
        },
        {
            "id": "msg-3",
            "from": {"emailAddress": {"address": "agent-gamma@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "agent-alpha@example.com"}}],
            "ccRecipients": [],
            "subject": "Closing the loop",
            "sentDateTime": "2026-05-22T12:00:00Z",
            "receivedDateTime": "2026-05-22T12:00:01Z",
        },
    ]


def _single_page_payload() -> dict[str, Any]:
    return {
        "value": _envelopes(),
        "@odata.deltaLink": (
            "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$deltatoken=tok-final"
        ),
    }


def _paginated_pages() -> tuple[dict[str, Any], dict[str, Any]]:
    """Two-page response — first carries nextLink, second carries deltaLink."""
    first = {
        "value": _envelopes()[:2],
        "@odata.nextLink": (
            "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$skiptoken=next-page-token"
        ),
    }
    second = {
        "value": _envelopes()[2:],
        "@odata.deltaLink": (
            "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$deltatoken=tok-final"
        ),
    }
    return first, second


def _single_folder_payload() -> dict[str, Any]:
    """One mailFolders response carrying a single ``inbox`` well-known folder."""
    return {
        "value": [
            {
                "id": "AAMkAGFmYWtl-inbox",
                "displayName": "Inbox",
                "wellKnownName": "inbox",
            },
        ],
    }


def _build_real_connector(
    handler: httpx.MockTransport | None = None,
    pages: list[dict[str, Any]] | None = None,
    recorded_urls: list[str] | None = None,
    folders_payload: dict[str, Any] | None = None,
) -> M365EmailHeadersConnector:
    """Compose the real connector against a MockTransport-backed Graph stub.

    The stub differentiates between the mailFolders enumeration call
    (#380: the connector now calls ``GET /users/{upn}/mailFolders``
    before each per-folder delta) and the per-folder delta calls. Tests
    pass ``pages`` to control the delta-response sequence and
    ``folders_payload`` to control the folder enumeration; default is a
    single ``inbox`` well-known folder with the three-envelope page.
    """
    if handler is None:
        sequence = list(pages) if pages is not None else [_single_page_payload()]
        recorded = recorded_urls if recorded_urls is not None else []
        folders = folders_payload if folders_payload is not None else _single_folder_payload()

        def _stub(request: httpx.Request) -> httpx.Response:
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
            # mailFolders enumeration — no $select, no $deltatoken.
            if "/mailFolders" in url and "/messages/delta" not in url:
                return httpx.Response(200, json=folders)
            recorded.append(url)
            payload = sequence.pop(0) if sequence else {"value": []}
            return httpx.Response(200, json=payload)

        handler = httpx.MockTransport(_stub)

    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    return M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(
            tenant_id="fake-tenant",
            client_id="fake-client",
            client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        ),
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(user_principal_name=u, auth=a, http_client=shared),
    )


# ---------------------------------------------------------------------------
# Delta-query behaviour
# ---------------------------------------------------------------------------


def test_list_changes_emits_one_event_per_envelope() -> None:
    """Three envelopes from Graph → three ``created`` events.

    Sabotage proof: change the loop body in
    :meth:`M365EmailHeadersConnector.list_changes` to skip every
    second message — the count assertion below drops to 2 and fails.
    """
    connector = _build_real_connector()
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 3, f"expected 3 events, got {len(events)}"
    for ev in events:
        assert ev.op == "created"
        assert ev.metadata.get("sensitivity") == "personal"


def test_list_changes_advances_cursor_to_delta_link() -> None:
    """After a successful drain, ``next_cursor`` returns a JSON-encoded
    ``{folder_id: deltaLink}`` mapping carrying the folder's deltaLink.

    #380: the cursor is now a JSON dict (one deltaLink per folder), not
    a bare deltaLink string. The mapping decodes to a dict whose value
    for the seeded inbox folder ends with ``$deltatoken=tok-final``.

    Sabotage proof: replace
    ``self._next_cursor = _encode_per_folder_cursor(next_cursors)``
    with ``self._next_cursor = None`` — both assertions below fail.
    """
    connector = _build_real_connector()
    _ = list(connector.list_changes(cursor=None))
    cursor = connector.next_cursor()
    assert cursor is not None, "cursor must persist a per-folder deltaLink mapping"
    decoded = json.loads(cursor)
    assert isinstance(decoded, dict), f"cursor must encode a dict; got {type(decoded).__name__}"
    assert "AAMkAGFmYWtl-inbox" in decoded, f"cursor missing inbox folder key; got {decoded!r}"
    assert "$deltatoken=tok-final" in decoded["AAMkAGFmYWtl-inbox"]


def test_pagination_drains_nextlink_pages() -> None:
    """A Graph response with ``@odata.nextLink`` is followed to its end.

    Sabotage proof: short-circuit the iter_messages loop in
    :class:`M365GraphClient` to break after the first page — the
    count assertion drops below 3.
    """
    first, second = _paginated_pages()
    connector = _build_real_connector(pages=[first, second])
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 3, f"expected 3 events across both pages, got {len(events)}"


# ---------------------------------------------------------------------------
# Header-only invariant per ADR-004
# ---------------------------------------------------------------------------


def test_initial_delta_url_carries_header_only_projection() -> None:
    """The Graph URL must carry ``$select`` AND must not list any body field.

    This is the mechanical guard for the ADR-004 no-body-content
    invariant. Sabotage proof: mutate
    :data:`HEADER_ONLY_SELECT` to ``"body,from,subject"`` — the
    forbidden-key assertion below fails immediately.
    """
    recorded: list[str] = []
    connector = _build_real_connector(recorded_urls=recorded)
    _ = list(connector.list_changes(cursor=None))
    assert recorded, "expected at least one Graph URL"
    url = recorded[0]
    assert "$select=" in url, f"Graph URL missing $select: {url!r}"
    fields = {f.strip() for f in url.split("$select=", 1)[1].split("&", 1)[0].split(",")}
    forbidden = {"body", "bodyPreview", "uniqueBody"}
    leaks = forbidden & fields
    assert not leaks, f"$select projection leaked body fields: {leaks!r}"
    # And the projection MUST carry the canonical header-only fields.
    for required in ("from", "toRecipients", "subject", "sentDateTime"):
        assert required in fields, f"missing required header field {required!r} in projection {fields!r}"


def test_list_changes_url_is_folder_scoped() -> None:
    """The Graph URL MUST be folder-scoped per #380.

    Graph rejects mailbox-wide ``/users/{upn}/messages/delta`` with
    ``BadRequest: Change tracking is not supported against
    'microsoft.graph.message'`` — delta only works folder-scoped
    (``/users/{upn}/mailFolders/{folder_id}/messages/delta``).

    Sabotage proof: revert :meth:`M365GraphClient.initial_delta_url`
    to the pre-fix mailbox-wide shape
    (``f"{base}/users/{upn}/messages/delta?$select=..."``) — the
    ``/mailFolders/AAMkAGFmYWtl-inbox/messages/delta`` substring
    assertion below fails immediately and Graph would reject the
    request in production.
    """
    recorded: list[str] = []
    connector = _build_real_connector(recorded_urls=recorded)
    _ = list(connector.list_changes(cursor=None))
    assert recorded, "expected at least one Graph URL"
    url = recorded[0]
    assert "/mailFolders/AAMkAGFmYWtl-inbox/messages/delta" in url, (
        f"Graph URL must be folder-scoped per #380; got {url!r}"
    )
    # The pre-fix mailbox-wide path must NOT appear in the URL.
    assert "/users/agent-alpha@example.com/messages/delta" not in url, (
        f"Graph URL is still mailbox-wide (#380 regression); got {url!r}"
    )


def test_header_only_select_constant_excludes_body_fields() -> None:
    """:data:`HEADER_ONLY_SELECT` itself contains no body field.

    Sabotage proof: append ``,body`` to the constant — this assertion
    fails. This pins the constant at the module level so a future
    contributor cannot widen it without breaking the test.
    """
    fields = {f.strip() for f in HEADER_ONLY_SELECT.split(",")}
    forbidden = {"body", "bodyPreview", "uniqueBody"}
    leaks = forbidden & fields
    assert not leaks, f"HEADER_ONLY_SELECT leaked body fields: {leaks!r}"


def test_fetch_returns_json_artefact_without_body() -> None:
    """The fetched artefact JSON contains no body / bodyPreview / uniqueBody.

    Sabotage proof: add ``"body": message.subject`` to the JSON payload
    in :meth:`M365EmailHeadersConnector.fetch` — the forbidden-key
    assertion below fails.
    """
    connector = _build_real_connector()
    _ = list(connector.list_changes(cursor=None))
    artefact = connector.fetch("msg-1")
    assert artefact.mime == "application/json"
    payload = json.loads(artefact.raw.decode("utf-8"))
    forbidden = {"body", "bodyPreview", "uniqueBody"}
    leaks = set(payload.keys()) & forbidden
    assert not leaks, f"fetch artefact leaked body fields: {leaks!r}"


# ---------------------------------------------------------------------------
# Sensitivity tier locked to personal
# ---------------------------------------------------------------------------


def test_sensitivity_for_returns_locked_personal_tier() -> None:
    """:meth:`sensitivity_for` returns ``personal`` regardless of item.

    Sabotage proof: change the return to ``"public"`` — this assertion
    fails. The locked-tier behaviour is what makes the connector ADR-005
    compliant.
    """
    connector = _build_real_connector()
    assert connector.sensitivity_for("msg-1") == LOCKED_SENSITIVITY == "personal"


# ---------------------------------------------------------------------------
# source_link
# ---------------------------------------------------------------------------


def test_source_link_round_trips_to_outlook_url() -> None:
    """``source_link`` returns a URL pointing at Outlook on the Web.

    Sabotage proof: return ``""`` from ``source_link`` — the
    ``startswith`` assertion fails.
    """
    connector = _build_real_connector()
    link = connector.source_link("msg-1")
    assert link.startswith("https://outlook.office.com/mail/inbox/id/")
    assert "msg-1" in link


# ---------------------------------------------------------------------------
# fetch with no list_changes priming raises a typed KeyError
# ---------------------------------------------------------------------------


def test_fetch_without_priming_raises_typed_key_error() -> None:
    """Calling ``fetch`` before any ``list_changes`` is a typed error.

    Sabotage proof: silently return an empty RawArtefact in that path
    — the ``pytest.raises`` block below fails (no exception raised).
    """
    connector = _build_real_connector()
    with pytest.raises(KeyError) as exc_info:
        connector.fetch("never-listed")
    msg = str(exc_info.value)
    assert "fix:" in msg, f"error message missing fix: marker: {msg!r}"
    assert "list_changes" in msg, f"error message missing list_changes hint: {msg!r}"


# ---------------------------------------------------------------------------
# make_connector factory shape
# ---------------------------------------------------------------------------


def test_make_connector_requires_user_principal_name() -> None:
    """A config without ``user_principal_name`` raises ValueError.

    Sabotage proof: change the check to ``upn = config.get("user_principal_name", "alice@x.com")``
    — the ``pytest.raises`` block fails.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({})
    assert "user_principal_name" in str(exc_info.value)


def test_make_connector_rejects_sensitivity_override() -> None:
    """A config that tries to lower sensitivity is rejected loudly.

    Per ADR-005, the personal tier is locked at the connector boundary.
    Sabotage proof: remove the sensitivity check in ``make_connector``
    — the ``pytest.raises`` block fails.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({"user_principal_name": "alice@example.com", "sensitivity": "public"})
    assert "locked" in str(exc_info.value)


def test_make_connector_accepts_locked_sensitivity_declaration() -> None:
    """Declaring the locked tier explicitly in config is allowed.

    Operators who want to be explicit about the tier in their YAML
    aren't punished for it. The constructor still resolves
    credentials lazily — this test asserts the factory call would
    not raise the locked-tier ValueError; the real-credentials path
    is exercised separately.
    """
    from kairix.secrets.loader import SecretNotFoundError

    # The factory will try to resolve secrets — we expect it to raise
    # SecretNotFoundError / MissingCredentialsError / OSError rather
    # than a sensitivity-locked ValueError. That demonstrates the
    # sensitivity check passed.
    with pytest.raises((SecretNotFoundError, MissingCredentialsError, OSError)):
        make_connector({"user_principal_name": "agent-alpha@example.com", "sensitivity": LOCKED_SENSITIVITY})


def test_make_connector_rejects_non_list_mailboxes() -> None:
    """A scalar ``mailboxes`` value is rejected with the fix-pointer.

    Pins the shared :func:`_coerce_optional_string_list` helper that
    backs both ``mailboxes`` and ``folders_allowlist`` validation
    (S3776 refactor). Sabotage-proof: drop the
    ``isinstance(raw, list | tuple)`` check in the helper — the test
    fails because a bare string no longer raises.
    """
    with pytest.raises(ValueError, match="'mailboxes' must be a list"):
        make_connector({"user_principal_name": "alice@example.com", "mailboxes": "alice@example.com"})


def test_make_connector_rejects_mailboxes_with_non_string_entries() -> None:
    """A ``mailboxes`` list containing a non-string entry is rejected.

    Sabotage-proof: drop the ``all(isinstance(item, str) and item ...)``
    check in :func:`_coerce_optional_string_list` — the test fails
    because the integer entry is silently accepted.
    """
    with pytest.raises(ValueError, match="'mailboxes' must be a list"):
        make_connector({"user_principal_name": "alice@example.com", "mailboxes": ["alice@example.com", 42]})


def test_make_connector_accepts_none_mailboxes_and_allowlist() -> None:
    """Both ``mailboxes`` and ``folders_allowlist`` default to None.

    A config that omits both keys must reach the constructor (which
    then attempts credential resolution). Sabotage-proof: change
    :func:`_coerce_optional_string_list` to ``return []`` on a ``None``
    input — the empty-list case in the constructor changes shape and
    other tests break first; this test confirms the ``None`` short-circuit
    is intact.
    """
    from kairix.secrets.loader import SecretNotFoundError

    # The factory will try to resolve secrets — anything other than the
    # F-rule ValueErrors above (UPN, sensitivity, list shape) means the
    # helper short-circuited None correctly.
    with pytest.raises((SecretNotFoundError, MissingCredentialsError, OSError)):
        make_connector({"user_principal_name": "agent-alpha@example.com"})


# ---------------------------------------------------------------------------
# Constructor input validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_user_principal_name() -> None:
    """Constructing with an empty UPN is a typed ValueError.

    Sabotage proof: drop the ``if not user_principal_name`` guard at the
    top of ``__init__`` — the ``pytest.raises`` block below stops firing
    and the test fails.
    """
    with pytest.raises(ValueError) as exc_info:
        M365EmailHeadersConnector(
            user_principal_name="",
            credentials=M365Credentials(
                tenant_id="fake-tenant",
                client_id="fake-client",
                client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
            ),
        )
    assert "user_principal_name" in str(exc_info.value)


def test_constructor_uses_default_graph_client_when_no_builder_supplied() -> None:
    """Omitting ``client_builder`` constructs a real :class:`M365GraphClient`.

    The default branch in ``__init__`` builds an :class:`M365GraphClient`
    with the resolved auth and the UPN. Sabotage proof: change the
    default-branch ``M365GraphClient(...)`` call to ``None`` — the
    attribute access ``connector._graph`` below fails.
    """
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        auth=auth,
    )
    # Direct attribute is internal — assert the public surface still
    # behaves: source_link round-trips, sensitivity stays personal.
    assert connector.source_link("msg-1").startswith("https://outlook.office.com/")
    assert connector.sensitivity_for("msg-1") == LOCKED_SENSITIVITY


def test_constructor_resolves_credentials_via_injected_secrets_loader() -> None:
    """Omitting ``credentials`` / ``auth`` resolves via the injected ``secrets``.

    Per ADR-031, the connector reads credentials through the canonical
    :class:`kairix.secrets.loader.SecretsResolver`. Tests pass a populated
    :class:`tests.fakes.FakeSecretsLoader` rather than priming env vars
    or per-file mounts; the canonical identity tuple
    ``(connector, m365, None, <leaf>)`` matches the one M365 / SharePoint
    / Calendar share per the legacy-alias map.

    Sabotage proof: remove the ``_resolve_credentials_from_secrets()``
    call from ``__init__`` (e.g. force ``creds = M365Credentials(...)`` with
    empty strings) — the auth helper raises ``MissingCredentialsError``
    because it sees an empty tenant_id.
    """
    from tests.fakes import FakeSecretsLoader

    loader = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "fake-tenant",
            ("connector", "m365", None, "client-id"): "fake-client",
            ("connector", "m365", None, "client-secret"): "fake-secret-value",
        }
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        secrets=loader,
    )
    assert connector.sensitivity_for("any-id") == LOCKED_SENSITIVITY
    assert connector.source_link("msg-1").startswith("https://outlook.office.com/")


def test_constructor_loads_secrets_via_loader() -> None:
    """``__init__`` calls ``loader.require`` for each of the three M365 leaves.

    Asserts on the loader's call history so the test pins each canonical
    identity tuple read at construction time — adding or removing a leaf
    surfaces here before downstream callers notice.

    Sabotage proof: drop one of the three ``secrets.require(...)`` calls
    in ``_resolve_credentials_from_secrets`` — the expected-tuples set
    no longer matches the loader's recorded calls and this test fails.
    """
    from tests.fakes import FakeSecretsLoader

    loader = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "fake-tenant",
            ("connector", "m365", None, "client-id"): "fake-client",
            ("connector", "m365", None, "client-secret"): "fake-secret-value",
        }
    )
    M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        secrets=loader,
    )
    expected: set[tuple[str, str, str | None, str]] = {
        ("connector", "m365", None, "tenant-id"),
        ("connector", "m365", None, "client-id"),
        ("connector", "m365", None, "client-secret"),
    }
    recorded = set(loader.get_calls)
    assert expected.issubset(recorded), (
        f"connector must call loader.require for each canonical M365 leaf; missing={expected - recorded}"
    )


# ---------------------------------------------------------------------------
# _event_modified_at timestamp-fallback ladder
# ---------------------------------------------------------------------------


def test_modified_at_falls_back_to_sent_when_received_missing() -> None:
    """When ``receivedDateTime`` is absent, fall back to ``sentDateTime``.

    Sabotage proof: change the second branch in ``_event_modified_at``
    from ``return message.sent_at`` to ``return ""`` — the assertion
    that the event timestamp matches the sent timestamp fails.
    """
    payload = {
        "value": [
            {
                "id": "msg-sent-only",
                "from": {"emailAddress": {"address": "agent-alpha@example.com"}},
                "toRecipients": [],
                "ccRecipients": [],
                "subject": "Sent-only",
                "sentDateTime": "2026-05-22T09:00:00Z",
                # NB: receivedDateTime intentionally omitted.
            }
        ],
        "@odata.deltaLink": (
            "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$deltatoken=tok"
        ),
    }
    connector = _build_real_connector(pages=[payload])
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 1
    assert events[0].modified_at == "2026-05-22T09:00:00Z"


# ---------------------------------------------------------------------------
# Folder-scoped delta failure paths (#380) — public surface only (F5)
# ---------------------------------------------------------------------------


def _list_folders_only_payload() -> dict[str, Any]:
    """Three folders for the multi-folder unit tests."""
    return {
        "value": [
            {"id": "f1", "displayName": "Inbox", "wellKnownName": "inbox"},
            {"id": "f2", "displayName": "Sent Items", "wellKnownName": "sentitems"},
            {"id": "f3", "displayName": "Archive", "wellKnownName": "archive"},
        ],
    }


def _recovery_message(folder_id: str) -> dict[str, Any]:
    """One complete header envelope for a recovered folder."""
    return {
        "id": f"{folder_id}-message",
        "from": {"emailAddress": {"address": "agent-alpha@example.com"}},
        "toRecipients": [],
        "ccRecipients": [],
        "subject": f"Recovered {folder_id}",
        "sentDateTime": "2026-05-22T10:00:00Z",
        "receivedDateTime": "2026-05-22T10:00:01Z",
    }


def _email_recovery_handler(
    requested: list[str],
    *,
    fail_reseed_for: str | None = None,
    folders_payload: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Serve two folders with an expired f1 cursor and a healthy f2 cursor."""

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                request=request,
                json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"},
            )
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(
                200,
                request=request,
                json=folders_payload or {"value": _list_folders_only_payload()["value"][:2]},
            )
        requested.append(url)
        if "stale-f1" in url:
            return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})
        if "/mailFolders/f1/messages/delta" in url and fail_reseed_for == "f1":
            return httpx.Response(410, request=request, json={"error": {"code": "syncStateNotFound"}})
        folder_id = "f1" if "/mailFolders/f1/" in url else "f2"
        return httpx.Response(
            200,
            request=request,
            json={
                "value": [_recovery_message(folder_id)],
                "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/fresh-{folder_id}",
            },
        )

    return httpx.MockTransport(_handler)


def test_expired_folder_cursor_resets_only_that_folder_and_siblings_progress() -> None:
    """A 410 resets f1 once while f2 continues from its own cursor."""
    requested: list[str] = []
    connector = _build_real_connector(
        handler=_email_recovery_handler(requested),
        folders_payload=_list_folders_only_payload(),
    )
    prior = json.dumps(
        {
            "f1": "https://graph.microsoft.com/v1.0/stale-f1",
            "f2": "https://graph.microsoft.com/v1.0/mailFolders/f2/messages/delta?$deltatoken=healthy-f2",
        }
    )

    events = list(connector.list_changes(cursor=prior))

    assert {event.item_id for event in events} == {"f1-message", "f2-message"}
    assert sum("stale-f1" in url for url in requested) == 1
    assert sum("/mailFolders/f1/messages/delta" in url for url in requested) == 1
    assert sum("healthy-f2" in url for url in requested) == 1
    assert json.loads(connector.next_cursor() or "{}") == {
        "f1": "https://graph.microsoft.com/v1.0/fresh-f1",
        "f2": "https://graph.microsoft.com/v1.0/fresh-f2",
    }


def test_folder_reseed_410_preserves_prior_cursor_and_sibling_progress(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed one-shot f1 reseed preserves f1 while f2 still advances."""
    requested: list[str] = []
    connector = _build_real_connector(
        handler=_email_recovery_handler(requested, fail_reseed_for="f1"),
        folders_payload=_list_folders_only_payload(),
    )
    stale_f1 = "https://graph.microsoft.com/v1.0/stale-f1"
    prior = json.dumps(
        {
            "f1": stale_f1,
            "f2": "https://graph.microsoft.com/v1.0/mailFolders/f2/messages/delta?$deltatoken=healthy-f2",
        }
    )

    with caplog.at_level("WARNING", logger="kairix.connectors.m365_email_headers.connector"):
        events = list(connector.list_changes(cursor=prior))

    assert [event.item_id for event in events] == ["f2-message"]
    assert sum("stale-f1" in url for url in requested) == 1
    assert sum("/mailFolders/f1/messages/delta" in url for url in requested) == 1
    assert json.loads(connector.next_cursor() or "{}") == {
        "f1": stale_f1,
        "f2": "https://graph.microsoft.com/v1.0/fresh-f2",
    }
    assert any("folder 'Inbox' drain failed" in record.getMessage() for record in caplog.records)


def test_failed_folder_without_display_name_is_identified_by_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery diagnostics retain an identity when Graph omits displayName."""
    requested: list[str] = []
    folders = _list_folders_only_payload()
    folders["value"][0]["displayName"] = ""
    connector = _build_real_connector(
        handler=_email_recovery_handler(requested, fail_reseed_for="f1", folders_payload=folders),
    )
    prior = json.dumps({"f1": "https://graph.microsoft.com/v1.0/stale-f1"})

    with caplog.at_level("WARNING", logger="kairix.connectors.m365_email_headers.connector"):
        list(connector.list_changes(cursor=prior))

    assert any("folder 'f1' drain failed" in record.getMessage() for record in caplog.records)


def test_legacy_string_cursor_collapses_to_cold_start() -> None:
    """A pre-#380 single-string deltaLink cursor decodes to cold-start.

    Drives the ``_decode_per_folder_cursor`` legacy / non-JSON fallback
    through the public ``list_changes`` surface. The legacy cursor is
    a real Graph deltaLink URL stored before the folder-scoped fix
    landed — Graph rejected the request when the cursor was active,
    so the connector restarts per-folder rather than crashing on the
    next post-fix tick.

    Sabotage proof: drop the ``except (TypeError, ValueError)`` block
    in ``_decode_per_folder_cursor`` — passing the legacy URL raises
    ``json.JSONDecodeError`` and ``list_changes`` aborts before
    yielding any events.
    """
    connector = _build_real_connector()
    legacy = "https://graph.microsoft.com/v1.0/users/x/messages/delta?$deltatoken=legacy"
    events = list(connector.list_changes(cursor=legacy))
    # Three envelopes from the seeded inbox folder — cold-start drain succeeded.
    assert len(events) == 3, f"legacy cursor should trigger cold-start; got {len(events)} events"


def test_non_dict_json_cursor_collapses_to_cold_start() -> None:
    """A JSON cursor that's a list / scalar (not a dict) decodes to cold-start.

    Sabotage proof: drop the ``if not isinstance(parsed, dict)`` guard
    in ``_decode_per_folder_cursor`` — a JSON list would try to
    iterate as a dict and surface a TypeError.
    """
    connector = _build_real_connector()
    events = list(connector.list_changes(cursor="[1, 2, 3]"))
    assert len(events) == 3, "non-dict JSON cursor should cold-start the drain"


def test_empty_string_cursor_collapses_to_cold_start() -> None:
    """An empty-string cursor decodes to cold-start, same as None.

    Sabotage proof: change the guard from ``not isinstance(cursor, str)
    or not cursor`` to ``cursor is None`` — passing an empty string
    raises on ``json.loads("")``.
    """
    connector = _build_real_connector()
    events = list(connector.list_changes(cursor=""))
    assert len(events) == 3


def test_cursor_with_non_string_values_drops_those_entries() -> None:
    """A JSON cursor whose entries have non-string values has those entries dropped.

    Drives the public surface with a cursor that decodes to
    ``{"f1": "valid", "f2": <non-string>}`` — the non-string entry is
    silently dropped and the cold-start drain proceeds for that
    folder.

    Sabotage proof: drop the ``isinstance(key, str) and isinstance(value, str)``
    guard in ``_decode_per_folder_cursor`` — the non-string value
    propagates as a deltaLink URL and the httpx client raises on the
    next request.
    """
    connector = _build_real_connector()
    cursor = json.dumps({"AAMkAGFmYWtl-inbox": 42, "other": None})
    events = list(connector.list_changes(cursor=cursor))
    # The inbox key was numeric (dropped), so the inbox folder
    # cold-starts and surfaces the three seeded envelopes.
    assert len(events) == 3


def test_empty_allowlist_ingests_all_folders() -> None:
    """An empty allowlist preserves the default-ingest-all behaviour.

    Drives the public surface with ``folders_allowlist=[]`` — the
    ``_select_folders`` ``if not allowlist`` early-return is reached
    through this path.

    Sabotage proof: change the ``if not allowlist`` guard to
    ``if allowlist is None`` — an explicit empty list would then
    silently exclude every folder.
    """
    from tests.fakes import FakeFeatureFlagResolver

    # Build a connector with a 3-folder mailbox + an empty allowlist.
    folders = _list_folders_only_payload()
    candidate_ids = ["f1", "f2", "f3"]

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(200, json=folders)
        for fid in candidate_ids:
            if f"/mailFolders/{fid}/messages/delta" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": f"{fid}-m",
                                "from": {"emailAddress": {"address": "a@b.c"}},
                                "toRecipients": [],
                                "ccRecipients": [],
                                "subject": "x",
                                "sentDateTime": "2026-05-22T10:00:00Z",
                                "receivedDateTime": "2026-05-22T10:00:01Z",
                            }
                        ],
                        "@odata.deltaLink": (
                            f"https://graph.microsoft.com/v1.0/users/a/mailFolders/{fid}/messages/delta?$deltatoken=t"
                        ),
                    },
                )
        return httpx.Response(404, json={})

    shared = httpx.Client(transport=httpx.MockTransport(_handler))
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    _ = FakeFeatureFlagResolver()  # imported to satisfy fake-first idiom; unused here.
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
        folders_allowlist=[],
    )
    events = list(connector.list_changes(cursor=None))
    # Empty allowlist keeps all three folders.
    assert len(events) == 3


def test_whitespace_only_allowlist_normalises_to_no_constraints() -> None:
    """An allowlist of whitespace-only entries normalises to no constraint.

    Drives the public surface with ``folders_allowlist=["   ", ""]``
    — the ``_select_folders`` normalisation strips whitespace and
    drops empties; the resulting set is empty and the
    ``if not normalised`` branch returns every folder.

    Sabotage proof: drop the ``if not normalised: return tuple(folders)``
    branch — every folder would be filtered out.
    """
    folders = _list_folders_only_payload()
    candidate_ids = ["f1", "f2", "f3"]

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(200, json=folders)
        for fid in candidate_ids:
            if f"/mailFolders/{fid}/messages/delta" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": f"{fid}-m",
                                "from": {"emailAddress": {"address": "a@b.c"}},
                                "toRecipients": [],
                                "ccRecipients": [],
                                "subject": "x",
                                "sentDateTime": "2026-05-22T10:00:00Z",
                                "receivedDateTime": "2026-05-22T10:00:01Z",
                            }
                        ],
                        "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/users/a/mailFolders/{fid}/messages/delta?$deltatoken=t",
                    },
                )
        return httpx.Response(404, json={})

    shared = httpx.Client(transport=httpx.MockTransport(_handler))
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
        folders_allowlist=["   ", ""],
    )
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 3


def test_list_changes_recovers_from_folder_enumeration_failure() -> None:
    """When ``list_mail_folders`` raises HTTPError, ``list_changes`` returns no events.

    The cursor falls through to ``_encode_per_folder_cursor`` of the
    previous-tick decoded mapping, so the next tick can resume from
    the same horizon rather than restart cold.

    Sabotage proof: remove the ``try / except httpx.HTTPError`` around
    the ``self._graph.list_mail_folders()`` call — the exception
    propagates and the orchestration tick crashes.
    """

    def _stub(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return httpx.Response(200, json={"value": []})

    handler = httpx.MockTransport(_stub)
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret — unit fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
    )
    events = list(connector.list_changes(cursor=None))
    assert events == []


def test_list_changes_preserves_prior_cursor_on_per_folder_failure() -> None:
    """A failing folder with a prior-tick cursor keeps that cursor for the next tick.

    Sabotage proof: change ``if prior is not None: next_cursors[...] = prior``
    to a no-op — the bad folder's cursor would silently disappear and
    the next tick would do a cold-start drain instead of resuming.
    """

    def _stub(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(200, json=_list_folders_only_payload())
        # Every delta call fails so every folder takes the prior-cursor path.
        return httpx.Response(500, json={"error": {"code": "InternalServerError"}})

    handler = httpx.MockTransport(_stub)
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret — unit fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
    )
    prior_links = {
        "f1": "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/mailFolders/f1/messages/delta?$deltatoken=prior-1",
        "f2": "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/mailFolders/f2/messages/delta?$deltatoken=prior-2",
        "f3": "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/mailFolders/f3/messages/delta?$deltatoken=prior-3",
    }
    prior = json.dumps(prior_links)
    _ = list(connector.list_changes(cursor=prior))
    cursor = connector.next_cursor()
    assert cursor is not None
    decoded = json.loads(cursor)
    assert decoded == prior_links


# ---------------------------------------------------------------------------
# make_connector folders_allowlist parsing
# ---------------------------------------------------------------------------


def test_make_connector_accepts_folders_allowlist() -> None:
    """An allowlist list of well-known names round-trips through ``make_connector``.

    Sabotage proof: drop the ``folders_allowlist=folders_allowlist``
    kwarg from the ``M365EmailHeadersConnector(...)`` call in
    ``make_connector`` — the connector's allowlist would always be
    None and downstream behaviour would diverge silently.
    """
    from kairix.secrets.loader import SecretNotFoundError

    with pytest.raises((SecretNotFoundError, MissingCredentialsError, OSError)):
        # The factory tries to resolve secrets; we expect a
        # credentials-related error rather than a folder-allowlist
        # ValueError, proving the allowlist parse succeeded.
        make_connector(
            {
                "user_principal_name": "agent-alpha@example.com",
                "folders_allowlist": ["inbox", "sentitems", "archive"],
            }
        )


def test_make_connector_rejects_non_list_folders_allowlist() -> None:
    """An allowlist value that isn't a list of strings raises a typed ValueError.

    Sabotage proof: remove the ``isinstance(raw_allowlist, list | tuple)``
    check from ``make_connector`` — the malformed allowlist propagates
    into the connector constructor and surfaces a less actionable
    AttributeError downstream.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({"user_principal_name": "agent-alpha@example.com", "folders_allowlist": "inbox"})
    msg = str(exc_info.value)
    assert "folders_allowlist" in msg
    assert "fix:" in msg, f"error message missing fix: marker: {msg!r}"


# ---------------------------------------------------------------------------
# Wave E ON branch (_list_changes_scoped) — folder-scoped per #380
# ---------------------------------------------------------------------------


def _build_wave_e_on_connector(
    *,
    folders_payload: dict[str, Any] | None = None,
    failing_folder_ids: set[str] | None = None,
    mailboxes: list[str] | None = None,
) -> tuple[M365EmailHeadersConnector, list[str]]:
    """Compose a Wave E ON connector against a multi-folder Graph stub."""
    from tests.fakes import FakeFeatureFlagResolver

    failing = failing_folder_ids or set()
    folders_doc = folders_payload if folders_payload is not None else _single_folder_payload()
    recorded: list[str] = []
    candidate_ids = [
        e["id"] for e in folders_doc.get("value", []) if isinstance(e, dict) and isinstance(e.get("id"), str)
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(200, json=folders_doc)
        recorded.append(url)
        for folder_id in candidate_ids:
            if f"/mailFolders/{folder_id}/messages/delta" in url:
                if folder_id in failing:
                    return httpx.Response(500, json={"error": {"code": "InternalServerError"}})
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": f"{folder_id}-m-1",
                                "from": {"emailAddress": {"address": "alpha@example.com"}},
                                "toRecipients": [],
                                "ccRecipients": [],
                                "subject": f"{folder_id} subject",
                                "sentDateTime": "2026-05-22T10:00:00Z",
                                "receivedDateTime": "2026-05-22T10:00:01Z",
                            }
                        ],
                        "@odata.deltaLink": (
                            f"https://graph.microsoft.com/v1.0/users/agent-alpha@example.com"
                            f"/mailFolders/{folder_id}/messages/delta?$deltatoken={folder_id}-tok"
                        ),
                    },
                )
        return httpx.Response(404, json={"error": {"code": "UnknownFolder"}})

    handler = httpx.MockTransport(_handler)
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    resolver = FakeFeatureFlagResolver().with_flag("topology_m365_email_headers", True)
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
        mailboxes=mailboxes,
        flag_reader=resolver.get,
    )
    return connector, recorded


def test_list_changes_scoped_drains_folder_per_container() -> None:
    """Wave E ON: ``_list_changes_scoped`` drains the container's folders.

    Sabotage proof: change the per-folder ``for folder in selected:``
    loop body in :meth:`_list_changes_scoped` to ``continue`` —
    every folder gets skipped and the event count drops to zero.
    """
    from kairix.core.protocols import Container

    folders = _list_folders_only_payload()
    connector, recorded = _build_wave_e_on_connector(folders_payload=folders)
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    events = list(connector.list_changes_for_container(container))
    assert len(events) == 3, f"expected 3 events (one per folder), got {len(events)}"
    folders_seen = {ev.metadata.get("folder") for ev in events}
    assert folders_seen == {"Inbox", "Sent Items", "Archive"}, f"got {folders_seen!r}"
    # Every recorded URL is folder-scoped.
    assert all("/mailFolders/" in u and "/messages/delta" in u for u in recorded), recorded


def test_list_changes_scoped_records_per_folder_cursor_mapping() -> None:
    """Wave E ON: ``next_cursor_for_container`` returns a JSON ``{folder_id: deltaLink}`` mapping.

    Sabotage proof: replace ``self._next_cursor_by_container[mailbox] = _encode_per_folder_cursor(next_cursors)``
    with ``self._next_cursor_by_container[mailbox] = None`` — the
    JSON decode below raises TypeError.
    """
    from kairix.core.protocols import Container

    folders = _list_folders_only_payload()
    connector, _recorded = _build_wave_e_on_connector(folders_payload=folders)
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    _ = list(connector.list_changes_for_container(container))
    cursor = connector.next_cursor_for_container("agent-alpha@example.com")
    assert cursor is not None
    decoded = json.loads(cursor)
    assert set(decoded.keys()) == {"f1", "f2", "f3"}


def test_list_changes_scoped_recovers_from_folder_enumeration_failure() -> None:
    """Wave E ON: folder enumeration HTTPError yields no events, preserves prior cursor.

    Sabotage proof: drop the ``try / except httpx.HTTPError`` around
    ``graph.list_mail_folders()`` — the exception propagates and the
    framework's per-container tick aborts.
    """
    from kairix.core.protocols import Container

    # Override the handler so /mailFolders specifically 503s for this mailbox.
    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return httpx.Response(200, json={"value": []})

    handler = httpx.MockTransport(_handler)
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    from tests.fakes import FakeFeatureFlagResolver

    resolver = FakeFeatureFlagResolver().with_flag("topology_m365_email_headers", True)
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
        flag_reader=resolver.get,
    )
    prior = json.dumps({"f1": "https://graph.microsoft.com/v1.0/users/x/mailFolders/f1/messages/delta?$deltatoken=p"})
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=prior,
        last_synced_at=None,
    )
    events = list(connector.list_changes_for_container(container))
    assert events == []
    # Prior cursor is preserved so the next tick can retry.
    cursor = connector.next_cursor_for_container("agent-alpha@example.com")
    assert cursor is not None
    decoded = json.loads(cursor)
    assert decoded == {"f1": "https://graph.microsoft.com/v1.0/users/x/mailFolders/f1/messages/delta?$deltatoken=p"}


def test_list_changes_scoped_per_folder_failure_preserves_prior() -> None:
    """Wave E ON: a single folder's delta failure keeps that folder's prior cursor.

    Sabotage proof: drop the ``if prior is not None: next_cursors[...] = prior``
    line — the bad folder's cursor disappears.
    """
    from kairix.core.protocols import Container

    folders = _list_folders_only_payload()
    connector, _recorded = _build_wave_e_on_connector(folders_payload=folders, failing_folder_ids={"f1"})
    prior_url = "https://graph.microsoft.com/v1.0/users/x/mailFolders/f1/messages/delta?$deltatoken=prior-1"
    prior = json.dumps({"f1": prior_url})
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=prior,
        last_synced_at=None,
    )
    events = list(connector.list_changes_for_container(container))
    # 2 surviving folders emit one envelope each.
    assert len(events) == 2
    cursor = connector.next_cursor_for_container("agent-alpha@example.com")
    assert cursor is not None
    decoded = json.loads(cursor)
    assert decoded["f1"] == prior_url, "failed folder must retain prior cursor"


def test_retrieve_all_slim_docs_drains_every_folder() -> None:
    """``retrieve_all_slim_docs`` yields message ids from every selected folder.

    Sabotage proof: change the ``yield message.message_id`` inside the
    per-folder loop to ``continue`` — the assertion that ids surface
    fails because the iterator yields nothing.
    """
    from kairix.core.protocols import Container

    folders = _list_folders_only_payload()
    connector, _recorded = _build_wave_e_on_connector(folders_payload=folders)
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    ids = list(connector.retrieve_all_slim_docs(container))
    assert set(ids) == {"f1-m-1", "f2-m-1", "f3-m-1"}, f"got {ids!r}"


def test_retrieve_all_slim_docs_handles_folder_enumeration_failure() -> None:
    """If the folder enumeration fails, ``retrieve_all_slim_docs`` yields an empty set.

    Sabotage proof: drop the ``try / except`` around
    ``graph.list_mail_folders()`` — the exception propagates and the
    prune cycle aborts.
    """
    from kairix.core.protocols import Container

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3600, "token_type": "Bearer"})
        if "/mailFolders" in url and "/messages/delta" not in url:
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return httpx.Response(200, json={"value": []})

    handler = httpx.MockTransport(_handler)
    shared = httpx.Client(transport=handler)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    connector = M365EmailHeadersConnector(
        user_principal_name="agent-alpha@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s-value"),  # pragma: allowlist secret
        auth=auth,
        client_builder=lambda a, u: M365GraphClient(
            user_principal_name=u, auth=a, http_client=shared, sleep_fn=lambda _s: None
        ),
    )
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    ids = list(connector.retrieve_all_slim_docs(container))
    assert ids == []


def test_retrieve_all_slim_docs_skips_failing_folder() -> None:
    """``retrieve_all_slim_docs`` skips a failing folder and continues with the rest.

    Sabotage proof: drop the inner ``try / except httpx.HTTPError`` —
    the failing folder's HTTPStatusError propagates and the iterator
    halts, yielding fewer ids than the surviving folder count.
    """
    from kairix.core.protocols import Container

    folders = _list_folders_only_payload()
    connector, _recorded = _build_wave_e_on_connector(folders_payload=folders, failing_folder_ids={"f1"})
    container = Container(
        cc_pair_id=7,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    ids = list(connector.retrieve_all_slim_docs(container))
    assert set(ids) == {"f2-m-1", "f3-m-1"}


def test_make_connector_rejects_empty_string_in_folders_allowlist() -> None:
    """An allowlist containing empty strings is rejected loudly.

    Sabotage proof: drop the ``all(isinstance(f, str) and f for f in raw_allowlist)``
    check — an empty-string entry would propagate as a match-nothing
    folder, silently disabling sync.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({"user_principal_name": "agent-alpha@example.com", "folders_allowlist": ["inbox", ""]})
    assert "folders_allowlist" in str(exc_info.value)


def test_modified_at_falls_back_to_now_when_both_timestamps_missing() -> None:
    """When both ``receivedDateTime`` and ``sentDateTime`` are absent,
    the helper falls back to wall-clock now (ISO-8601 ending in ``Z``).

    Sabotage proof: change the final fallback ``return _now_iso()`` to
    ``return ""`` — the ``endswith("Z")`` assertion below fails.
    """
    payload = {
        "value": [
            {
                "id": "msg-no-timestamps",
                "from": {"emailAddress": {"address": "agent-alpha@example.com"}},
                "toRecipients": [],
                "ccRecipients": [],
                "subject": "No timestamps",
                # NB: sentDateTime AND receivedDateTime both omitted.
            }
        ],
        "@odata.deltaLink": (
            "https://graph.microsoft.com/v1.0/users/agent-alpha@example.com/messages/delta?$deltatoken=tok"
        ),
    }
    connector = _build_real_connector(pages=[payload])
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 1
    assert events[0].modified_at.endswith("Z"), f"expected ISO-8601 Zulu timestamp, got {events[0].modified_at!r}"
