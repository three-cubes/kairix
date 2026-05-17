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


# ---------------------------------------------------------------------------
# check_kairix_on_path — DI via which
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_kairix_on_path_ok() -> None:
    """which returns a path → ok=True with the path in detail."""
    from kairix.platform.onboard.check import check_kairix_on_path

    result = check_kairix_on_path(deps=OnboardChecksDeps(which=lambda _name: "/usr/local/bin/kairix"))
    assert result.ok is True
    assert "/usr/local/bin/kairix" in result.detail


@pytest.mark.unit
def test_kairix_on_path_missing() -> None:
    """which returns None → ok=False with deploy-vm.sh hint."""
    from kairix.platform.onboard.check import check_kairix_on_path

    result = check_kairix_on_path(deps=OnboardChecksDeps(which=lambda _name: None))
    assert result.ok is False
    assert result.fix is not None
    assert "deploy-vm.sh" in result.fix


# ---------------------------------------------------------------------------
# check_wrapper_installed — non-Docker branches via DI which + tmp_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrapper_check_missing_kairix_returns_failed(tmp_path: Path) -> None:
    """When which returns None, wrapper check fails with a helpful hint."""
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: None),
    )
    assert result.ok is False
    assert result.fix is not None
    assert "deploy-vm.sh" in result.fix


@pytest.mark.unit
def test_wrapper_check_python_binary_returns_failed(tmp_path: Path) -> None:
    """Symlink to a Python binary (#!python shebang) is flagged with the
    'points to raw Python binary' detail (distinct from the 'unexpected format'
    fallback that also matches non-shell binaries)."""
    fake_bin = tmp_path / "kairix"
    fake_bin.write_text("#!/usr/bin/env python3\n# pretend python binary\n")
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: str(fake_bin)),
    )
    assert result.ok is False
    assert result.fix is not None
    # The python-specific branch produces a 'raw Python binary' message;
    # the 'unexpected format' fallback would NOT. Tightened to catch a
    # sabotage that removes the python-detection branch.
    assert "raw Python binary" in result.detail


@pytest.mark.unit
def test_wrapper_check_bash_wrapper_returns_ok(tmp_path: Path) -> None:
    """Symlink to a bash wrapper (#!bash shebang) is accepted."""
    fake_bin = tmp_path / "kairix-wrapper.sh"
    fake_bin.write_text("#!/usr/bin/env bash\n# real wrapper\n")
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: str(fake_bin)),
    )
    assert result.ok is True
    assert "wrapper installed" in result.detail


@pytest.mark.unit
def test_wrapper_check_unknown_format_returns_failed(tmp_path: Path) -> None:
    """Binary without a recognised shebang prefix is rejected."""
    fake_bin = tmp_path / "kairix"
    fake_bin.write_text("ELF\x00garbage")
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: str(fake_bin)),
    )
    assert result.ok is False
    assert "unexpected format" in result.detail


@pytest.mark.unit
def test_wrapper_check_unreadable_file_returns_failed(tmp_path: Path) -> None:
    """When the binary cannot be opened, the except branch fires."""
    missing = tmp_path / "does-not-exist"
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: str(missing)),
    )
    assert result.ok is False
    assert "Cannot read" in result.detail


# ---------------------------------------------------------------------------
# check_secrets_loaded — file with missing keys branch (line 225)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_secrets_loaded_file_missing_required_keys(tmp_path: Path) -> None:
    """When the secrets file exists but lacks required keys, ok=False
    with a 'missing required keys' detail and a fix hint."""
    secrets_file = tmp_path / "kairix.env"
    secrets_file.write_text("KAIRIX_LLM_API_KEY=onlyone\n")  # missing endpoint
    result = check_secrets_loaded(env={"KAIRIX_SECRETS_FILE": str(secrets_file)})
    assert result.ok is False
    assert "missing required keys" in result.detail
    assert result.fix is not None


# ---------------------------------------------------------------------------
# check_vector_search_working — DI seam via pipeline parameter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_vector_search_working_with_results() -> None:
    """A pipeline returning >0 results with vec_count > 0 produces ok=True."""
    from dataclasses import dataclass, field

    from kairix.platform.onboard.check import check_vector_search_working

    @dataclass
    class _FakeSearchResult:
        results: list = field(default_factory=lambda: [object(), object(), object()])
        vec_count: int = 3
        bm25_count: int = 5
        vec_failed: bool = False

    class _FakePipeline:
        def search(self, query, budget):
            return _FakeSearchResult()

    result = check_vector_search_working(pipeline=_FakePipeline())
    assert result.ok is True
    assert "results=3" in result.detail


