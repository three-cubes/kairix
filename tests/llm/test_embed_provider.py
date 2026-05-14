"""Tests for kairix.platform.llm.embed_provider — SDK-based embedding clients."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Create a mock openai module so tests work without the real package installed
_mock_openai = ModuleType("openai")
_mock_openai.AzureOpenAI = MagicMock  # type: ignore[attr-defined]  # injecting attr onto synthetic stand-in module
_mock_openai.OpenAI = MagicMock  # type: ignore[attr-defined]  # injecting attr onto synthetic stand-in module


@pytest.fixture(autouse=True)
def _mock_openai_module(monkeypatch):
    """Ensure openai module is available for import even if not installed."""
    monkeypatch.setitem(sys.modules, "openai", _mock_openai)
    yield
    # Re-import to clear cached provider instances
    if "kairix.platform.llm.embed_provider" in sys.modules:
        del sys.modules["kairix.platform.llm.embed_provider"]


from kairix.platform.llm.embed_provider import (  # noqa: E402  # import deferred until after openai mock is installed
    AzureEmbedProvider,
    EmbedProvider,
    OpenAIEmbedProvider,
    get_embed_provider,
)


@pytest.mark.unit
class TestEmbedProviderProtocol:
    @pytest.mark.unit
    def test_azure_provider_is_embed_provider(self) -> None:
        provider = AzureEmbedProvider(endpoint="https://test.openai.azure.com", api_key="key")
        assert isinstance(provider, EmbedProvider)

    @pytest.mark.unit
    def test_openai_provider_is_embed_provider(self) -> None:
        provider = OpenAIEmbedProvider(api_key="key")
        assert isinstance(provider, EmbedProvider)


@pytest.mark.unit
class TestAzureEmbedProvider:
    @pytest.mark.unit
    def test_embed_batch_delegates_to_sdk(self) -> None:
        provider = AzureEmbedProvider(endpoint="https://test.openai.azure.com", api_key="key")

        mock_item = MagicMock()
        mock_item.embedding = [0.1, 0.2, 0.3]
        provider.client.embeddings.create.return_value = MagicMock(data=[mock_item])

        result = provider.embed_batch(["hello"], model="text-embedding-3-large", dims=1536)
        assert result == [[0.1, 0.2, 0.3]]

    @pytest.mark.unit
    def test_batch_returns_correct_count(self) -> None:
        provider = AzureEmbedProvider(endpoint="https://test", api_key="key")

        items = [MagicMock(embedding=[float(i)]) for i in range(3)]
        provider.client.embeddings.create.return_value = MagicMock(data=items)

        result = provider.embed_batch(["a", "b", "c"], model="m", dims=3)
        assert len(result) == 3


@pytest.mark.unit
class TestOpenAIEmbedProvider:
    @pytest.mark.unit
    def test_embed_batch_delegates_to_sdk(self) -> None:
        provider = OpenAIEmbedProvider(api_key="key")

        items = [MagicMock(embedding=[1.0, 2.0]), MagicMock(embedding=[3.0, 4.0])]
        provider.client.embeddings.create.return_value = MagicMock(data=items)

        result = provider.embed_batch(["a", "b"], model="text-embedding-3-small", dims=512)
        assert len(result) == 2
        assert result[0] == [1.0, 2.0]


@pytest.mark.unit
class TestGetEmbedProvider:
    @pytest.mark.unit
    def test_returns_azure_when_azure_creds_supplied(self) -> None:
        from kairix.credentials import Credentials

        creds = Credentials(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://test.openai.azure.com",
            model="text-embedding-3-large",
        )
        provider = get_embed_provider(creds_resolver=lambda: creds)
        assert isinstance(provider, AzureEmbedProvider)

    @pytest.mark.unit
    def test_falls_back_to_openai_via_env(self) -> None:
        """When the creds_resolver returns None, the OPENAI_API_KEY env entry wins."""
        provider = get_embed_provider(
            creds_resolver=lambda: None,
            env={"OPENAI_API_KEY": "sk-test"},  # pragma: allowlist secret
        )
        assert isinstance(provider, OpenAIEmbedProvider)

    @pytest.mark.unit
    def test_raises_when_no_credentials(self) -> None:
        with pytest.raises(OSError, match="No embedding provider"):
            get_embed_provider(creds_resolver=lambda: None, env={})
