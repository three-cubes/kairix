"""Shared step definitions for per-provider BDD feature files (#provider-plugin-arch IM-7).

The seven provider feature files
(``provider_{azure_foundry,azure_legacy,openai,bedrock,ollama,litellm_proxy,anthropic}.feature``)
share many step phrases verbatim — the Background "wire-endpoint
fixture" line, the "configured endpoint is <url>" Given, the
"recorded request host / path / header" Then assertions, and the
"error message names the configured provider as <name>" assertion.

Defining the same phrase in multiple step modules would either silently
shadow (most-recently-registered wins under pytest-bdd 8) or produce
ambiguous matches. Centralising the shared phrases here keeps the
per-provider modules narrow — they only own the provider-specific
``Given <provider> provider configured with ...`` and ``When the
operator <does X> via the <provider> plugin`` steps, plus the
provider-name-bearing ``Then the <provider> plugin raises a canonical
<ErrorType> error`` assertions.

State is held in a single ``_provider_wire_state`` fixture keyed by
provider name (set in the per-provider module's ``Given <provider>
provider configured with ...`` step). The per-provider ``When`` step
reads the configured name and dispatches to the correct factory; the
recording transport client lives in the per-provider module so the
host / path / header capture matches what the real plugin sends.

F1-clean, F2-clean, F5-clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Recorded-request shape — shared across all provider step modules
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """Synthetic wire-shape snapshot recorded by every provider's fake
    transport client.

    The fake transport client constructed in each per-provider module
    appends a :class:`RecordedRequest` per outbound call. The
    assertion steps below read the last entry off the
    ``recorded_requests`` list on the configured transport (via the
    shared ``_last_recorded`` helper) so wire-shape assertions are
    identical across every plugin even when the per-plugin auth /
    URL-suffix rules differ.

    Fields:

    - ``host``: the URL authority (``api.openai.com``,
      ``example-resource.services.ai.azure.com``, ``localhost:11434``).
    - ``path``: the URL path (``/openai/v1/embeddings``,
      ``/v1/embeddings``, ``/api/embeddings``).
    - ``headers``: request headers — every plugin records its actual
      auth header here (``api-key`` for Azure, ``Authorization: Bearer``
      for OpenAI / LiteLLM, ``x-api-key`` + ``anthropic-version`` for
      Anthropic, AWS SigV4 ``Authorization`` for Bedrock, none for Ollama).
    - ``body``: the request body — the openai-SDK kwargs (``model``,
      ``input``, ``messages``, ``max_tokens``, ``dimensions``) verbatim.
    - ``query``: parsed query parameters (used by ``azure_legacy``'s
      ``api-version`` assertions).
    """

    host: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any]
    query: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def _provider_wire_state() -> dict[str, Any]:
    """Per-scenario state shared by every per-provider step module.

    ``provider_name`` is populated by the per-provider Background step
    (``the <name> provider configured with ...``). The ``transport``
    slot starts as ``None``; one of the error-state ``Given`` steps
    below may pre-populate it with a ``{"raise_status": N, "headers":
    {...}}`` dict, in which case the per-provider ``When`` step
    constructs a *raising* fake transport. Otherwise the ``When`` step
    constructs the per-provider *recording* fake transport (whose
    recorded_requests list is the assertion surface).
    """
    return {
        "provider_name": None,
        "endpoint": None,
        "api_key": None,
        "model": None,
        "extra": {},
        "transport": None,
        "raised": None,
        "no_outbound_recorded": False,
    }


def last_recorded(state: dict[str, Any]) -> RecordedRequest:
    """Return the last :class:`RecordedRequest` captured by the transport.

    The per-provider module's fake transport client appends to a
    ``recorded_requests`` list exposed as an attribute on the client.
    This helper is the canonical accessor.
    """
    transport = state["transport"]
    recorded = getattr(transport, "recorded_requests", None)
    assert recorded is not None, (
        "configured transport does not expose a recorded_requests list; did the When step build the recording fake?"
    )
    assert recorded, "no outbound request was recorded by the wire endpoint"
    return recorded[-1]


# ---------------------------------------------------------------------------
# Given — shared wiring
# ---------------------------------------------------------------------------


@given("a wire-endpoint fixture that records every outbound request")
def _given_wire_fixture(_provider_wire_state: dict[str, Any]) -> None:
    """Reset the recording slot.

    The per-provider ``When`` step instantiates the recording transport
    once endpoint + api_key are known. Documented as a no-op so
    pytest-bdd's strict step matching binds the Background step.
    """
    _provider_wire_state["transport"] = None


@given(parsers.parse('the configured endpoint is "{endpoint}"'))
def _given_endpoint(_provider_wire_state: dict[str, Any], endpoint: str) -> None:
    _provider_wire_state["endpoint"] = endpoint


@given("the wire endpoint will respond with status 429 and a Retry-After header")
def _given_429(_provider_wire_state: dict[str, Any]) -> None:
    """Mark the transport as 'will raise 429 with Retry-After: 7'.

    The fixed Retry-After value of ``7`` seconds is asserted by the
    shared "error carries the upstream retry-after hint" step below so
    the parsed float flowing through ``_retry_after_of`` is the same
    across every plugin.
    """
    _provider_wire_state["transport"] = {"raise_status": 429, "headers": {"Retry-After": "7"}}


@given("the wire endpoint will respond with status 401")
def _given_401(_provider_wire_state: dict[str, Any]) -> None:
    _provider_wire_state["transport"] = {"raise_status": 401, "headers": {}}


@given("the wire endpoint will respond with status 500")
def _given_500(_provider_wire_state: dict[str, Any]) -> None:
    _provider_wire_state["transport"] = {"raise_status": 500, "headers": {}}


# ---------------------------------------------------------------------------
# Then — shared wire-shape assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the recorded request host is "{host}"'))
def _then_host(_provider_wire_state: dict[str, Any], host: str) -> None:
    """Sabotage-proof: corrupt the endpoint host in any recording fake
    → host mismatch, this fails. Provider-agnostic.
    """
    req = last_recorded(_provider_wire_state)
    assert req.host == host, f"expected host {host!r}; got {req.host!r}"


@then(parsers.parse('the recorded request path begins with "{prefix}"'))
def _then_path_prefix(_provider_wire_state: dict[str, Any], prefix: str) -> None:
    """Sabotage-proof: drop the per-plugin URL-suffix logic → path no
    longer matches the expected prefix, this fails. Provider-agnostic.
    """
    req = last_recorded(_provider_wire_state)
    assert req.path.startswith(prefix), f"expected path to begin with {prefix!r}; got {req.path!r}"


@then(parsers.parse('the recorded request path does not contain "{needle}"'))
def _then_path_not_contains(_provider_wire_state: dict[str, Any], needle: str) -> None:
    """Sabotage-proof: regress URL handling to always append a suffix
    → path contains the forbidden substring, this fails.
    """
    req = last_recorded(_provider_wire_state)
    assert needle not in req.path, f"path unexpectedly contains {needle!r}: {req.path!r}"


@then(parsers.parse('the recorded request path equals "{path}"'))
def _then_path_equals(_provider_wire_state: dict[str, Any], path: str) -> None:
    """Sabotage-proof: any path mutation in the recording fake fails the equality."""
    req = last_recorded(_provider_wire_state)
    assert req.path == path, f"expected path {path!r}; got {req.path!r}"


@then(parsers.parse('the recorded request header "{name}" equals "{value}"'))
def _then_header_equals(_provider_wire_state: dict[str, Any], name: str, value: str) -> None:
    """Sabotage-proof: rename or omit the auth header in any plugin →
    lookup misses, this fails.
    """
    req = last_recorded(_provider_wire_state)
    actual = req.headers.get(name)
    assert actual == value, f"header {name!r} expected {value!r}; got {actual!r}"


@then(parsers.parse('the recorded request header "{name}" is set'))
def _then_header_present(_provider_wire_state: dict[str, Any], name: str) -> None:
    req = last_recorded(_provider_wire_state)
    assert name in req.headers, f"expected header {name!r} to be present; headers were {list(req.headers)!r}"


@then(parsers.parse('the recorded request has no header named "{name}"'))
def _then_header_absent(_provider_wire_state: dict[str, Any], name: str) -> None:
    req = last_recorded(_provider_wire_state)
    assert name not in req.headers, f"expected header {name!r} absent; got headers {list(req.headers)!r}"


@then(parsers.parse('the recorded request body contains model "{model}"'))
def _then_body_model(_provider_wire_state: dict[str, Any], model: str) -> None:
    """Sabotage-proof: drop the ``model=`` kwarg from any plugin's
    embed/chat call → recorded body has no model key, this fails.
    """
    req = last_recorded(_provider_wire_state)
    assert req.body.get("model") == model, f"recorded body model expected {model!r}; got {req.body.get('model')!r}"


@then("the error carries the upstream retry-after hint")
def _then_retry_after(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: stop parsing Retry-After in any plugin's
    ``_retry_after_of`` → ``retry_after_s`` is None, this fails.

    The retry-after value of ``7.0`` seconds is fixed by the shared
    "wire endpoint will respond with status 429" Given step above so
    the assertion target is constant across every plugin.
    """
    from kairix.providers import RateLimited

    err = _provider_wire_state["raised"]
    assert isinstance(err, RateLimited), f"expected RateLimited; got {type(err).__name__ if err else 'None'}"
    assert err.retry_after_s == 7.0, f"expected retry-after 7.0s; got {err.retry_after_s!r}"