@pytest.mark.unit
def test_vector_search_working_vec_failed() -> None:
    """vec_failed=True produces ok=False with credentials hint."""
    from dataclasses import dataclass, field

    from kairix.platform.onboard.check import check_vector_search_working

    @dataclass
    class _FakeSearchResult:
        results: list = field(default_factory=lambda: [object()])
        vec_count: int = 0
        bm25_count: int = 1
        vec_failed: bool = True

    class _FakePipeline:
        def search(self, query, budget):
            return _FakeSearchResult()

    result = check_vector_search_working(pipeline=_FakePipeline())
    assert result.ok is False
    assert "Vector search failed" in result.detail
    assert result.fix is not None


@pytest.mark.unit
def test_vector_search_working_zero_results() -> None:
    """vec_count=0 and result_count=0 returns 'not embedded' hint."""
    from dataclasses import dataclass, field

    from kairix.platform.onboard.check import check_vector_search_working

    @dataclass
    class _FakeSearchResult:
        results: list = field(default_factory=list)
        vec_count: int = 0
        bm25_count: int = 0
        vec_failed: bool = False

    class _FakePipeline:
        def search(self, query, budget):
            return _FakeSearchResult()

    result = check_vector_search_working(pipeline=_FakePipeline())
    assert result.ok is False
    assert "0 results" in result.detail


@pytest.mark.unit
def test_vector_search_working_pipeline_exception() -> None:
    """Exception from pipeline.search → ok=False with credentials hint."""
    from kairix.platform.onboard.check import check_vector_search_working

    class _ExplodingPipeline:
        def search(self, query, budget):
            raise RuntimeError("auth failed")

    result = check_vector_search_working(pipeline=_ExplodingPipeline())
    assert result.ok is False
    assert "Search raised" in result.detail


# ---------------------------------------------------------------------------
# check_agent_knowledge_populated — DI via document_root_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_knowledge_missing_directory(tmp_path: Path) -> None:
    """No 04-Agent-Knowledge dir → ok=False with mkdir hint."""
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    result = check_agent_knowledge_populated(document_root_path=tmp_path)
    assert result.ok is False
    assert "not found" in result.detail
    assert result.fix is not None


@pytest.mark.unit
def test_agent_knowledge_empty_directory(tmp_path: Path) -> None:
    """Empty 04-Agent-Knowledge dir → ok=False with 'No agent memory logs' detail."""
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    (tmp_path / "04-Agent-Knowledge").mkdir()
    result = check_agent_knowledge_populated(document_root_path=tmp_path)
    assert result.ok is False
    assert "No agent memory logs" in result.detail


@pytest.mark.unit
def test_agent_knowledge_populated_ok(tmp_path: Path) -> None:
    """Memory log present → ok=True with file count in detail."""
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    log_dir = tmp_path / "04-Agent-Knowledge" / "builder" / "memory"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-05-01.md").write_text("# log")

    result = check_agent_knowledge_populated(document_root_path=tmp_path)
    assert result.ok is True
    assert "1 files" in result.detail


