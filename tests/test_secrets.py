"""
Tests for kairix.secrets — sidecar secrets file loader and get_secret resolver.

All tests use tmp_path and monkeypatch to isolate env and filesystem state.
No external services required.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kairix.secrets import get_secret, load_secrets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_secrets(tmp_path, content: str) -> str:
    """Write a secrets file and return its path as a string."""
    p = tmp_path / "kairix.env"
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# load_secrets: File absent / empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_zero_when_file_absent(tmp_path) -> None:
    count = load_secrets(str(tmp_path / "nonexistent.env"))
    assert count == 0


@pytest.mark.unit
def test_returns_zero_for_empty_file(tmp_path) -> None:
    path = _write_secrets(tmp_path, "")
    assert load_secrets(path) == 0


@pytest.mark.unit
def test_returns_zero_for_comments_only(tmp_path) -> None:
    path = _write_secrets(tmp_path, "# This is a comment\n# Another comment\n")
    assert load_secrets(path) == 0


# ---------------------------------------------------------------------------
# load_secrets: Loading values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_loads_single_key_value(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_VAR_ALPHA", raising=False)
    path = _write_secrets(tmp_path, "TEST_VAR_ALPHA=hello\n")
    count = load_secrets(path)
    assert count == 1
    assert os.environ["TEST_VAR_ALPHA"] == "hello"
    monkeypatch.delenv("TEST_VAR_ALPHA")


@pytest.mark.unit
def test_loads_multiple_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SECRET_A", raising=False)
    monkeypatch.delenv("SECRET_B", raising=False)
    path = _write_secrets(tmp_path, "SECRET_A=val1\nSECRET_B=val2\n")
    count = load_secrets(path)
    assert count == 2
    assert os.environ["SECRET_A"] == "val1"
    assert os.environ["SECRET_B"] == "val2"
    monkeypatch.delenv("SECRET_A")
    monkeypatch.delenv("SECRET_B")


@pytest.mark.unit
def test_value_with_equals_sign(tmp_path, monkeypatch) -> None:
    """Values containing '=' are supported (partition splits on first '=' only)."""
    monkeypatch.delenv("URL_VAR", raising=False)
    path = _write_secrets(tmp_path, "URL_VAR=https://example.com/path?foo=bar\n")
    load_secrets(path)
    assert os.environ["URL_VAR"] == "https://example.com/path?foo=bar"
    monkeypatch.delenv("URL_VAR")


@pytest.mark.unit
def test_ignores_blank_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ONLY_VAR", raising=False)
    path = _write_secrets(tmp_path, "\n\nONLY_VAR=yes\n\n")
    count = load_secrets(path)
    assert count == 1
    monkeypatch.delenv("ONLY_VAR")


@pytest.mark.unit
def test_ignores_comment_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REAL_VAR", raising=False)
    content = "# comment\nREAL_VAR=real\n# another comment\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path)
    assert count == 1
    assert os.environ["REAL_VAR"] == "real"
    monkeypatch.delenv("REAL_VAR")


@pytest.mark.unit
def test_ignores_lines_without_equals(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GOOD_VAR", raising=False)
    content = "BADLINE\nGOOD_VAR=ok\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path)
    assert count == 1
    monkeypatch.delenv("GOOD_VAR")


# ---------------------------------------------------------------------------
# load_secrets: Priority — existing env vars are NOT overwritten
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_does_not_overwrite_existing_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROTECTED_VAR", "original")
    path = _write_secrets(tmp_path, "PROTECTED_VAR=override\n")
    count = load_secrets(path)
    assert count == 0  # not loaded — already set
    assert os.environ["PROTECTED_VAR"] == "original"


@pytest.mark.unit
def test_partial_load_when_some_already_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALREADY_SET", "existing")
    monkeypatch.delenv("NOT_SET_YET", raising=False)
    content = "ALREADY_SET=new\nNOT_SET_YET=fresh\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path)
    assert count == 1
    assert os.environ["ALREADY_SET"] == "existing"
    assert os.environ["NOT_SET_YET"] == "fresh"
    monkeypatch.delenv("NOT_SET_YET")


# ---------------------------------------------------------------------------
# load_secrets: KAIRIX_SECRETS_FILE env var controls default path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_uses_kairix_secrets_file_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ENV_FROM_FILE", raising=False)
    path = _write_secrets(tmp_path, "ENV_FROM_FILE=loaded\n")
    monkeypatch.setenv("KAIRIX_SECRETS_FILE", path)
    count = load_secrets()  # no explicit path — reads from env var
    assert count == 1
    assert os.environ["ENV_FROM_FILE"] == "loaded"
    monkeypatch.delenv("ENV_FROM_FILE")


# ---------------------------------------------------------------------------
# load_secrets: Error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_zero_on_permission_error(tmp_path, monkeypatch) -> None:
    """load_secrets should not raise even if the file can't be read."""
    path = _write_secrets(tmp_path, "X=1\n")
    import os as _os

    _os.chmod(path, 0o000)
    try:
        count = load_secrets(path)
        assert count == 0
    finally:
        _os.chmod(path, 0o644)


@pytest.mark.unit
def test_idempotent_multiple_calls(tmp_path, monkeypatch) -> None:
    """Calling load_secrets twice is safe — second call adds nothing."""
    monkeypatch.delenv("IDEMPOTENT_VAR", raising=False)
    path = _write_secrets(tmp_path, "IDEMPOTENT_VAR=once\n")
    count1 = load_secrets(path)
    count2 = load_secrets(path)
    assert count1 == 1
    assert count2 == 0  # already set after first call
    assert os.environ["IDEMPOTENT_VAR"] == "once"
    monkeypatch.delenv("IDEMPOTENT_VAR")


