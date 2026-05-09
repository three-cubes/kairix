"""
Tests for kairix.secrets — sidecar secrets file loader and get_secret resolver.

All tests pass an explicit ``env={}`` dict (and where relevant a tmp_path
secrets dir) instead of mutating ``os.environ``. ``load_secrets`` mutates
the env mapping it's given; tests pass a fresh dict so the test's view of
"env" is hermetic.
"""

from __future__ import annotations

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
    count = load_secrets(str(tmp_path / "nonexistent.env"), env={})
    assert count == 0


@pytest.mark.unit
def test_returns_zero_for_empty_file(tmp_path) -> None:
    path = _write_secrets(tmp_path, "")
    assert load_secrets(path, env={}) == 0


@pytest.mark.unit
def test_returns_zero_for_comments_only(tmp_path) -> None:
    path = _write_secrets(tmp_path, "# This is a comment\n# Another comment\n")
    assert load_secrets(path, env={}) == 0


# ---------------------------------------------------------------------------
# load_secrets: Loading values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_loads_single_key_value(tmp_path) -> None:
    env: dict[str, str] = {}
    path = _write_secrets(tmp_path, "TEST_VAR_ALPHA=hello\n")
    count = load_secrets(path, env=env)
    assert count == 1
    assert env["TEST_VAR_ALPHA"] == "hello"


@pytest.mark.unit
def test_loads_multiple_keys(tmp_path) -> None:
    env: dict[str, str] = {}
    path = _write_secrets(tmp_path, "SECRET_A=val1\nSECRET_B=val2\n")
    count = load_secrets(path, env=env)
    assert count == 2
    assert env["SECRET_A"] == "val1"
    assert env["SECRET_B"] == "val2"


@pytest.mark.unit
def test_value_with_equals_sign(tmp_path) -> None:
    """Values containing '=' are supported (partition splits on first '=' only)."""
    env: dict[str, str] = {}
    path = _write_secrets(tmp_path, "URL_VAR=https://example.com/path?foo=bar\n")
    load_secrets(path, env=env)
    assert env["URL_VAR"] == "https://example.com/path?foo=bar"


@pytest.mark.unit
def test_ignores_blank_lines(tmp_path) -> None:
    env: dict[str, str] = {}
    path = _write_secrets(tmp_path, "\n\nONLY_VAR=yes\n\n")
    count = load_secrets(path, env=env)
    assert count == 1


@pytest.mark.unit
def test_ignores_comment_lines(tmp_path) -> None:
    env: dict[str, str] = {}
    content = "# comment\nREAL_VAR=real\n# another comment\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path, env=env)
    assert count == 1
    assert env["REAL_VAR"] == "real"


@pytest.mark.unit
def test_ignores_lines_without_equals(tmp_path) -> None:
    env: dict[str, str] = {}
    content = "BADLINE\nGOOD_VAR=ok\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path, env=env)
    assert count == 1


# ---------------------------------------------------------------------------
# load_secrets: Priority — existing env entries are NOT overwritten
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_does_not_overwrite_existing_env_var(tmp_path) -> None:
    env = {"PROTECTED_VAR": "original"}
    path = _write_secrets(tmp_path, "PROTECTED_VAR=override\n")
    count = load_secrets(path, env=env)
    assert count == 0  # not loaded — already set
    assert env["PROTECTED_VAR"] == "original"


@pytest.mark.unit
def test_partial_load_when_some_already_set(tmp_path) -> None:
    env = {"ALREADY_SET": "existing"}
    content = "ALREADY_SET=new\nNOT_SET_YET=fresh\n"
    path = _write_secrets(tmp_path, content)
    count = load_secrets(path, env=env)
    assert count == 1
    assert env["ALREADY_SET"] == "existing"
    assert env["NOT_SET_YET"] == "fresh"


# ---------------------------------------------------------------------------
# load_secrets: KAIRIX_SECRETS_FILE entry controls default path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_uses_kairix_secrets_file_env_var(tmp_path) -> None:
    path = _write_secrets(tmp_path, "ENV_FROM_FILE=loaded\n")
    env = {"KAIRIX_SECRETS_FILE": path}
    count = load_secrets(env=env)  # no explicit path — reads from env mapping
    assert count == 1
    assert env["ENV_FROM_FILE"] == "loaded"


# ---------------------------------------------------------------------------
# load_secrets: Error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_zero_on_permission_error(tmp_path) -> None:
    """load_secrets should not raise even if the file can't be read."""
    path = _write_secrets(tmp_path, "X=1\n")
    import os as _os

    _os.chmod(path, 0o000)
    try:
        count = load_secrets(path, env={})
        assert count == 0
    finally:
        _os.chmod(path, 0o644)


@pytest.mark.unit
def test_idempotent_multiple_calls(tmp_path) -> None:
    """Calling load_secrets twice on the same env adds nothing the second time."""
    env: dict[str, str] = {}
    path = _write_secrets(tmp_path, "IDEMPOTENT_VAR=once\n")
    count1 = load_secrets(path, env=env)
    count2 = load_secrets(path, env=env)
    assert count1 == 1
    assert count2 == 0  # already set after first call
    assert env["IDEMPOTENT_VAR"] == "once"


