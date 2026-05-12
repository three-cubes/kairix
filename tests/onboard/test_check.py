"""
Tests for kairix.platform.onboard.check deployment health checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.platform.onboard.check import (
    CheckResult,
    OnboardChecksDeps,
    check_document_root_configured,
    check_neo4j_reachable,
    check_secrets_loaded,
    check_wrapper_installed,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# Fake Neo4j client for health check tests
# ---------------------------------------------------------------------------


class _FakeNeo4jClient:
    """Minimal fake Neo4j client for onboard health checks.

    Satisfies the subset of the GraphRepository protocol used by
    check_neo4j_reachable (available property + cypher method).
    """

    def __init__(self, *, available: bool = True, node_count: int = 0) -> None:
        self._available = available
        self._node_count = node_count

    @property
    def available(self) -> bool:
        return self._available

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        return [{"total": self._node_count}]


# ---------------------------------------------------------------------------
# check_wrapper_installed — Docker skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrapper_check_skipped_in_docker() -> None:
    """In Docker, wrapper_installed check returns ok=True without probing the binary."""
    result = check_wrapper_installed(deps=OnboardChecksDeps(is_docker=lambda: True))
    assert result.ok is True
    assert "Docker" in result.detail


# ---------------------------------------------------------------------------
# check_neo4j_reachable — fix hint content
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_neo4j_fix_hint_contains_install_script() -> None:
    """fix hint must reference install-neo4j.sh when Neo4j is unavailable."""
    fake_client = _FakeNeo4jClient(available=False)

    result = check_neo4j_reachable(neo4j_client=fake_client)

    assert not result.ok
    assert result.fix is not None
    assert "install-neo4j.sh" in result.fix
    assert "docker" in result.fix.lower()
    assert "optional" in result.fix.lower()


@pytest.mark.unit
def test_neo4j_fix_hint_contains_docker_run() -> None:
    """fix hint must include a docker run command as a quick-start option."""
    fake_client = _FakeNeo4jClient(available=False)

    result = check_neo4j_reachable(neo4j_client=fake_client)

    assert result.fix is not None
    assert "neo4j:5-community" in result.fix


@pytest.mark.unit
def test_neo4j_reachable_ok_when_has_nodes() -> None:
    """Returns ok=True when Neo4j is reachable and contains at least one node."""
    fake_client = _FakeNeo4jClient(available=True, node_count=42)

    result = check_neo4j_reachable(neo4j_client=fake_client)

    assert result.ok
    assert "42" in result.detail


@pytest.mark.unit
def test_neo4j_reachable_fail_when_empty() -> None:
    """Returns ok=False when Neo4j is reachable but empty (document store crawler not run)."""
    fake_client = _FakeNeo4jClient(available=True, node_count=0)

    result = check_neo4j_reachable(neo4j_client=fake_client)

    assert not result.ok
    assert result.fix is not None


@pytest.mark.unit
def test_neo4j_check_exception_surfaces_as_failed_result() -> None:
    """Exceptions from Neo4j client are caught and returned as a failed CheckResult."""

    # Pass a client that raises on attribute access to simulate ImportError path
    class _FailingClient:
        @property
        def available(self):
            raise ImportError("neo4j not installed")

    result = check_neo4j_reachable(neo4j_client=_FailingClient())

    assert not result.ok
    assert result.fix is not None


# ---------------------------------------------------------------------------
# check_secrets_loaded — two-tier probe
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_secrets_loaded_ok_from_env() -> None:
    env = {
        "KAIRIX_LLM_API_KEY": "key-abc12345",  # pragma: allowlist secret
        "KAIRIX_LLM_ENDPOINT": "https://example.openai.azure.com/",
    }
    result = check_secrets_loaded(env=env)
    assert result.ok
    assert "key-abc1" in result.detail  # masked key present


@pytest.mark.unit
def test_secrets_loaded_fail_when_missing() -> None:
    result = check_secrets_loaded(env={})
    assert not result.ok
    assert result.fix is not None


@pytest.mark.unit
def test_secrets_loaded_ok_from_file(tmp_path: Path) -> None:
    """Tier 2: secrets file with both keys present returns ok=True."""
    secrets_file = tmp_path / "kairix.env"
    secrets_file.write_text("KAIRIX_LLM_API_KEY=test-key\nKAIRIX_LLM_ENDPOINT=https://example.openai.azure.com/\n")

    result = check_secrets_loaded(env={"KAIRIX_SECRETS_FILE": str(secrets_file)})
    assert result.ok
    assert "Secrets file" in result.detail


# ---------------------------------------------------------------------------
# check_document_root_configured (document root configuration check)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_document_root_configured_ok(tmp_path: Path) -> None:
    md_file = tmp_path / "note.md"
    md_file.write_text("# test")
    result = check_document_root_configured(env={"KAIRIX_DOCUMENT_ROOT": str(tmp_path)})
    assert result.ok
    assert str(tmp_path) in result.detail


@pytest.mark.unit
def test_document_root_configured_missing_dir() -> None:
    result = check_document_root_configured(env={"KAIRIX_DOCUMENT_ROOT": "/nonexistent/path/vault"})
    assert not result.ok
    assert result.fix is not None


@pytest.mark.unit
def test_document_root_configured_not_set() -> None:
    result = check_document_root_configured(env={})
    assert not result.ok
    assert result.fix is not None


# ---------------------------------------------------------------------------
# run_all_checks — structural
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_all_checks_returns_list_of_check_results() -> None:
    """run_all_checks always returns a list of CheckResult objects without raising."""
    results = run_all_checks()
    assert isinstance(results, list)
    assert len(results) > 0
    for r in results:
        assert isinstance(r, CheckResult)
        assert isinstance(r.name, str)
        assert isinstance(r.ok, bool)
        assert isinstance(r.detail, str)
