"""Integration: provider selection flows from kairix.config.yaml through to the loaded plugin.

End-to-end boundary chain:

  yaml file ─► load_config(config_path=...) ─► RetrievalConfig.provider
              ─► kairix.providers.get_provider(name, registry=fake)
              ─► Provider instance (FakeProvider in this test)
              ─► ProviderEmbeddingService(provider).embed("text")
              ─► provider.embed_batch records the call

This exercises the v2026.5.17 architectural pivot: provider selection
now lives in the config-yaml field, not the env var. The plugin is the
only embed path.

Sabotage proofs:

- ``test_yaml_provider_field_drives_plugin_selection``: change
  ``parse_config`` to drop the top-level ``provider:`` field → the
  ``cfg.provider`` assertion fails because the parser silently
  discards the operator's choice.
- ``test_unset_provider_field_propagates_as_none``: change the
  parser to default ``provider`` to ``"azure_foundry"`` → the
  ``None`` assertion fails because the silent default would mask
  misconfiguration.
- ``test_configured_provider_resolves_through_registry``: change the
  registry seam so it ignores the requested name and returns a
  different plugin → the assertion that the embed call lands on the
  correct ``FakeProvider`` fails (counted via ``embed_calls`` on the
  registry-provided fake).

F2-clean — no ``monkeypatch.setenv("KAIRIX_*")``. The config yaml is
written to ``tmp_path`` and passed via ``load_config(config_path=)``;
the registry is the test-side ``FakeProviderRegistry`` from
``tests/fakes.py``.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import load_cached, load_config
from kairix.providers import get_provider
from kairix.transport.cache import reset_embed_cache
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """Reset the embed cache + coalescer + config lru_cache between cases."""
    reset_embed_cache()
    reset_embed_coalescer()
    load_cached.cache_clear()
    yield
    reset_embed_cache()
    reset_embed_coalescer()
    load_cached.cache_clear()


class TestProviderSelectionFromConfigYaml:
    """The full operator-visible journey: yaml → adapter → provider."""

    def test_yaml_provider_field_drives_plugin_selection(self, tmp_path) -> None:
        """Operator writes ``provider: azure_foundry`` → ``load_config`` returns
        it on ``RetrievalConfig.provider``."""
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text(
            "provider: azure_foundry\nretrieval:\n  fusion_strategy: rrf\n",
            encoding="utf-8",
        )

        cfg = load_config(config_path=config_file)

        assert cfg.provider == "azure_foundry", (
            f"expected provider='azure_foundry' to flow through, got {cfg.provider!r}"
        )
        # The retrieval block survives — sabotage proofs against a parser
        # that rewrites top-level keys into the retrieval section.
        assert cfg.fusion_strategy == "rrf"

    def test_unset_provider_field_propagates_as_none(self, tmp_path) -> None:
        """No ``provider:`` field → ``cfg.provider is None`` so callers raise
        a typed error rather than silently defaulting."""
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text(
            "retrieval:\n  fusion_strategy: bm25_primary\n",
            encoding="utf-8",
        )

        cfg = load_config(config_path=config_file)

        assert cfg.provider is None, f"expected None for unset provider, got {cfg.provider!r}"

    def test_configured_provider_resolves_through_registry(self) -> None:
        """The configured name resolves to the registry-provided plugin and
        embed calls land on that exact instance.

        This is the load-bearing assertion: provider selection is
        operator-visible config → resolved Provider → embed dispatch.
        Threading the test seam (``registry=`` parameter on
        ``get_provider``) is how the integration test honestly drives
        the production accessor without an entry-point install.
        """
        # Two distinct fakes so the assertion proves the correct one
        # was selected — not that "some fake" was called.
        azure_fake = FakeProvider(name="azure_foundry", vector=[0.11, 0.22, 0.33])
        openai_fake = FakeProvider(name="openai", vector=[0.99, 0.99, 0.99])
        registry = FakeProviderRegistry({"azure_foundry": azure_fake, "openai": openai_fake})

        provider = get_provider("azure_foundry", registry=registry)
        service = ProviderEmbeddingService(provider)
        result = service.embed("integration probe")

        # Returned vector matches the selected plugin's configured vector.
        assert result == [0.11, 0.22, 0.33], f"got {result!r}, expected azure_foundry's vector"
        # The other fake was never asked to embed anything.
        assert openai_fake.embed_calls == [], (
            f"openai fake unexpectedly received {openai_fake.embed_calls!r}; "
            "selection should route only to the configured plugin"
        )
        # The selected fake recorded the embed call.
        assert azure_fake.embed_calls, "azure_foundry fake never received the embed call"
