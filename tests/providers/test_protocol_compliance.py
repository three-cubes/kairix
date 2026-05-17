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
    """Each first-party stub's make_provider raises NotImplementedError.

    Wave 1 verifies only that the entry-point factory is importable
    and that calling it surfaces the expected NotImplementedError —
    Wave 2+ swaps the body for the real implementation. We don't run
    ``isinstance`` against the factory's *return value* yet (there
    isn't one), but the factory's existence under the import path is
    what the entry-point spec keys on.
    """

    @pytest.mark.parametrize(
        "import_path",
        [
            "kairix.providers.azure_foundry",
            "kairix.providers.azure_legacy",
            "kairix.providers.openai",
            "kairix.providers.bedrock",
            "kairix.providers.ollama",
            "kairix.providers.litellm_proxy",
            "kairix.providers.anthropic",
        ],
    )
    @pytest.mark.unit
    def test_stub_make_provider_raises_not_implemented(self, import_path: str) -> None:
        import importlib

        module = importlib.import_module(import_path)
        assert hasattr(module, "make_provider"), (
            f"{import_path} missing make_provider — entry-point factory "
            f"target must exist even before the Wave-2 implementation lands."
        )
        with pytest.raises(NotImplementedError):
            module.make_provider()


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
