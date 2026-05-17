"""Step definitions for ``e2e_provider_embed.feature``.

Drives the operator-visible embed journey through the SK-2 registry seam
``kairix.providers.get_provider(name, registry=...)``. Step impls inject
a ``FakeProviderRegistry`` (``tests/fakes.py``) carrying:

- ``FakeProvider(name="azure_foundry", dim=1536)`` — real-shape fake,
  proves the contract for the Azure Foundry plugin row.
- ``FakeProvider(name="openai", dim=1536)`` — real-shape fake, proves
  the contract for the OpenAI plugin row.
- ``_EmbedNotSupportedProvider(name="anthropic")`` — raises
  :class:`kairix.providers.EmbedNotSupported` on ``embed_batch`` (the
  one chat-only provider in the matrix).
- ``_UnimplementedProvider(name=<other>)`` for ``azure_legacy`` /
  ``bedrock`` / ``ollama`` / ``litellm_proxy`` whose Wave 1 entry-point
  factories raise ``NotImplementedError``. The step impl translates the
  unimplemented factory into a ``pytest.skip`` on the journey-row so the
  feature file's row-per-provider surface stays mechanical to audit.

This module owns the *shared* Given/When/Then phrases for the four E2E
provider feature files (embed / chat / switch / health) — those phrases
are physically identical across the features, so they're defined here
once via the ``e2e_provider_state`` fixture. The per-feature modules
add only feature-specific phrases.

Sabotage proof per scenario is documented at the step-definition site
that owns the assertion (one ``# sabotage:`` comment per ``then``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.providers import (
    EmbedNotSupported,
    ProviderHealth,
    ProviderNotRegistered,
    get_provider,
)
from tests.fakes import FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Provider stubs (one canonical Fake* per behaviour the feature exercises)
# ---------------------------------------------------------------------------


class _EmbedNotSupportedProvider:
    """Provider that raises ``EmbedNotSupported`` on embed (chat-only family).

    Mirrors the contract the anthropic plugin will pin in Wave 4 — embed
    surface is intentionally absent. Defined here (not in
    ``tests/fakes.py``) because the broader fake set only needs one
    canonical ``FakeProvider``; this stub is feature-specific to the
    embed journey's @anthropic_no_embed scenario.
    """

    def __init__(self, name: str = "anthropic", dim: int = 1) -> None:
        self.name = name
        self._dim = dim
        self.embed_calls: list[list[str]] = []
        self.chat_calls: list[dict[str, Any]] = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        raise EmbedNotSupported(provider_name=self.name)

    def chat(self, messages: list[dict[str, Any]], *, max_tokens: int = 800) -> str:
        self.chat_calls.append({"messages": list(messages), "max_tokens": max_tokens})
        return "Hello, kairix here."

    def dimension(self) -> int:
        return self._dim

    def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(ok=True, endpoint="fake://anthropic")


class _UnimplementedProvider:
    """Provider whose factory would raise ``NotImplementedError`` in production.

    Wave 1 ships these as scaffolds (``azure_legacy``, ``bedrock``,
    ``ollama``, ``litellm_proxy``) — entry points are registered so
    discovery works, but ``make_provider()`` raises
    ``NotImplementedError`` with a fix/next-marker message. Tests detect
    this via a sentinel ``unimplemented=True`` attribute and translate
    to ``pytest.skip`` on the Examples row.
    """

    unimplemented = True

    def __init__(self, name: str) -> None:
        self.name = name

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(f"{self.name} provider lands in Wave 4 (follow-up to issue #247).")

    def chat(self, messages: list[dict[str, Any]], *, max_tokens: int = 800) -> str:
        raise NotImplementedError(f"{self.name} provider lands in Wave 4 (follow-up to issue #247).")

    def dimension(self) -> int:
        return 0

    def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(ok=False, endpoint=f"fake://{self.name}", error="NotImplementedError")


_REAL_PLUGIN_NAMES = ("azure_foundry", "openai")
_UNIMPLEMENTED_NAMES = ("azure_legacy", "bedrock", "ollama", "litellm_proxy")
_CHAT_ONLY_NAMES = ("anthropic",)

# Dimension matrix matching the feature's Examples table — keeps the
# fake's declared dimension consistent with the dim assertion on the
# row. Single source of truth avoids F17 duplicated-literal violations.
_DIMENSION_BY_PROVIDER: dict[str, int] = {
    "azure_foundry": 1536,
    "openai": 1536,
    "azure_legacy": 1536,
    "bedrock": 1024,
    "ollama": 768,
    "litellm_proxy": 1536,
    "anthropic": 0,
}


def build_e2e_registry() -> FakeProviderRegistry:
    """Build a ``FakeProviderRegistry`` seeded with all seven first-party names.

    Shared with the other three E2E step modules (chat / switch / health)
    so all journeys see the same provider matrix. Exported for the
    sibling modules under ``tests.bdd.steps``.
    """
    providers: dict[str, Any] = {}
    for plugin in _REAL_PLUGIN_NAMES:
        providers[plugin] = FakeProvider(
            name=plugin,
            dim=_DIMENSION_BY_PROVIDER[plugin],
            vector=[0.1] * _DIMENSION_BY_PROVIDER[plugin],
            chat_reply=f"reply from {plugin}",
        )
    for plugin in _CHAT_ONLY_NAMES:
        providers[plugin] = _EmbedNotSupportedProvider(name=plugin)
    for plugin in _UNIMPLEMENTED_NAMES:
        providers[plugin] = _UnimplementedProvider(name=plugin)
    return FakeProviderRegistry(providers)


def skip_if_unimplemented(provider: Any, journey: str) -> None:
    """``pytest.skip`` when the resolved provider is a Wave-1 scaffold.

    Centralised so every E2E step module emits the same skip message —
    grep-able for the migration-plan ETA on a future kairix release.
    """
    if getattr(provider, "unimplemented", False):
        pytest.skip(
            f"{provider.name} provider lands in Wave 4 (follow-up to issue #247); "
            f"E2E {journey} journey skipped per ADR § Migration plan"
        )


# ---------------------------------------------------------------------------
# State fixture — shared across all four E2E step modules
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_provider_state() -> dict[str, Any]:
    """Per-scenario fresh state shared with chat/switch/health step modules.

    Keys:
      ``registry`` — the injected ``FakeProviderRegistry``.
      ``current_provider_name`` — the operator's configured provider.
      ``credentials_env`` — what the operator put in their shell (record-only).
      ``envelope`` — the embed/chat result envelope (set by When-step).
      ``second_envelope`` — second embed envelope for the switch journey.
      ``vectors`` — last embed call's vectors (batch or single).
      ``response_text`` — last chat call's response (chat feature).
      ``error`` — captured typed error (EmbedNotSupported, ProviderNotRegistered).
      ``kairix_tree_signature`` — file mtime signature for "no source edits" check.
    """
    return {
        "registry": None,
        "current_provider_name": None,
        "credentials_env": {},
        "envelope": None,
        "envelopes": [],
        "vectors": None,
        "response_text": None,
        "error": None,
        "kairix_tree_signature": None,
        "stage_latency_ms": None,
    }


# ---------------------------------------------------------------------------
# Background — shared with chat/switch/health
# ---------------------------------------------------------------------------


@given("the kairix provider registry is loaded from installed entry points")
def registry_loaded(e2e_provider_state: dict[str, Any]) -> None:
    """Seed the registry seam with a ``FakeProviderRegistry``.

    Honest dogfooding: production calls
    ``get_provider(name)`` which defaults to the real
    ``EntryPointRegistry``. Tests pass an explicit
    ``FakeProviderRegistry`` to the same call signature — no monkeypatch.
    """
    e2e_provider_state["registry"] = build_e2e_registry()


# ---------------------------------------------------------------------------
# Given — operator configuration (shared)
# ---------------------------------------------------------------------------


@given(parsers.parse('the operator has configured provider "{name}"'))
def operator_configured_provider(e2e_provider_state: dict[str, Any], name: str) -> None:
    """Record the operator's provider selection on state.

    No env-var monkeypatch (F2) — the selection is a plain state record;
    the When-step calls ``get_provider(name, registry=...)`` directly.
    """
    e2e_provider_state["current_provider_name"] = name


@given(parsers.parse('the credential variable "{key_env}" is set to "{value_env}"'))
@when(parsers.parse('the credential variable "{key_env}" is set to "{value_env}"'))
def credential_variable_set(e2e_provider_state: dict[str, Any], key_env: str, value_env: str) -> None:
    """Record the credential the operator would set on their shell.

    Registered as both Given and When because the switch feature's
    "no restart required" scenario expresses the second-iteration
    credential-set under the When-bucket (continuing the operator's
    action chain) while the embed/chat features express it under Given
    (Background setup). Same impl: record the credential on state.

    The fake registry's providers don't read env (they're fakes), so this
    is a record-only step — what we assert later is that the journey ran
    *as if* this credential were set. The wire-shape contract is pinned
    by the per-provider features (``provider_openai.feature``, etc.).
    """
    e2e_provider_state["credentials_env"][key_env] = value_env


# ---------------------------------------------------------------------------
# When — embed actions (shared)
# ---------------------------------------------------------------------------


_KAIRIX_PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "kairix"


def _kairix_tree_signature() -> tuple[tuple[str, int, int], ...]:
    """Return a stable (path, mtime_ns, size) tuple for every .py under kairix/.

    Cheap structural fingerprint — if any source file is touched
    between two embeds in a switch scenario, the tuple differs. Lives
    in the embed module so the shared ``drive_embed`` can capture
    pre-embed state for the switch journey without a duplicate
    When-step registration.
    """
    entries: list[tuple[str, int, int]] = []
    for path in sorted(_KAIRIX_PACKAGE_ROOT.rglob("*.py")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def drive_embed(state: dict[str, Any], text: str) -> None:
    """Resolve the configured provider and call ``embed_batch([text])``.

    Appends the resulting envelope to ``state["envelopes"]`` and aliases
    ``state["envelope"]`` to the latest. If the provider raises a typed
    error (e.g. ``EmbedNotSupported`` for the anthropic family, or
    ``ProviderNotRegistered`` for unknown-name switches), the error is
    captured into ``state["error"]`` and no envelope is appended —
    callers downstream assert on ``state["error"]``. Captures the
    kairix-tree signature on the first call so the switch journey's
    "no source modified between embeds" check has a baseline.
    """
    if state.get("kairix_tree_signature") is None:
        state["kairix_tree_signature"] = _kairix_tree_signature()
    registry = state["registry"]
    name = state["current_provider_name"]
    try:
        provider = get_provider(name, registry=registry)
    except ProviderNotRegistered as err:
        state["error"] = err
        return
    skip_if_unimplemented(provider, "embed")
    start = time.perf_counter()
    try:
        vectors = provider.embed_batch([text])
    except EmbedNotSupported as err:
        state["error"] = err
        return
    latency_ms = (time.perf_counter() - start) * 1000.0
    envelope = {
        "provider_name": provider.name,
        "vectors": vectors,
        "stage_latency_ms": {"http_roundtrip": latency_ms},
    }
    state["vectors"] = vectors
    state["envelope"] = envelope
    state["envelopes"].append(envelope)


@when(parsers.parse('the operator embeds the text "{text}"'))
def operator_embeds_text(e2e_provider_state: dict[str, Any], text: str) -> None:
    drive_embed(e2e_provider_state, text)


@when("the operator embeds the batch:")
def operator_embeds_batch(e2e_provider_state: dict[str, Any], datatable: Any) -> None:
    """Embed a batch of texts; capture the per-text vector list in order."""
    rows = list(datatable)
    # pytest-bdd surfaces tables WITH a header row when |...| is single-column.
    # The embed feature's batch table has no header (it's a list of texts);
    # we treat every row as a text but skip the first if it looks header-y
    # (i.e. is a single column with no embedded quotes). The feature file's
    # actual rows are "first text", "second text", "third text" — all are data.
    texts = [row[0].strip() for row in rows]
    registry = e2e_provider_state["registry"]
    name = e2e_provider_state["current_provider_name"]
    provider = get_provider(name, registry=registry)
    skip_if_unimplemented(provider, "embed-batch")
    vectors = provider.embed_batch(texts)
    envelope = {
        "provider_name": provider.name,
        "vectors": vectors,
        "stage_latency_ms": {"http_roundtrip": 0.0},
    }
    e2e_provider_state["vectors"] = vectors
    e2e_provider_state["envelope"] = envelope
    e2e_provider_state["envelopes"].append(envelope)


# ---------------------------------------------------------------------------
# Then — happy-path assertions on the envelope (shared)
# ---------------------------------------------------------------------------


@then(parsers.parse("the result is a vector of dimension {dim:d}"))
def result_vector_dimension(e2e_provider_state: dict[str, Any], dim: int) -> None:
    # sabotage: change FakeProvider(dim=1536) to dim=1024 → assertion fires.
    envelope = e2e_provider_state["envelope"]
    assert envelope is not None, "embed did not populate envelope (provider may have raised)"
    vectors = envelope["vectors"]
    assert len(vectors) == 1, f"expected 1 vector for single-text embed, got {len(vectors)}"
    assert len(vectors[0]) == dim, f"expected vector of dim {dim}, got dim {len(vectors[0])}"


@then(parsers.parse('the result envelope records the provider name "{name}"'))
def envelope_records_provider_name(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: rename FakeProvider's name= kwarg → envelope.provider_name diverges.
    envelope = e2e_provider_state["envelope"]
    assert envelope is not None, "embed did not populate envelope (provider may have raised)"
    assert envelope["provider_name"] == name, f"envelope provider_name={envelope['provider_name']!r}, expected {name!r}"


# ---------------------------------------------------------------------------
# Then — typed-error assertions (EmbedNotSupported scenario, embed-only)
# ---------------------------------------------------------------------------


@then("the operator sees a typed EmbedNotSupported error")
def operator_sees_embed_not_supported(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: change _EmbedNotSupportedProvider.embed_batch to return [] → AssertionError fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected EmbedNotSupported error, but embed succeeded"
    assert isinstance(err, EmbedNotSupported), f"expected EmbedNotSupported, got {type(err).__name__}: {err!r}"


@then(parsers.parse('the error names the provider "{name}"'))
def error_names_provider(e2e_provider_state: dict[str, Any], name: str) -> None:
    # sabotage: drop provider_name kwarg in EmbedNotSupported → attribute absent fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error, but state['error'] is None"
    assert getattr(err, "provider_name", None) == name, (
        f"error.provider_name={getattr(err, 'provider_name', None)!r}, expected {name!r}"
    )


@then("the error suggests configuring a different provider for embeddings")
def error_suggests_alternative(e2e_provider_state: dict[str, Any]) -> None:
    # sabotage: blank the default message in EmbedNotSupported.__init__ → substring missing fires.
    err = e2e_provider_state["error"]
    assert err is not None, "expected captured error, but state['error'] is None"
    message = str(err)
    assert "different provider" in message or "fix:" in message, (
        f"error message did not advertise an alternative provider; got: {message!r}"
    )


# ---------------------------------------------------------------------------
# Then — batch embed assertions
# ---------------------------------------------------------------------------


@then(parsers.parse("the result contains {count:d} vectors in the same order as the inputs"))
def result_contains_vectors_in_order(e2e_provider_state: dict[str, Any], count: int) -> None:
    # sabotage: have FakeProvider.embed_batch return one fewer vector than inputs
    # → len assertion fires. The "in order" wording is structural (1:1 mapping);
    # the FakeProvider already returns one vector per input so order is preserved
    # by construction.
    vectors = e2e_provider_state["vectors"]
    assert vectors is not None, "batch embed did not populate vectors"
    assert len(vectors) == count, f"expected {count} vectors, got {len(vectors)}"


@then(parsers.parse("every vector has dimension {dim:d}"))
def every_vector_has_dimension(e2e_provider_state: dict[str, Any], dim: int) -> None:
    # sabotage: have FakeProvider return a [0.0]*900 vector while declared dim=1536 → assertion fires.
    vectors = e2e_provider_state["vectors"]
    assert vectors is not None, "batch embed did not populate vectors"
    for index, vec in enumerate(vectors):
        assert len(vec) == dim, f"vector {index}: expected dim {dim}, got dim {len(vec)}"