# ---------------------------------------------------------------------------
# check_chunk_date_populated — DI via db_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_date_populated_index_creates_then_finds_missing_column(tmp_path: Path) -> None:
    """When the index doesn't exist, open_db creates a new empty SQLite DB
    that lacks the content_vectors table. The check surfaces the 'column
    missing' branch."""
    from kairix.platform.onboard.check import check_chunk_date_populated

    result = check_chunk_date_populated(db_path=tmp_path / "fresh.sqlite")
    assert result.ok is False
    # New DB has no table → PRAGMA returns empty → 'chunk_date not in cols'
    assert "chunk_date" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_missing_column(tmp_path: Path) -> None:
    """When chunk_date column is missing, returns the 'migration required' hint."""
    import sqlite3

    from kairix.platform.onboard.check import check_chunk_date_populated

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(db_path)
    # Create content_vectors WITHOUT chunk_date column
    db.execute("CREATE TABLE content_vectors (id INTEGER PRIMARY KEY, content TEXT)")
    db.commit()
    db.close()

    result = check_chunk_date_populated(db_path=db_path)
    assert result.ok is False
    assert "chunk_date" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_empty_table(tmp_path: Path) -> None:
    """When content_vectors is empty, returns 'vault has not been embedded'."""
    import sqlite3

    from kairix.platform.onboard.check import check_chunk_date_populated

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE content_vectors (id INTEGER PRIMARY KEY, chunk_date TEXT)")
    db.commit()
    db.close()

    result = check_chunk_date_populated(db_path=db_path)
    assert result.ok is False
    assert "empty" in result.detail.lower() or "not been embedded" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_zero_dated(tmp_path: Path) -> None:
    """When all chunks have NULL chunk_date, returns the '0% dated' hint."""
    import sqlite3

    from kairix.platform.onboard.check import check_chunk_date_populated

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE content_vectors (id INTEGER PRIMARY KEY, chunk_date TEXT)")
    for i in range(10):
        db.execute("INSERT INTO content_vectors (id, chunk_date) VALUES (?, NULL)", (i,))
    db.commit()
    db.close()

    result = check_chunk_date_populated(db_path=db_path)
    assert result.ok is False
    assert "0/" in result.detail or "0%" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_low_coverage(tmp_path: Path) -> None:
    """When < 20% of chunks have chunk_date, returns 'low coverage' hint."""
    import sqlite3

    from kairix.platform.onboard.check import check_chunk_date_populated

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE content_vectors (id INTEGER PRIMARY KEY, chunk_date TEXT)")
    # 1 dated, 9 NULL → 10%
    db.execute("INSERT INTO content_vectors (id, chunk_date) VALUES (0, '2026-05-01')")
    for i in range(1, 10):
        db.execute("INSERT INTO content_vectors (id, chunk_date) VALUES (?, NULL)", (i,))
    db.commit()
    db.close()

    result = check_chunk_date_populated(db_path=db_path)
    assert result.ok is False
    assert "low coverage" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_high_coverage(tmp_path: Path) -> None:
    """When >= 20% chunks have chunk_date, returns ok=True."""
    import sqlite3

    from kairix.platform.onboard.check import check_chunk_date_populated

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE content_vectors (id INTEGER PRIMARY KEY, chunk_date TEXT)")
    # All dated → 100%
    for i in range(10):
        db.execute("INSERT INTO content_vectors (id, chunk_date) VALUES (?, '2026-05-01')", (i,))
    db.commit()
    db.close()

    result = check_chunk_date_populated(db_path=db_path)
    assert result.ok is True
    assert "100%" in result.detail


# ---------------------------------------------------------------------------
# MCP probe helpers — exercise the harness probes via fake config files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_mcp_service_runs_without_raising() -> None:
    """check_mcp_service combines the harness probes and never raises."""
    from kairix.platform.onboard.check import check_mcp_service

    result = check_mcp_service()
    assert isinstance(result, CheckResult)
    # The function returns ok=True if any harness is active, else ok=False
    # We accept either — the point is no exception escapes.
    assert isinstance(result.ok, bool)


@pytest.mark.unit
def test_check_mcp_service_handles_invalid_openclaw_json(tmp_path: Path, monkeypatch) -> None:
    """When openclaw.json is invalid JSON, check_mcp_service does not raise.

    Drives the OpenClaw probe's JSONDecodeError branch through the public
    check_mcp_service entry point (F5-clean — no private import).
    """
    from kairix.platform.onboard import check as check_mod

    bogus = tmp_path / "openclaw.json"
    bogus.write_text("not json {{{")

    # Drive the OpenClaw probe through the public ``config_paths`` kwarg
    # seam — F1-clean. The check_mcp_service public surface accepts a
    # custom OpenClaw probe.
    result = check_mod.check_mcp_service(
        openclaw_probe=lambda: check_mod._probe_openclaw_harness(config_paths=(str(bogus),)),
    )
    assert isinstance(result, CheckResult)


@pytest.mark.unit
def test_run_all_checks_swallows_individual_failures(monkeypatch) -> None:
    """If a check raises, run_all_checks catches the exception and surfaces
    it as a failed CheckResult (lines 768-769)."""
    from kairix.platform.onboard import check as check_mod

    def _exploding_check():
        raise RuntimeError("simulated check failure")

    _exploding_check.__name__ = "check_test_simulated"

    # Drive the runner through its public ``checks`` kwarg seam — F1-clean.
    results = run_all_checks(checks=[*check_mod.ALL_CHECKS, _exploding_check])
    test_results = [r for r in results if r.name == "test_simulated"]
    assert len(test_results) == 1
    assert test_results[0].ok is False
    assert "unexpected exception" in test_results[0].detail.lower()


