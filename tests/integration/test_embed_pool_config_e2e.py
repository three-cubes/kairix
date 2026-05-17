"""Integration: pool config flows through factory + real httpx client.

Drives ``make_openai_client`` with explicit pool kwargs and asserts on
the post-construction httpx.Limits. The env-read mechanism that supplies
those kwargs in production is unit-tested separately in tests/test_paths.py;
this layer pins the wiring from kwarg through SDK construction to wire-
visible pool state.

No env-monkeypatch (F2 — env state leaks between tests; pool config is
expressed as explicit kwargs on the public factory surface).
"""

from __future__ import annotations

import pytest

from kairix.credentials import make_openai_client

pytestmark = pytest.mark.integration


_FOUNDRY_ENDPOINT = "https://example.services.ai.azure.com"
_LEGACY_AZURE_ENDPOINT = "https://example.openai.azure.com"
_OPENAI_DIRECT_ENDPOINT = "https://api.openai.com/v1"


def _read_pool_state(client: object) -> tuple[int, int]:
    """Return (max_connections, max_keepalive) from the constructed client.

    The openai SDK stores the supplied httpx.Client at ``client._client``;
    its underlying transport pool exposes the configured Limits as
    ``_max_connections`` / ``_max_keepalive_connections``. This is the
    integration-time observable proving the configured kwargs reached the wire.
    """
    pool = client._client._transport._pool  # type: ignore[attr-defined] — openai SDK exposes the supplied httpx.Client via its private _client attr; this is the documented integration-time observable for verifying configured pool kwargs reached the wire
    return pool._max_connections, pool._max_keepalive_connections


@pytest.mark.integration
def test_pool_config_reaches_httpx_limits_across_all_three_endpoint_shapes() -> None:
    """Pool kwargs flow through all three branches of make_openai_client.

    Foundry / legacy-Azure / OpenAI-direct each construct their own SDK
    client wrapper; the pool config has to thread through all three or
    one of the branches silently no-ops the operator's tuning. Sabotage:
    drop the ``http_client=`` kwarg from one of the three branches and
    that endpoint-shape's assertion fires (its pool stays at the SDK
    default).
    """
    for endpoint in (_FOUNDRY_ENDPOINT, _LEGACY_AZURE_ENDPOINT, _OPENAI_DIRECT_ENDPOINT):
        client = make_openai_client(
            api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
            endpoint=endpoint,
            pool_max_connections=42,
        )
        max_conns, _ = _read_pool_state(client)
        assert max_conns == 42, f"endpoint {endpoint}: expected 42 max_connections, got {max_conns}"


@pytest.mark.integration
def test_default_pool_state_when_kwargs_unset() -> None:
    """No kwargs and no env overrides → defaults (20 / 10) reach the underlying pool.

    The defaults ARE the operator contract for an unconfigured deployment.
    Sabotage: change the default constants in ``kairix.credentials`` and
    the assertion fails.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
    )
    max_conns, max_keepalive = _read_pool_state(client)
    assert max_conns == 20
    assert max_keepalive == 10


@pytest.mark.integration
def test_pool_size_and_keepalive_independently_overridable() -> None:
    """Operator can set max_connections without touching keepalive (and vice versa).

    Sabotage: collapse the two reads into one (e.g. derive keepalive from
    pool_size) and the test fails — operators tune them independently.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
        pool_max_connections=60,
        pool_max_keepalive=5,
    )
    max_conns, max_keepalive = _read_pool_state(client)
    assert max_conns == 60
    assert max_keepalive == 5


# The "kwarg beats env" precedence contract is unit-tested in
# tests/test_credentials.py (which IS F2-baselined because credentials
# wraps secret env-vars where env IS the public interface). Integration
# tests stay F2-clean — env state must not leak between integration
# scenarios. The kwarg-routes-to-Limits contract above is sufficient
# for the integration layer's promise.