@then(parsers.parse('the error message names the configured provider as "{provider}"'))
def _then_error_names_provider(_provider_wire_state: dict[str, Any], provider: str) -> None:
    """Sabotage-proof: drop the ``provider_name`` interpolation OR the
    provider's brand-name prefix in ``_map_transport_error`` → no form
    of the provider name appears in the message, this fails.

    Matched case-insensitively because plugins use a brand-name prefix
    in the message (``"OpenAI upstream error..."``, ``"Azure Foundry
    rate-limited..."``) plus the lowercase canonical name in the
    ``provider_name`` interpolation. Either form satisfies the spec —
    the test asserts that the operator can identify which plugin
    surfaced the error from the message text.
    """
    err = _provider_wire_state["raised"]
    assert err is not None, "expected an error to have been raised"
    message = str(err).lower()
    # The canonical provider name (e.g. "openai", "azure_foundry") may
    # appear as itself or via the brand-name prefix the plugin emits
    # ("OpenAI", "Azure Foundry"). Normalise both before the match.
    needle = provider.lower().replace("_", " ")
    needle_underscored = provider.lower()
    assert needle in message or needle_underscored in message, (
        f"expected {provider!r} (or its brand form) in error message; got {err!s}"
    )


@then("no outbound request was recorded by the wire endpoint")
def _then_no_outbound(_provider_wire_state: dict[str, Any]) -> None:
    """Sabotage-proof: if a plugin that shouldn't emit a request
    (e.g. anthropic embed_batch) starts emitting one anyway, the
    transport's recorded_requests list would be non-empty, failing here.
    """
    transport = _provider_wire_state["transport"]
    recorded = getattr(transport, "recorded_requests", None) if transport else None
    assert not recorded, f"expected no outbound; recorded_requests={recorded!r}"