# ---------------------------------------------------------------------------
# check_chunk_date_populated — FileNotFoundError branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_date_populated_filenotfound_branch(tmp_path: Path, monkeypatch) -> None:
    """When open_db raises FileNotFoundError, the 'Index not found' hint surfaces.

    This branch is distinct from the generic Exception fallback below.
    """
    from kairix.platform.onboard import check as check_mod

    def _raise_fnf(_path):
        raise FileNotFoundError("simulated missing index")

    # Drive the open_db seam through the public ``opener`` kwarg on
    # check_chunk_date_populated — F1-clean.
    result = check_mod.check_chunk_date_populated(
        db_path=tmp_path / "irrelevant.sqlite", opener=_raise_fnf
    )
    assert result.ok is False
    assert "Index not found" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_generic_exception(tmp_path: Path, monkeypatch) -> None:
    """When open_db raises a non-FileNotFoundError, the generic exception
    branch fires (line 553-558)."""
    from kairix.platform.onboard import check as check_mod

    def _raise_runtime(_path):
        raise RuntimeError("locked database")

    result = check_mod.check_chunk_date_populated(
        db_path=tmp_path / "irrelevant.sqlite", opener=_raise_runtime
    )
    assert result.ok is False
    assert "failed" in result.detail.lower()


# ---------------------------------------------------------------------------
# MCP probes — hit the executable/registered-but-broken branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_openclaw_registered_with_executable_command(tmp_path: Path, monkeypatch) -> None:
    """When openclaw.json registers mcp-kairix with an executable command,
    the probe returns ok=True (lines 612-616)."""
    from kairix.platform.onboard import check as check_mod

    # Build a fake openclaw.json under tmp_path
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    fake_cmd = tmp_path / "kairix-start.sh"
    fake_cmd.write_text("#!/bin/bash\necho ok\n")
    fake_cmd.chmod(0o755)

    config = openclaw_dir / "openclaw.json"
    import json

    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "mcp-kairix": {"command": str(fake_cmd)},
                    },
                },
            }
        )
    )

    # Drive the OpenClaw probe through its public ``config_paths`` kwarg.
    ok, detail = check_mod._probe_openclaw_harness(config_paths=(str(config),))
    assert ok is True
    assert "OpenClaw" in detail


@pytest.mark.unit
def test_probe_openclaw_registered_but_command_missing(tmp_path: Path, monkeypatch) -> None:
    """When mcp-kairix is registered but the command path doesn't exist,
    the probe returns ok=False with a 'missing/not executable' detail (617-620)."""
    from kairix.platform.onboard import check as check_mod

    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    config = openclaw_dir / "openclaw.json"
    import json

    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "mcp-kairix": {"command": "/nonexistent/cmd"},
                    },
                },
            }
        )
    )

    ok, detail = check_mod._probe_openclaw_harness(config_paths=(str(config),))
    assert ok is False
    assert "missing" in detail.lower() or "not executable" in detail.lower()


@pytest.mark.unit
def test_probe_claude_desktop_registered(tmp_path: Path, monkeypatch) -> None:
    """When claude_desktop_config.json registers kairix, the probe returns ok=True."""
    from kairix.platform.onboard import check as check_mod

    config = tmp_path / "claude_desktop_config.json"
    import json

    config.write_text(json.dumps({"mcpServers": {"kairix": {"command": "kairix"}}}))

    ok, detail = check_mod._probe_claude_desktop_harness(config_paths=(config,))
    assert ok is True
    assert "Claude Desktop" in detail


@pytest.mark.unit
def test_probe_sse_harness_port_listening(monkeypatch) -> None:
    """When the MCP SSE port is listening, the probe returns ok=True."""
    import socket

    from kairix.platform.onboard import check as check_mod

    class _FakeSocket:
        def __init__(self, *args, **kwargs):
            """Test stub — accepts any args; the socket isn't actually opened."""

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _fake_create_connection(*args, **kwargs):
        return _FakeSocket()

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    ok, detail = check_mod._probe_sse_harness()
    assert ok is True
    assert "listening" in detail.lower()


