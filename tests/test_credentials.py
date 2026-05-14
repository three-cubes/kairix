"""Tests for kairix.credentials — resolved Credentials/GraphCredentials.

Uses real env-var injection via monkeypatch (this file is baselined for F2
because kairix.credentials wraps the secret env-vars; injecting them is the
public interface). No @patch on kairix internals.
"""

from __future__ import annotations

import pytest

from kairix.credentials import (
    AZURE_API_VERSION,
    Credentials,
    GraphCredentials,
    get_credentials,
    make_openai_client,
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_credentials_is_azure_detection() -> None:
    """is_azure is True for Azure endpoints and false otherwise."""
    azure = Credentials(api_key="k", endpoint="https://x.openai.azure.com", model="m")
    cogn = Credentials(api_key="k", endpoint="https://x.cognitiveservices.azure.com", model="m")
    openai = Credentials(api_key="k", endpoint="https://api.openai.com/v1", model="m")
    assert azure.is_azure is True
    assert cogn.is_azure is True
    assert openai.is_azure is False


@pytest.mark.unit
def test_graph_credentials_dataclass() -> None:
    gc = GraphCredentials(uri="bolt://x:7687", user="neo4j", password="pw")  # pragma: allowlist secret
    assert gc.uri == "bolt://x:7687"
    assert gc.user == "neo4j"
    assert gc.password == "pw"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# get_credentials dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_credentials_unknown_purpose_raises() -> None:
    """Unknown purpose raises ValueError with hint at valid options."""
    with pytest.raises(ValueError, match="Unknown credential purpose"):
        get_credentials("nonsense")


# ---------------------------------------------------------------------------
# _resolve_llm — happy path + model fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_llm_with_explicit_model(monkeypatch, tmp_path) -> None:
    """When all three env vars set, returns a Credentials with that model."""
    monkeypatch.setenv("KAIRIX_LLM_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("KAIRIX_LLM_ENDPOINT", "https://api.openai.com/v1")
    monkeypatch.setenv("KAIRIX_LLM_MODEL", "gpt-4o")
    # Point secrets dir at non-existent path so file/KV steps short-circuit
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)

    creds = get_credentials("llm")
    assert isinstance(creds, Credentials)
    assert creds.api_key == "test-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://api.openai.com/v1"
    assert creds.model == "gpt-4o"


@pytest.mark.unit
def test_resolve_llm_uses_default_model(monkeypatch, tmp_path) -> None:
    """When KAIRIX_LLM_MODEL is unset, falls back to 'gpt-4o-mini'."""
    monkeypatch.setenv("KAIRIX_LLM_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("KAIRIX_LLM_ENDPOINT", "https://api.openai.com/v1")
    monkeypatch.delenv("KAIRIX_LLM_MODEL", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)

    creds = get_credentials("llm")
    assert isinstance(creds, Credentials)
    assert creds.model == "gpt-4o-mini"


@pytest.mark.unit
def test_resolve_llm_raises_when_missing(monkeypatch, tmp_path) -> None:
    """When required secret missing, raises OSError."""
    monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_LLM_ENDPOINT", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    with pytest.raises(OSError):
        get_credentials("llm")


# ---------------------------------------------------------------------------
# _resolve_embed — explicit overrides + fallback to LLM creds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_embed_uses_embed_specific_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KAIRIX_EMBED_API_KEY", "embed-key")
    monkeypatch.setenv("KAIRIX_EMBED_ENDPOINT", "https://embed.example.com")
    monkeypatch.setenv("KAIRIX_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_LLM_ENDPOINT", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)

    creds = get_credentials("embed")
    assert isinstance(creds, Credentials)
    assert creds.api_key == "embed-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://embed.example.com"
    assert creds.model == "text-embedding-3-small"
    assert creds.dims > 0


@pytest.mark.unit
def test_resolve_embed_falls_back_to_llm_secrets(monkeypatch, tmp_path) -> None:
    """When embed-specific creds are missing, falls back to LLM creds."""
    monkeypatch.delenv("KAIRIX_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_EMBED_ENDPOINT", raising=False)
    monkeypatch.delenv("KAIRIX_EMBED_MODEL", raising=False)
    monkeypatch.setenv("KAIRIX_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("KAIRIX_LLM_ENDPOINT", "https://api.openai.com/v1")
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)

    creds = get_credentials("embed")
    assert isinstance(creds, Credentials)
    assert creds.api_key == "llm-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://api.openai.com/v1"
    # Default embed model
    assert creds.model == "text-embedding-3-large"


# ---------------------------------------------------------------------------
# _resolve_graph — None when password absent, GraphCredentials otherwise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_graph_returns_none_without_password(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KAIRIX_NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    assert get_credentials("graph") is None


@pytest.mark.unit
def test_resolve_graph_returns_credentials_with_password(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KAIRIX_NEO4J_PASSWORD", "secret-pw")
    monkeypatch.setenv("KAIRIX_NEO4J_URI", "bolt://neo4j.test:7687")
    monkeypatch.setenv("KAIRIX_NEO4J_USER", "alice")
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    creds = get_credentials("graph")
    assert isinstance(creds, GraphCredentials)
    assert creds.uri == "bolt://neo4j.test:7687"
    assert creds.user == "alice"
    assert creds.password == "secret-pw"  # pragma: allowlist secret


@pytest.mark.unit
def test_resolve_graph_uses_default_uri_when_unset(monkeypatch, tmp_path) -> None:
    """KAIRIX_NEO4J_URI defaults to bolt://localhost:7687 when unset."""
    monkeypatch.setenv("KAIRIX_NEO4J_PASSWORD", "pw")
    monkeypatch.delenv("KAIRIX_NEO4J_URI", raising=False)
    monkeypatch.delenv("KAIRIX_NEO4J_USER", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path / "no-such-dir"))
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    creds = get_credentials("graph")
    assert isinstance(creds, GraphCredentials)
    assert creds.uri == "bolt://localhost:7687"
    assert creds.user == "neo4j"


# ---------------------------------------------------------------------------
# make_openai_client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_openai_client_returns_azure_client_for_azure_endpoint() -> None:
    """Endpoint containing 'azure' triggers the AzureOpenAI factory."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.openai.azure.com",
    )
    # AzureOpenAI sets api_version internally
    assert client is not None
    cls_name = type(client).__name__
    assert "Azure" in cls_name


@pytest.mark.unit
def test_make_openai_client_returns_openai_client_for_openai_endpoint() -> None:
    """Endpoint without 'azure' triggers the OpenAI factory."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
    )
    assert client is not None
    cls_name = type(client).__name__
    assert cls_name == "OpenAI"


@pytest.mark.unit
def test_make_openai_client_detects_cognitiveservices_as_azure() -> None:
    """cognitiveservices endpoint is treated as Azure."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.cognitiveservices.azure.com",
    )
    assert "Azure" in type(client).__name__


@pytest.mark.unit
def test_azure_api_version_constant() -> None:
    """AZURE_API_VERSION is non-empty (read at module import via env or default)."""
    assert AZURE_API_VERSION
    assert isinstance(AZURE_API_VERSION, str)
