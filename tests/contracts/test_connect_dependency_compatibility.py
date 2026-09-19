"""Runtime contracts for the connect and CalDAV dependency boundaries.

These checks use the real third-party libraries without network calls or
module substitution.  They protect the two production paths affected by the
dependency upgrade:

* GitHub App RS256 signing through PyJWT's cryptography backend.
* CalDAV event parsing through caldav and icalendar into Kairix's typed record.
"""

from __future__ import annotations

from importlib.metadata import version

import pytest

from kairix.connect.oauth2.github_app import JWT_ALGORITHM
from kairix.connectors.apple_caldav.client import AppleCalDavClient

pytestmark = pytest.mark.contract


def _release_version(distribution: str) -> tuple[int, ...]:
    """Return the numeric release components without pinning future patches."""
    public = version(distribution).partition("+")[0]
    return tuple(int(component) for component in public.split("."))


def test_github_app_rs256_signing_uses_supported_cryptography_backend() -> None:
    """The installed PyJWT/cryptography pair signs and verifies an App JWT."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    assert _release_version("cryptography") >= (50, 0, 1)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    payload = {"iat": 1_700_000_000, "exp": 1_700_000_600, "iss": "12345"}

    encoded = jwt.encode(payload, private_pem, algorithm=JWT_ALGORITHM)
    decoded = jwt.decode(
        encoded,
        private_key.public_key(),
        algorithms=[JWT_ALGORITHM],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert decoded == payload


def test_apple_caldav_parses_real_icalendar_event_into_typed_record() -> None:
    """The installed caldav/icalendar pair preserves Kairix event fields."""
    from caldav import Event

    assert _release_version("icalendar") >= (7, 3, 0)

    raw_ics = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Kairix//Dependency Contract//EN\r
BEGIN:VEVENT\r
UID:dependency-contract-event\r
DTSTAMP:20260919T010203Z\r
DTSTART:20260920T100000Z\r
DTEND:20260920T110000Z\r
SUMMARY:Dependency contract\r
LOCATION:Sydney\r
ORGANIZER:mailto:owner@example.com\r
ATTENDEE:mailto:guest@example.com\r
RRULE:FREQ=WEEKLY;COUNT=2\r
LAST-MODIFIED:20260919T010203Z\r
END:VEVENT\r
END:VCALENDAR\r
"""
    event_url = "https://caldav.example/calendars/dependency-contract-event.ics"
    event = Event(url=event_url, data=raw_ics)
    client = object.__new__(AppleCalDavClient)

    record = client._parse_event(event)

    assert record.event_id == "dependency-contract-event"
    assert record.summary == "Dependency contract"
    assert record.location == "Sydney"
    assert record.attendees == ("mailto:guest@example.com",)
    assert record.organiser == "mailto:owner@example.com"
    assert "FREQ" in record.recurrence_rule
    assert "WEEKLY" in record.recurrence_rule
    assert "UID:dependency-contract-event" in record.raw_ics
    assert record.event_url == event_url