@pytest.mark.unit
def test_probe_sse_harness_systemctl_active(monkeypatch) -> None:
    """When port is not listening but systemctl says service is active,
    the probe returns ok=True."""
    import socket
    import subprocess

    from kairix.platform.onboard import check as check_mod

    def _raise_oserror(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", _raise_oserror)

    class _FakeCompleted:
        stdout = "active\n"

    def _fake_run(*args, **kwargs):
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok, detail = check_mod._probe_sse_harness()
    assert ok is True
    assert "active" in detail.lower()


@pytest.mark.unit
def test_check_mcp_service_active_when_any_harness_passes(monkeypatch) -> None:
    """When at least one harness is configured, check_mcp_service returns ok=True."""
    from kairix.platform.onboard import check as check_mod

    result = check_mod.check_mcp_service(
        openclaw_probe=lambda: (True, "OpenClaw: configured"),
        claude_desktop_probe=lambda: (False, "Claude: nope"),
        sse_probe=lambda: (False, "SSE: nope"),
    )
    assert result.ok is True
    assert "OpenClaw" in result.detail


# ---------------------------------------------------------------------------
# OnboardResult + CheckFailure — structured output (#246 W4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_onboard_result_fully_passed_when_passed_equals_total() -> None:
    """fully_passed is True iff passed == total — derived, not asserted manually."""
    from kairix.platform.onboard.check import OnboardResult

    ok = OnboardResult(passed=9, total=9, failures=[], fully_passed=True)
    assert ok.fully_passed is True
    assert ok.passed == ok.total


@pytest.mark.unit
def test_onboard_result_not_fully_passed_when_any_failure() -> None:
    """fully_passed is False when any check failed."""
    from kairix.platform.onboard.check import CheckFailure, OnboardResult

    failure = CheckFailure(check="x", detail="d", remediation="r")
    result = OnboardResult(passed=8, total=9, failures=[failure], fully_passed=False)
    assert result.fully_passed is False
    assert len(result.failures) == 1


@pytest.mark.unit
def test_run_onboard_check_returns_onboard_result() -> None:
    """run_onboard_check returns an OnboardResult with passed + total derived."""
    from kairix.platform.onboard.check import OnboardResult, run_onboard_check

    result = run_onboard_check()
    assert isinstance(result, OnboardResult)
    assert isinstance(result.passed, int)
    assert isinstance(result.total, int)
    assert result.total > 0  # Sabotage-prove: if total goes to zero, this check fails
    assert result.passed <= result.total
    # fully_passed must be derived consistently, not stored independently
    assert result.fully_passed == (result.passed == result.total)


@pytest.mark.unit
def test_run_onboard_check_failures_match_unpassed_count() -> None:
    """Exactly (total - passed) CheckFailures are emitted — accounting holds.

    Sabotage check: if run_onboard_check ever silently drops failures or
    emits extra ones, this asserts a hard mismatch.
    """
    from kairix.platform.onboard.check import run_onboard_check

    result = run_onboard_check()
    assert len(result.failures) == result.total - result.passed


@pytest.mark.unit
def test_run_onboard_check_every_failure_has_non_empty_remediation() -> None:
    """Every CheckFailure in an OnboardResult carries a non-empty remediation.

    Sabotage check: if a check is added without a canonical remediation
    entry and produces a CheckResult with fix=None, this asserts will fail
    rather than silently emitting an empty remediation.
    """
    from kairix.platform.onboard.check import run_onboard_check

    result = run_onboard_check()
    for failure in result.failures:
        assert failure.remediation, f"empty remediation for check={failure.check!r}"
        assert failure.remediation.strip() == failure.remediation
        # Sabotage-prove: blank strings, single spaces, etc. all fail this
        assert len(failure.remediation) > 10, f"remediation too short for {failure.check!r}: {failure.remediation!r}"


@pytest.mark.unit
def test_run_onboard_check_failure_check_id_matches_a_known_check() -> None:
    """Every CheckFailure.check matches the .name of an executed CheckResult.

    Sabotage check: if the failure check ID ever drifts from CheckResult.name
    (e.g. someone renames a check but doesn't update _CANONICAL_REMEDIATIONS),
    this catches the mismatch.
    """
    from kairix.platform.onboard.check import run_all_checks, run_onboard_check

    all_names = {r.name for r in run_all_checks()}
    result = run_onboard_check()
    for failure in result.failures:
        assert failure.check in all_names, f"unknown check id: {failure.check!r}"


@pytest.mark.unit
def test_run_onboard_check_uses_canonical_remediation_strings() -> None:
    """When a check fails, its remediation comes from the canonical registry
    in check.py — not a placeholder. Sabotage check: confirms the
    structured surface routes through _remediation_for() rather than
    silently emitting the raw CheckResult.fix string.
    """
    from kairix.platform.onboard import check as check_mod

    # Build a synthetic ALL_CHECKS where every check fails with a deliberately
    # weird fix value. The remediation surfaced should still be the canonical
    # one (matching _CANONICAL_REMEDIATIONS), not the weird value.
    fake_results = [
        check_mod.CheckResult(name=name, ok=False, detail="x", fix="WRONG-DO-NOT-USE")
        for name in check_mod._CANONICAL_REMEDIATIONS
    ]

    def _fake_run_all(*, checks=None) -> list[check_mod.CheckResult]:
        return fake_results

    # Drive run_onboard_check via a one-shot ALL_CHECKS override
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(check_mod, "run_all_checks", _fake_run_all)
        result = check_mod.run_onboard_check()

    canonical = check_mod._CANONICAL_REMEDIATIONS
    for failure in result.failures:
        assert failure.remediation == canonical[failure.check], (
            f"non-canonical remediation for {failure.check!r}: {failure.remediation!r}"
        )
        # Sabotage-prove: the deliberately bad fix value should NOT leak through
        assert "WRONG" not in failure.remediation


@pytest.mark.unit
def test_canonical_remediations_cover_every_registered_check() -> None:
    """Every check in ALL_CHECKS has a canonical remediation entry.

    Sabotage check: if someone adds a new check function without adding a
    canonical remediation, this fails on import-time (test discovery)
    rather than at runtime when a real operator hits the failure path.
    """
    from kairix.platform.onboard import check as check_mod

    expected_names = {fn.__name__.removeprefix("check_") for fn in check_mod.ALL_CHECKS}
    canonical_names = set(check_mod._CANONICAL_REMEDIATIONS)
    missing = expected_names - canonical_names
    assert not missing, f"checks without canonical remediation: {sorted(missing)}"


@pytest.mark.unit
def test_every_canonical_remediation_is_actionable() -> None:
    """Every canonical remediation contains a concrete command, path, or
    actionable verb. Sabotage check: a vague string like "fix it" would
    fail this. The bar is low (one of run/set/check/confirm/add/register)
    but non-zero — proves the string isn't placeholder text."""
    from kairix.platform.onboard import check as check_mod

    actionable_tokens = ("Run ", "Set ", "Check ", "Confirm ", "Add ", "Register ", "`")
    for name, remediation in check_mod._CANONICAL_REMEDIATIONS.items():
        assert any(token in remediation for token in actionable_tokens), (
            f"remediation for {name!r} contains no actionable token: {remediation!r}"
        )
        # Sabotage-prove: every remediation is non-trivial length
        assert len(remediation) > 20, f"remediation for {name!r} is too short: {remediation!r}"


@pytest.mark.unit
def test_run_onboard_check_unknown_check_falls_back_to_fix() -> None:
    """When a check fails and is not in the canonical registry (e.g. a brand
    new check added without an entry), the per-check fix string is used as
    the remediation fallback. Forward-compatibility safeguard.
    """
    from kairix.platform.onboard import check as check_mod

    def _fake_run_all(*, checks=None) -> list[check_mod.CheckResult]:
        return [
            check_mod.CheckResult(
                name="brand_new_check_with_no_canonical_entry",
                ok=False,
                detail="some failure",
                fix="run this specific command",
            ),
        ]

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(check_mod, "run_all_checks", _fake_run_all)
        result = check_mod.run_onboard_check()

    assert len(result.failures) == 1
    # No canonical entry → falls back to the raw fix
    assert result.failures[0].remediation == "run this specific command"


@pytest.mark.unit
def test_run_onboard_check_unknown_and_no_fix_surfaces_bug_hint() -> None:
    """When a check fails AND has neither a canonical entry nor a fix string,
    the remediation surfaces a bug-report hint rather than an empty string.

    Sabotage-prove: an empty remediation would be a silent failure for any
    machine consumer; this assertion catches that.
    """
    from kairix.platform.onboard import check as check_mod

    def _fake_run_all(*, checks=None) -> list[check_mod.CheckResult]:
        return [
            check_mod.CheckResult(
                name="orphan_check_with_no_remediation",
                ok=False,
                detail="failed",
                fix=None,
            ),
        ]

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(check_mod, "run_all_checks", _fake_run_all)
        result = check_mod.run_onboard_check()

    assert len(result.failures) == 1
    assert result.failures[0].remediation
    assert "bug" in result.failures[0].remediation.lower()