# ---------------------------------------------------------------------------
# get_secret: Step 1 — direct env var resolution (highest priority)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_from_env_var() -> None:
    """get_secret returns value when the mapped env var is set."""
    env = {
        "KAIRIX_LLM_API_KEY": "test-key-from-env",  # pragma: allowlist secret
        "KAIRIX_SECRETS_DIR": "/nonexistent-dir-abc123",
    }
    value = get_secret("kairix-llm-api-key", env=env)
    assert value == "test-key-from-env"


@pytest.mark.unit
def test_get_secret_env_var_takes_priority_over_file(tmp_path) -> None:
    """Env var wins over sidecar file — highest priority."""
    p = tmp_path / "kairix.env"
    p.write_text("KAIRIX_LLM_API_KEY=file-value\n", encoding="utf-8")
    env = {
        "KAIRIX_LLM_API_KEY": "env-wins",  # pragma: allowlist secret
        "KAIRIX_SECRETS_DIR": str(tmp_path),
    }
    value = get_secret("kairix-llm-api-key", env=env)
    assert value == "env-wins"


# ---------------------------------------------------------------------------
# get_secret: Step 2 — file-based resolution (sidecar secrets file)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_from_file(tmp_path) -> None:
    """get_secret reads from the sidecar secrets file when env var is absent."""
    p = tmp_path / "kairix.env"
    p.write_text("KAIRIX_LLM_ENDPOINT=https://example.openai.azure.com\n", encoding="utf-8")
    env = {"KAIRIX_SECRETS_DIR": str(tmp_path)}
    # Clear lru_cache so this path is freshly read
    from kairix.secrets import load_secrets_file

    load_secrets_file.cache_clear()
    value = get_secret("kairix-llm-endpoint", env=env)
    assert value == "https://example.openai.azure.com"


@pytest.mark.unit
def test_get_secret_file_ignores_comments_and_blank_lines(tmp_path) -> None:
    """File parser skips # comments and blank lines."""
    content = "# generated by vault-agent\n\nKAIRIX_NEO4J_PASSWORD=s3cr3t\n# end\n"  # pragma: allowlist secret
    p = tmp_path / "kairix.env"
    p.write_text(content, encoding="utf-8")
    env = {"KAIRIX_SECRETS_DIR": str(tmp_path)}
    from kairix.secrets import load_secrets_file

    load_secrets_file.cache_clear()
    value = get_secret("kairix-neo4j-password", env=env)
    assert value == "s3cr3t"


# ---------------------------------------------------------------------------
# get_secret: Missing secret error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_secret_required_raises_oserror() -> None:
    """Missing required secret raises OSError with an informative message."""
    env = {"KAIRIX_SECRETS_DIR": "/nonexistent-dir-abc123"}
    with pytest.raises(OSError, match="not available"):
        get_secret("kairix-llm-api-key", env=env)


@pytest.mark.unit
def test_get_secret_required_true_is_default() -> None:
    """required=True is the default — omitting it raises on missing secret."""
    env = {"KAIRIX_SECRETS_DIR": "/nonexistent-dir-abc123"}
    with pytest.raises(OSError):
        get_secret("kairix-neo4j-password", env=env)


@pytest.mark.unit
def test_get_secret_not_required_returns_none() -> None:
    """required=False returns None instead of raising when secret is absent."""
    env = {"KAIRIX_SECRETS_DIR": "/nonexistent-dir-abc123"}
    result = get_secret("kairix-llm-api-key", required=False, env=env)
    assert result is None


@pytest.mark.unit
def test_get_secret_oserror_message_is_informative() -> None:
    """OSError message must NOT name the requested secret (security)."""
    env = {"KAIRIX_SECRETS_DIR": "/nonexistent-dir-abc123"}
    with pytest.raises(OSError) as exc_info:
        get_secret("kairix-llm-api-key", env=env)
    msg = str(exc_info.value)
    # Error message must NOT contain the secret name (security: no key names in output)
    assert "kairix-llm-api-key" not in msg
    assert "not available" in msg


# ---------------------------------------------------------------------------
# refresh_secrets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_secrets_clears_cache_and_reloads(tmp_path: Path) -> None:
    """refresh_secrets clears lru_cache and re-reads the secrets file."""
    from kairix.secrets import load_secrets_file, refresh_secrets

    secrets_file = tmp_path / "kairix.env"
    secrets_file.write_text("MY_SECRET_A=original\n")

    # First load
    load_secrets_file.cache_clear()
    env: dict[str, str] = {"KAIRIX_SECRETS_FILE": str(secrets_file)}
    loaded = refresh_secrets(str(secrets_file), env=env)
    assert loaded >= 1
    assert env.get("MY_SECRET_A") == "original"

    # Rotate the secret on disk and reload into a fresh env
    secrets_file.write_text("MY_SECRET_A=rotated\nMY_SECRET_B=new\n")
    fresh_env: dict[str, str] = {"KAIRIX_SECRETS_FILE": str(secrets_file)}
    loaded = refresh_secrets(str(secrets_file), env=fresh_env)
    assert loaded >= 2
    assert fresh_env.get("MY_SECRET_A") == "rotated"
    assert fresh_env.get("MY_SECRET_B") == "new"


@pytest.mark.unit
def test_refresh_secrets_returns_zero_when_no_file(tmp_path: Path) -> None:
    """refresh_secrets returns 0 when secrets file doesn't exist."""
    from kairix.secrets import refresh_secrets

    result = refresh_secrets(str(tmp_path / "nonexistent.env"), env={})
    assert result == 0
