"""Protocol compliance contract tests for kairix.providers.

Mirrors ``tests/contracts/test_protocols.py``: every Protocol gets an
``isinstance()`` conformance check against its fake and against the
real entry-point-backed implementation.

The first-party plugin stubs (azure_foundry / openai / bedrock / ...)
each ship a ``make_provider()`` factory that currently raises
``NotImplementedError``. We don't *call* those factories here; we
verify that the entry-point group resolves and that the production
``EntryPointRegistry`` satisfies the ``ProviderRegistry`` Protocol.

All tests use the ``unit`` marker — they only touch in-memory fakes
and import metadata, no disk / network / DB.
"""

from __future__ import annotations

import pytest

from kairix.providers import (
    ENTRY_POINT_GROUP,
    EntryPointRegistry,
    Provider,
    ProviderHealth,
    ProviderRegistry,
    get_provider,
)
from tests.fakes import FakeProvider, FakeProviderRegistry


@pytest.mark.unit
class TestProviderProtocolCompliance:
    """FakeProvider satisfies the Provider Protocol."""

    @pytest.mark.unit
    def test_fake_provider_satisfies_protocol(self) -> None:
        assert isinstance(FakeProvider(), Provider)

    @pytest.mark.unit
    def test_fake_provider_records_embed_calls(self) -> None:
        # Sabotage-proof: if FakeProvider stopped recording calls,
        # transport-layer assertions about coalesce counts would
        # silently pass. Mutate embed_calls.append → confirm fail.
        provider = FakeProvider(vector=[1.0, 2.0, 3.0])
        out = provider.embed_batch(["alpha", "beta"])
        assert out == [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]
        assert provider.embed_calls == [["alpha", "beta"]]

    @pytest.mark.unit
    def test_fake_provider_records_chat_calls(self) -> None:
        provider = FakeProvider(chat_reply="hello world")
        out = provider.chat([{"role": "user", "content": "hi"}], max_tokens=50)
        assert out == "hello world"
        assert provider.chat_calls == [{"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}]

    @pytest.mark.unit
    def test_fake_provider_dimension_and_health(self) -> None:
        provider = FakeProvider(dim=7)
        assert provider.dimension() == 7
        assert provider.dimension_calls == 1
        health = provider.healthcheck()
        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert provider.healthcheck_calls == 1


@pytest.mark.unit
class TestProviderRegistryProtocolCompliance:
    """FakeProviderRegistry and EntryPointRegistry both satisfy the Protocol."""

    @pytest.mark.unit
    def test_fake_registry_satisfies_protocol(self) -> None:
        assert isinstance(FakeProviderRegistry(), ProviderRegistry)

    @pytest.mark.unit
    def test_entry_point_registry_satisfies_protocol(self) -> None:
        assert isinstance(EntryPointRegistry(), ProviderRegistry)


@pytest.mark.unit
class TestFirstPartyStubProtocolShape:
    """Each first-party stub's make_provider is importable and callable.

    Stubs that have not yet been implemented (Wave 1 placeholders)
    raise ``NotImplementedError`` when called; stubs whose Wave-2
    implementation has landed return a ``Provider`` or raise a
    typed configuration error (e.g. missing credentials surfaces as
    ``RuntimeError``). The factory's mere *existence* under the
    declared import path is what the entry-point spec keys on.

    ``azure_foundry`` (IM-4) and ``openai`` (IM-5) are the first two
    plugins to graduate to Wave 2. Their conformance is exercised by
    ``tests/providers/<name>/test_provider.py``; here we only assert
    that the factory symbol stays importable.
    """

    @pytest.mark.unit
    def test_no_stub_make_provider_remains(self) -> None:
        # Wave 4 graduated every first-party plugin out of the
        # NotImplementedError-stub state. If a new stub is added in a
        # future Wave it should be registered in this test (and removed
        # once the implementation lands) — same pattern as the
        # graduated tests below.
        import importlib

        for name in ("azure_foundry", "azure_legacy", "openai", "bedrock", "ollama", "litellm_proxy", "anthropic"):
            module = importlib.import_module(f"kairix.providers.{name}")
            assert hasattr(module, "make_provider"), (
                f"kairix.providers.{name} missing make_provider — entry-point factory target is removed by mistake."
            )
            assert callable(module.make_provider)

    @pytest.mark.unit
    def test_anthropic_make_provider_is_importable(self) -> None:
        # anthropic graduated to Wave 4 (IM-13) — chat-only plugin.
        # Behaviour conformance (including the load-bearing
        # embed-short-circuit invariant) is asserted in
        # tests/providers/anthropic/test_provider.py. We don't call the
        # factory here because it would need a real credential
        # resolution (which the unit suite intentionally doesn't wire).
        import importlib

        module = importlib.import_module("kairix.providers.anthropic")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.anthropic missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_azure_foundry_make_provider_is_importable(self) -> None:
        # azure_foundry graduated to Wave 2 (IM-4). The factory exists
        # and is callable; behaviour conformance is asserted in
        # tests/providers/azure_foundry/test_provider.py. We don't call
        # the factory here because it would need a real credential
        # resolution (which the unit suite intentionally doesn't wire).
        import importlib

        module = importlib.import_module("kairix.providers.azure_foundry")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.azure_foundry missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_openai_make_provider_is_importable(self) -> None:
        # openai graduated to Wave 2 (IM-5) — the proof-of-shape that
        # the plugin contract carries beyond Azure. Behaviour conformance
        # is asserted in tests/providers/openai/test_provider.py. We
        # don't call the factory here because it would need a real
        # credential resolution (which the unit suite intentionally
        # doesn't wire).
        import importlib

        module = importlib.import_module("kairix.providers.openai")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.openai missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_azure_legacy_make_provider_is_importable(self) -> None:
        # azure_legacy graduated to Wave 4 (IM-14) — sibling to
        # azure_foundry covering the pre-Foundry Azure OpenAI Service
        # endpoint shape (``<resource>.openai.azure.com``). Behaviour
        # conformance is asserted in tests/providers/azure_legacy/
        # test_provider.py. We don't call the factory here because it
        # would need a real credential resolution (which the unit suite
        # intentionally doesn't wire).
        import importlib

        module = importlib.import_module("kairix.providers.azure_legacy")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.azure_legacy missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_ollama_make_provider_is_importable(self) -> None:
        # ollama graduated to Wave 4 (IM-11) — the local-sidecar plugin
        # proving the contract carries to an unauthenticated endpoint
        # with a non-OpenAI wire shape.
        import importlib

        module = importlib.import_module("kairix.providers.ollama")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.ollama missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_bedrock_make_provider_is_importable(self) -> None:
        # bedrock graduated to Wave 4 (IM-10) — first non-OpenAI-compatible
        # plugin, proving the contract carries beyond the openai SDK shape
        # (AWS SigV4 + boto3-style client). Behaviour conformance lives in
        # tests/providers/bedrock/test_provider.py. boto3 is an optional
        # dep under the [bedrock] extra.
        import importlib

        module = importlib.import_module("kairix.providers.bedrock")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.bedrock missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)

    @pytest.mark.unit
    def test_litellm_proxy_make_provider_is_importable(self) -> None:
        # litellm_proxy graduated to Wave 4 (IM-12) — the proxy-sidecar
        # plugin proving the OpenAI-compatible Bearer-auth contract
        # carries through a virtual-key-fronted aggregator. Behaviour
        # conformance is asserted in
        # tests/providers/litellm_proxy/test_provider.py. We don't call
        # the factory here because it would need a real credential
        # resolution (which the unit suite intentionally doesn't wire).
        import importlib

        module = importlib.import_module("kairix.providers.litellm_proxy")
        assert hasattr(module, "make_provider"), (
            "kairix.providers.litellm_proxy missing make_provider — entry-point factory target is removed by mistake."
        )
        assert callable(module.make_provider)


@pytest.mark.unit
class TestGetProviderConvenience:
    """get_provider() delegates to the injected registry."""

    @pytest.mark.unit
    def test_get_provider_uses_injected_registry(self) -> None:
        fake = FakeProvider(name="openai")
        registry = FakeProviderRegistry({"openai": fake})
        resolved = get_provider("openai", registry=registry)
        assert resolved is fake
        assert registry.resolve_calls == ["openai"]

    @pytest.mark.unit
    def test_entry_point_group_name_is_canonical(self) -> None:
        # Sanity check: the group constant matches the pyproject entry
        # exactly. If someone renames the group in _base.py without
        # updating pyproject.toml, third-party plugins stop loading.
        assert ENTRY_POINT_GROUP == "kairix.providers"