# ---------------------------------------------------------------------------
# get_secret: Step 1 — direct env var resolution (highest priority)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_from_env_var(monkeypatch) -> None:
    """get_secret returns value when the mapped env var is set."""
    monkeypatch.setenv("KAIRIX_LLM_API_KEY", "test-key-from-env")
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    # Point secrets dir at a nonexistent path so file step is skipped
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", "/nonexistent-dir-abc123")
    value = get_secret("kairix-llm-api-key")
    assert value == "test-key-from-env"


@pytest.mark.unit
def test_get_secret_env_var_takes_priority_over_file(tmp_path, monkeypatch) -> None:
    """Env var wins over sidecar file — highest priority."""
    monkeypatch.setenv("KAIRIX_LLM_API_KEY", "env-wins")
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    p = tmp_path / "kairix.env"
    p.write_text("KAIRIX_LLM_API_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path))
    value = get_secret("kairix-llm-api-key")
    assert value == "env-wins"


# ---------------------------------------------------------------------------
# get_secret: Step 2 — file-based resolution (sidecar secrets file)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_from_file(tmp_path, monkeypatch) -> None:
    """get_secret reads from the sidecar secrets file when env var is absent."""
    monkeypatch.delenv("KAIRIX_LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    p = tmp_path / "kairix.env"
    p.write_text("KAIRIX_LLM_ENDPOINT=https://example.openai.azure.com\n", encoding="utf-8")
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path))
    # Clear lru_cache so this path is freshly read
    from kairix.secrets import load_secrets_file

    load_secrets_file.cache_clear()
    value = get_secret("kairix-llm-endpoint")
    assert value == "https://example.openai.azure.com"


@pytest.mark.unit
def test_get_secret_file_ignores_comments_and_blank_lines(tmp_path, monkeypatch) -> None:
    """File parser skips # comments and blank lines."""
    monkeypatch.delenv("KAIRIX_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    content = "# generated by vault-agent\n\nKAIRIX_NEO4J_PASSWORD=s3cr3t\n# end\n"  # pragma: allowlist secret
    p = tmp_path / "kairix.env"
    p.write_text(content, encoding="utf-8")
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", str(tmp_path))
    from kairix.secrets import load_secrets_file

    load_secrets_file.cache_clear()
    value = get_secret("kairix-neo4j-password")
    assert value == "s3cr3t"


# ---------------------------------------------------------------------------
# get_secret: Missing secret error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_required_raises_oserror(monkeypatch) -> None:
    """Missing required secret raises OSError with an informative message."""
    monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", "/nonexistent-dir-abc123")
    with pytest.raises(OSError, match="not available"):
        get_secret("kairix-llm-api-key")


@pytest.mark.unit
def test_get_secret_required_true_is_default(monkeypatch) -> None:
    """required=True is the default — omitting it raises on missing secret."""
    monkeypatch.delenv("KAIRIX_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", "/nonexistent-dir-abc123")
    with pytest.raises(OSError):
        get_secret("kairix-neo4j-password")


@pytest.mark.unit
def test_get_secret_not_required_returns_none(monkeypatch) -> None:
    """required=False returns None instead of raising when secret is absent."""
    monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", "/nonexistent-dir-abc123")
    result = get_secret("kairix-llm-api-key", required=False)
    assert result is None


@pytest.mark.unit
def test_get_secret_oserror_message_is_informative(monkeypatch) -> None:
    """OSError message names the secret and hints at resolution steps."""
    monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KAIRIX_KV_NAME", raising=False)
    monkeypatch.setenv("KAIRIX_SECRETS_DIR", "/nonexistent-dir-abc123")
    with pytest.raises(OSError) as exc_info:
        get_secret("kairix-llm-api-key")
    msg = str(exc_info.value)
    # Error message must NOT contain the secret name (security: no key names in output)
    assert "kairix-llm-api-key" not in msg
    assert "not available" in msg


# ---------------------------------------------------------------------------
# refresh_secrets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_secrets_clears_cache_and_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """refresh_secrets clears lru_cache and re-reads the secrets file."""
    from kairix.secrets import load_secrets_file, refresh_secrets

    secrets_file = tmp_path / "kairix.env"
    secrets_file.write_text("MY_SECRET_A=original\n")
    monkeypatch.setenv("KAIRIX_SECRETS_FILE", str(secrets_file))

    # First load
    load_secrets_file.cache_clear()
    loaded = refresh_secrets(str(secrets_file))
    assert loaded >= 1
    assert os.environ.get("MY_SECRET_A") == "original"

    # Rotate the secret
    secrets_file.write_text("MY_SECRET_A=rotated\nMY_SECRET_B=new\n")

    # Without refresh, cache would return old value
    # After refresh, new value should be picked up
    monkeypatch.delenv("MY_SECRET_A", raising=False)
    monkeypatch.delenv("MY_SECRET_B", raising=False)
    loaded = refresh_secrets(str(secrets_file))
    assert loaded >= 2
    assert os.environ.get("MY_SECRET_A") == "rotated"
    assert os.environ.get("MY_SECRET_B") == "new"


@pytest.mark.unit
def test_refresh_secrets_returns_zero_when_no_file(tmp_path: Path) -> None:
    """refresh_secrets returns 0 when secrets file doesn't exist."""
    from kairix.secrets import refresh_secrets

    result = refresh_secrets(str(tmp_path / "nonexistent.env"))
    assert result == 0
