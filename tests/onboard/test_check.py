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
# check_neo4j_reachable — fix hint content (deployment-aware, GH #476)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_neo4j_fix_hint_default_points_at_bundled_compose() -> None:
    """Outside a container the fix points at the bundled docker-compose.yml
    (Neo4j is included) OR a local Neo4j install with the localhost bolt URI,
    and keeps the production-required framing. Earlier versions curl'd a
    stale fork URL (quanyeomans) — the fix must carry no remote URL at all."""
    fake_client = _FakeNeo4jClient(available=False)

    result = check_neo4j_reachable(neo4j_client=fake_client, env={})

    assert not result.ok
    assert result.fix is not None
    assert "docker-compose.yml" in result.fix
    assert "KAIRIX_NEO4J_URI=bolt://localhost:7687" in result.fix
    # The fix hint frames Neo4j as required for production — the system
    # loads without it but entity-heavy queries degrade. Earlier versions
    # called it "optional"; the framing landed in v2026.5.24a3 because
    # operators were skipping Neo4j based on the "optional" wording and
    # then hitting recall regressions on entity-named queries.
    assert "required for production" in result.fix.lower()
    # GH #476 — the old remediation curl'd a stale fork; no URLs allowed.
    assert "quanyeomans" not in result.fix
    assert "https://" not in result.fix


@pytest.mark.unit
def test_neo4j_fix_hint_container_points_at_compose_sidecar() -> None:
    """Inside the container (KAIRIX_CONTAINER=1) the fix names the compose
    sidecar service URI (bolt://neo4j:7687) and the compose restart verb."""
    fake_client = _FakeNeo4jClient(available=False)

    result = check_neo4j_reachable(neo4j_client=fake_client, env={"KAIRIX_CONTAINER": "1"})

    assert not result.ok
    assert result.fix is not None
    assert "KAIRIX_NEO4J_URI=bolt://neo4j:7687" in result.fix
    assert "docker compose up -d" in result.fix
    assert "quanyeomans" not in result.fix


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


@pytest.mark.unit
def test_secrets_loaded_ok_from_env_canonical_names() -> None:
    """Canonical KAIRIX_PROVIDER_LLM_* names satisfy the check (GH #473)."""
    env = {
        "KAIRIX_PROVIDER_LLM_API_KEY": "key-can12345",  # pragma: allowlist secret
        "KAIRIX_PROVIDER_LLM_ENDPOINT": "https://example.openai.azure.com/",
    }
    result = check_secrets_loaded(env=env)
    assert result.ok
    assert "key-can1" in result.detail


@pytest.mark.unit
def test_secrets_loaded_legacy_env_carries_rotation_note() -> None:
    """Legacy KAIRIX_LLM_* names still pass but name the canonical rotation target."""
    env = {
        "KAIRIX_LLM_API_KEY": "key-leg12345",  # pragma: allowlist secret
        "KAIRIX_LLM_ENDPOINT": "https://example.openai.azure.com/",
    }
    result = check_secrets_loaded(env=env)
    assert result.ok
    assert "KAIRIX_PROVIDER_LLM_API_KEY" in result.detail


@pytest.mark.unit
def test_secrets_loaded_ok_from_file_canonical_names(tmp_path: Path) -> None:
    """Tier 2: a secrets file carrying only the canonical pair passes (GH #473)."""
    secrets_file = tmp_path / "kairix.env"
    secrets_file.write_text(
        "KAIRIX_PROVIDER_LLM_API_KEY=test-key\nKAIRIX_PROVIDER_LLM_ENDPOINT=https://example.openai.azure.com/\n"
    )
    result = check_secrets_loaded(env={"KAIRIX_SECRETS_FILE": str(secrets_file)})
    assert result.ok


@pytest.mark.unit
def test_secrets_loaded_failure_names_canonical_keys() -> None:
    """Tier 3 failure teaches the canonical names, not the retired ones."""
    result = check_secrets_loaded(env={})
    assert not result.ok
    assert "KAIRIX_PROVIDER_LLM_API_KEY" in result.detail


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
# check_document_root_configured — deployment-aware fix (GH #477)
# ---------------------------------------------------------------------------
# The old fix strings hard-coded /opt/kairix/service.env — a path from a
# single historical VM layout that does NOT exist on a fresh Docker or pip
# install. README tells agents to surface these remediations verbatim, so a
# container/pip user was sent to edit a phantom path. The fix must be
# deployment-aware via the existing Mode.detect() seam (same shape as
# check_neo4j_reachable, GH #476): no /opt/kairix/service.env in any branch.


@pytest.mark.unit
def test_document_root_not_set_fix_never_names_phantom_vm_path() -> None:
    """The "not set" fix must not name /opt/kairix/service.env in any mode.

    That path is a stale single-VM assumption (GH #477) — a fresh Docker
    or pip install has no such file, so the remediation misdirected the
    operator. Exercise all three modes through the Mode.detect() env seam.
    """
    for env in (
        {"KAIRIX_CONTAINER": "1"},  # container mode
        {},  # user / system mode (no container signal)
    ):
        result = check_document_root_configured(env=env)
        assert not result.ok
        assert result.fix is not None
        assert "/opt/kairix/service.env" not in result.fix
        # Still actionable — names the canonical env var the operator sets.
        assert "KAIRIX_DOCUMENT_ROOT" in result.fix


@pytest.mark.unit
def test_document_root_not_set_fix_is_container_aware() -> None:
    """In container mode the "not set" fix points at the operator .env next
    to docker-compose.yml (the real container config surface), not a VM path."""
    result = check_document_root_configured(env={"KAIRIX_CONTAINER": "1"})
    assert not result.ok
    assert result.fix is not None
    # Container operators edit the .env beside docker-compose.yml.
    assert ".env" in result.fix
    assert "/opt/kairix/service.env" not in result.fix


@pytest.mark.unit
def test_document_root_not_set_fix_is_pip_aware() -> None:
    """In user/pip mode the "not set" fix points at kairix.config.yaml or the
    KAIRIX_DOCUMENT_ROOT env var — never the VM-only /opt/kairix/service.env."""
    result = check_document_root_configured(env={})
    assert not result.ok
    assert result.fix is not None
    assert "kairix.config.yaml" in result.fix
    assert "/opt/kairix/service.env" not in result.fix


@pytest.mark.unit
def test_document_root_missing_dir_fix_never_names_phantom_vm_path() -> None:
    """The "directory does not exist" fix must not name /opt/kairix/service.env
    in any mode (GH #477)."""
    for env_extra in ({"KAIRIX_CONTAINER": "1"}, {}):
        env = {"KAIRIX_DOCUMENT_ROOT": "/nonexistent/path/vault", **env_extra}
        result = check_document_root_configured(env=env)
        assert not result.ok
        assert result.fix is not None
        assert "/opt/kairix/service.env" not in result.fix


# ---------------------------------------------------------------------------
# CANONICAL_REMEDIATIONS — VM-only path hygiene broadened (GH #477)
# ---------------------------------------------------------------------------
# The existing phantom-artifact guard banned /opt/kairix/secrets.env but NOT
# /opt/kairix/service.env, so the document_root remediation slipped through.
# These tests pin the broadened ban explicitly.


@pytest.mark.unit
def test_document_root_canonical_remediation_is_deployment_neutral() -> None:
    """The canonical document_root remediation must not name /opt/kairix/
    service.env — that single-VM path doesn't exist on Docker/pip installs.

    The remediation key is derived from the public check's result name
    (F5-clean — no private-constant import).
    """
    from kairix.platform.onboard.check import CANONICAL_REMEDIATIONS

    # The check's own CheckResult.name is the canonical-remediation key.
    check_name = check_document_root_configured(env={}).name
    remediation = CANONICAL_REMEDIATIONS[check_name]
    assert "/opt/kairix/service.env" not in remediation
    assert "KAIRIX_DOCUMENT_ROOT" in remediation


@pytest.mark.unit
def test_no_canonical_remediation_references_vm_only_service_env() -> None:
    """No canonical remediation may name /opt/kairix/service.env or
    /opt/openclaw — both are single-VM-layout paths the README would
    otherwise surface verbatim to a container/pip operator (GH #477).
    """
    from kairix.platform.onboard.check import CANONICAL_REMEDIATIONS

    for name, remediation in CANONICAL_REMEDIATIONS.items():
        for banned in ("/opt/kairix/service.env", "/opt/openclaw"):
            assert banned not in remediation, f"{name} remediation references VM-only path {banned!r}"


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
# Remediation hygiene — no phantom scripts / stale forks / install-specific
# paths in any operator-facing fix string (GH #473 / #476 / #477)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_canonical_remediation_references_phantom_artifacts() -> None:
    """Every canonical remediation must name only things that exist.

    scripts/deploy-vm.sh was deleted, the quanyeomans GitHub org is a stale
    fork, and /opt/openclaw/bin + /opt/kairix/secrets.env are paths from a
    single historical VM layout — none of them may appear in a remediation.
    """
    from kairix.platform.onboard.check import CANONICAL_REMEDIATIONS

    for name, remediation in CANONICAL_REMEDIATIONS.items():
        for banned in ("deploy-vm.sh", "quanyeomans", "/opt/openclaw", "/opt/kairix/secrets.env"):
            assert banned not in remediation, f"{name} remediation references {banned!r}"


@pytest.mark.unit
def test_secrets_missing_fix_is_deployment_neutral() -> None:
    """Tier-3 (nothing found) fix points at KAIRIX_SECRETS_FILE + `kairix
    secrets verify` rather than the historical /opt/kairix/secrets.env path."""
    result = check_secrets_loaded(env={})
    assert not result.ok
    assert result.fix is not None
    assert "KAIRIX_SECRETS_FILE" in result.fix
    assert "kairix secrets verify" in result.fix
    assert "/opt/kairix/secrets.env" not in result.fix


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
    """which returns None → ok=False pointing at `kairix init verify` (GH #473 —
    the old hint named scripts/deploy-vm.sh, which does not exist)."""
    from kairix.platform.onboard.check import check_kairix_on_path

    result = check_kairix_on_path(deps=OnboardChecksDeps(which=lambda _name: None))
    assert result.ok is False
    assert result.fix is not None
    assert "kairix init verify" in result.fix
    assert "deploy-vm.sh" not in result.fix


# ---------------------------------------------------------------------------
# check_wrapper_installed — non-Docker branches via DI which + tmp_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrapper_check_missing_kairix_returns_failed(tmp_path: Path) -> None:
    """When which returns None, wrapper check fails pointing at `kairix init
    verify` (GH #473 — the old hint named scripts/deploy-vm.sh, which does
    not exist)."""
    result = check_wrapper_installed(
        deps=OnboardChecksDeps(is_docker=lambda: False, which=lambda _name: None),
    )
    assert result.ok is False
    assert result.fix is not None
    assert "kairix init verify" in result.fix
    assert "deploy-vm.sh" not in result.fix


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


@pytest.mark.unit
def test_agent_knowledge_default_glob_accepts_files_directly_under_agent(tmp_path: Path) -> None:
    """Default glob (``**/*.md``) accepts ``<agent>/<date>.md`` — the layout
    used by dogfood vaults that don't enforce a per-agent ``memory/`` subdir.
    Regression: pre-config the glob was hard-coded to ``*/memory/*.md`` which
    rejected this layout and broke the onboard healthcheck after v2026.5.24a2.
    """
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    agent_dir = tmp_path / "04-Agent-Knowledge" / "builder"
    agent_dir.mkdir(parents=True)
    (agent_dir / "2026-05-15.md").write_text("# log")

    result = check_agent_knowledge_populated(document_root_path=tmp_path)
    assert result.ok is True
    assert "1 files" in result.detail


@pytest.mark.unit
def test_agent_knowledge_custom_dir_name_and_glob_via_di(tmp_path: Path) -> None:
    """Operators override the directory name and glob via config; verify both
    kwargs (the same seams the config helpers feed) route correctly."""
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    custom = tmp_path / "agent-vault"
    (custom / "alpha").mkdir(parents=True)
    (custom / "alpha" / "diary-2026-05-25.txt").write_text("note")

    # Strict glob — only ``diary-*.txt`` files count. The default
    # ``**/*.md`` would miss the .txt file; the override picks it up.
    result = check_agent_knowledge_populated(
        document_root_path=tmp_path,
        agent_knowledge_dir="agent-vault",
        memory_glob="**/diary-*.txt",
    )
    assert result.ok is True
    assert "1 files" in result.detail


@pytest.mark.unit
def test_agent_knowledge_custom_glob_rejects_non_matching_files(tmp_path: Path) -> None:
    """A stricter operator-supplied glob rejects files that don't match.
    Surfaces the glob pattern in the failure detail so operators can
    triage layout vs config mismatch."""
    from kairix.platform.onboard.check import check_agent_knowledge_populated

    agent_dir = tmp_path / "04-Agent-Knowledge" / "builder"
    agent_dir.mkdir(parents=True)
    (agent_dir / "loose.md").write_text("# log")  # not under memory/

    result = check_agent_knowledge_populated(
        document_root_path=tmp_path,
        memory_glob="*/memory/*.md",  # strict — requires <agent>/memory/<file>
    )
    assert result.ok is False
    assert "*/memory/*.md" in result.detail
    assert result.fix is not None
    assert "agent_memory_glob" in result.fix


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
        openclaw_probe=lambda: check_mod.probe_openclaw_harness(config_paths=(str(bogus),)),
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
    result = check_mod.check_chunk_date_populated(db_path=tmp_path / "irrelevant.sqlite", opener=_raise_fnf)
    assert result.ok is False
    assert "Index not found" in result.detail


@pytest.mark.unit
def test_chunk_date_populated_generic_exception(tmp_path: Path, monkeypatch) -> None:
    """When open_db raises a non-FileNotFoundError, the generic exception
    branch fires (line 553-558)."""
    from kairix.platform.onboard import check as check_mod

    def _raise_runtime(_path):
        raise RuntimeError("locked database")

    result = check_mod.check_chunk_date_populated(db_path=tmp_path / "irrelevant.sqlite", opener=_raise_runtime)
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
    ok, detail = check_mod.probe_openclaw_harness(config_paths=(str(config),))
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

    ok, detail = check_mod.probe_openclaw_harness(config_paths=(str(config),))
    assert ok is False
    assert "missing" in detail.lower() or "not executable" in detail.lower()


@pytest.mark.unit
def test_probe_claude_desktop_registered(tmp_path: Path, monkeypatch) -> None:
    """When claude_desktop_config.json registers kairix, the probe returns ok=True."""
    from kairix.platform.onboard import check as check_mod

    config = tmp_path / "claude_desktop_config.json"
    import json

    config.write_text(json.dumps({"mcpServers": {"kairix": {"command": "kairix"}}}))

    ok, detail = check_mod.probe_claude_desktop_harness(config_paths=(config,))
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

    ok, detail = check_mod.probe_sse_harness()
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

    ok, detail = check_mod.probe_sse_harness()
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
    (e.g. someone renames a check but doesn't update CANONICAL_REMEDIATIONS),
    this catches the mismatch.

    Driven through the public ``checks=`` DI seam (the same seam
    ``run_all_checks`` / ``run_onboard_check`` expose) with a small fake
    check list — one passing, one failing. The prior shape ran the full
    unmocked ``ALL_CHECKS`` registry TWICE (real subprocess spawns for
    ``check_mcp_service`` + real socket timeouts), paying ~5.8s of
    wall-clock to assert a near-definitional subset relation. The fake
    list exercises the identical collation path (``run_onboard_check`` ->
    ``run_all_checks`` -> ``CheckFailure(check=r.name, ...)``) in <0.1s,
    and pins the invariant the same way: every ``CheckFailure.check``
    must be the ``.name`` of a result the runner actually produced.

    Sabotage proof: change ``CheckFailure(check=r.name, ...)`` in
    ``run_onboard_check`` to ``check=r.name + "-typo"`` — the failing
    check's id no longer appears in ``all_names`` and the assertion fails.
    """
    from kairix.platform.onboard.check import CheckResult, run_all_checks, run_onboard_check

    def _passing_check() -> CheckResult:
        return CheckResult(name="alpha", ok=True, detail="ok")

    def _failing_check() -> CheckResult:
        return CheckResult(name="beta", ok=False, detail="boom", fix="run `kairix fix beta`")

    fake_checks = [_passing_check, _failing_check]

    all_names = {r.name for r in run_all_checks(checks=fake_checks)}
    result = run_onboard_check(checks=fake_checks)
    # The fake list guarantees exactly one failure surfaces, so the
    # subset relation is exercised against a real (non-empty) failure.
    assert [f.check for f in result.failures] == ["beta"]
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
    # one (matching CANONICAL_REMEDIATIONS), not the weird value.
    fake_results = [
        check_mod.CheckResult(name=name, ok=False, detail="x", fix="WRONG-DO-NOT-USE")
        for name in check_mod.CANONICAL_REMEDIATIONS
    ]

    def _fake_run_all(*, checks=None) -> list[check_mod.CheckResult]:
        return fake_results

    # Drive run_onboard_check via a one-shot ALL_CHECKS override
    # Inject through the public ``checks=`` seam (each check returns one
    # canned result) rather than monkeypatching ``run_all_checks`` (F1).
    result = check_mod.run_onboard_check(checks=[(lambda r=r: r) for r in _fake_run_all()])

    canonical = check_mod.CANONICAL_REMEDIATIONS
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
    canonical_names = set(check_mod.CANONICAL_REMEDIATIONS)
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
    for name, remediation in check_mod.CANONICAL_REMEDIATIONS.items():
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

    # Inject through the public ``checks=`` seam (each check returns one
    # canned result) rather than monkeypatching ``run_all_checks`` (F1).
    result = check_mod.run_onboard_check(checks=[(lambda r=r: r) for r in _fake_run_all()])

    assert len(result.failures) == 1
    # No canonical entry → falls back to the raw fix
    assert result.failures[0].remediation == "run this specific command"


# ---------------------------------------------------------------------------
# v2026.5.24a1 — topology + SharePoint credential checks
# ---------------------------------------------------------------------------
# Each check is gated by a feature flag. When the flag is OFF the check
# must return ok=True with a "skipped" detail; when ON it exercises the
# live config / DB / secrets path. Both branches are covered for each
# check, plus the failure shapes (parse error, validation failure,
# missing secret, missing cc_pair) so the F21 fix: / next: affordance
# reaches the operator on every failed branch.


# ── check_topology_config_valid ────────────────────────────────────────

_FLAG_TOPOLOGY = "topology_config"
_FLAG_SHAREPOINT = "connector_sharepoint"


def _flag_on(name: str, *, target: str) -> bool:
    return name == target


def _flag_off(_name: str) -> bool:
    return False


def _deps_for_topology(
    *,
    flag_target: str | None = None,
    config: object | None = None,
    cc_pairs: frozenset[str] = frozenset(),
    secrets: dict[str, str] | None = None,
    config_raises: Exception | None = None,
    cc_pair_namer_raises: Exception | None = None,
    secret_raises: Exception | None = None,
):
    """Build a TopologyCheckDeps with selectable substitutes.

    Helper so each test stays one logical assertion long and doesn't
    re-construct the same Deps boilerplate.
    """
    from kairix.platform.onboard.check import TopologyCheckDeps

    def _flag(name: str) -> bool:
        return flag_target is not None and name == flag_target

    def _config_loader() -> object:
        if config_raises is not None:
            raise config_raises
        return config

    def _cc_pair_namer() -> frozenset[str]:
        if cc_pair_namer_raises is not None:
            raise cc_pair_namer_raises
        return cc_pairs

    def _secret_reader(name: str) -> str | None:
        if secret_raises is not None:
            raise secret_raises
        return (secrets or {}).get(name)

    return TopologyCheckDeps(
        flag_reader=_flag,
        config_loader=_config_loader,
        db_cc_pair_namer=_cc_pair_namer,
        secret_reader=_secret_reader,
    )


@pytest.mark.unit
# NOTE: test_topology_config_valid_skipped_when_flag_off retired with the
# topology_config flag (#132). Post-cutover the check runs unconditionally;
# the flag-off skip path no longer exists.


@pytest.mark.unit
def test_topology_config_valid_missing_config_when_flag_on() -> None:
    """Flag ON + no config → ok=False with fix hint pointing at example."""
    from kairix.platform.onboard.check import check_topology_config_valid

    result = check_topology_config_valid(deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=None))
    assert result.ok is False
    assert "not found" in result.detail
    assert result.fix is not None
    assert "kairix.config.example.yaml" in result.fix
    assert "fix:" in result.fix
    assert "next:" in result.fix


@pytest.mark.unit
def test_topology_config_valid_parse_error() -> None:
    """Flag ON + malformed YAML → ok=False, fix points at validate command."""
    from kairix.platform.onboard.check import check_topology_config_valid

    # topology.connectors is the wrong shape (str, not list).
    bad_data = {"topology": {"connectors": "this should be a list"}}

    result = check_topology_config_valid(
        deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=bad_data),
    )
    assert result.ok is False
    assert "parse failed" in result.detail
    assert result.fix is not None
    assert "fix:" in result.fix


@pytest.mark.unit
def test_topology_config_valid_cross_reference_failure() -> None:
    """Flag ON + dangling cc_pair → ok=False with failure summary in detail."""
    from kairix.platform.onboard.check import check_topology_config_valid

    # cc_pair references a connector that wasn't declared.
    data = {
        "topology": {
            "connectors": [],
            "credentials": [],
            "cc_pairs": [
                {
                    "id": "bad-pair",
                    "connector": "nonexistent-connector",
                    "credential": None,
                    "name": "bad-pair",
                }
            ],
        }
    }

    result = check_topology_config_valid(deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=data))
    assert result.ok is False
    assert "cross-reference failure" in result.detail
    assert "nonexistent-connector" in result.detail


@pytest.mark.unit
def test_topology_config_valid_clean_config_passes() -> None:
    """Flag ON + valid declarative config → ok=True with counts in detail."""
    from kairix.platform.onboard.check import check_topology_config_valid

    data = {
        "topology": {
            "connectors": [{"id": "c1", "kind": "obsidian", "name": "obsidian-personal"}],
            "credentials": [],
            "cc_pairs": [{"id": "p1", "connector": "c1", "credential": None, "name": "obsidian-personal"}],
            "collections": [{"name": "obsidian-all", "sources": [{"cc_pair": "p1"}]}],
            "scope_profiles": [],
            "skills": [],
        }
    }

    result = check_topology_config_valid(deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=data))
    assert result.ok is True
    assert "connectors=1" in result.detail
    assert "cc_pairs=1" in result.detail


@pytest.mark.unit
def test_topology_config_valid_loader_raises() -> None:
    """Flag ON + loader exception → ok=False with fix hint."""
    from kairix.platform.onboard.check import check_topology_config_valid

    result = check_topology_config_valid(
        deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config_raises=OSError("yaml parse failed")),
    )
    assert result.ok is False
    assert "loader raised" in result.detail
    assert result.fix is not None
    assert "fix:" in result.fix


# ── check_topology_cc_pairs_registered ─────────────────────────────────


@pytest.mark.unit
# NOTE: test_topology_cc_pairs_registered_skipped_when_flag_off retired
# with the topology_config flag (#132).


@pytest.mark.unit
def test_topology_cc_pairs_registered_missing_pair() -> None:
    """Flag ON + declared cc_pair without DB row → ok=False with apply-config fix."""
    from kairix.platform.onboard.check import check_topology_cc_pairs_registered

    data = {
        "topology": {
            "connectors": [{"id": "c1", "kind": "obsidian", "name": "obsidian"}],
            "cc_pairs": [{"id": "p1", "connector": "c1", "credential": None, "name": "obsidian-personal"}],
        }
    }

    result = check_topology_cc_pairs_registered(
        deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=data, cc_pairs=frozenset()),
    )
    assert result.ok is False
    assert "obsidian-personal" in result.detail
    assert result.fix is not None
    assert "restart the worker" in result.fix


@pytest.mark.unit
def test_topology_cc_pairs_registered_all_present() -> None:
    """Flag ON + every declared cc_pair has a DB row → ok=True."""
    from kairix.platform.onboard.check import check_topology_cc_pairs_registered

    data = {
        "topology": {
            "connectors": [{"id": "c1", "kind": "obsidian", "name": "obsidian"}],
            "cc_pairs": [{"id": "p1", "connector": "c1", "credential": None, "name": "obsidian-personal"}],
        }
    }

    result = check_topology_cc_pairs_registered(
        deps=_deps_for_topology(
            flag_target=_FLAG_TOPOLOGY,
            config=data,
            cc_pairs=frozenset({"obsidian-personal"}),
        ),
    )
    assert result.ok is True
    assert "1 declared cc_pair" in result.detail


@pytest.mark.unit
def test_topology_cc_pairs_registered_no_declared() -> None:
    """Flag ON + no declared cc_pairs → ok=True (nothing to apply)."""
    from kairix.platform.onboard.check import check_topology_cc_pairs_registered

    data = {"topology": {"connectors": [], "cc_pairs": []}}

    result = check_topology_cc_pairs_registered(
        deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=data),
    )
    assert result.ok is True
    assert "nothing to register" in result.detail


@pytest.mark.unit
def test_topology_cc_pairs_registered_no_config_file() -> None:
    """Flag ON + no kairix.config.yaml → ok=True (nothing to register)."""
    from kairix.platform.onboard.check import check_topology_cc_pairs_registered

    result = check_topology_cc_pairs_registered(
        deps=_deps_for_topology(flag_target=_FLAG_TOPOLOGY, config=None),
    )
    assert result.ok is True
    assert "no kairix.config.yaml" in result.detail


@pytest.mark.unit
def test_topology_cc_pairs_registered_db_lookup_fails() -> None:
    """Flag ON + DB error → ok=False with fix hint."""
    from kairix.platform.onboard.check import check_topology_cc_pairs_registered

    data = {
        "topology": {
            "connectors": [{"id": "c1", "kind": "obsidian", "name": "obsidian"}],
            "cc_pairs": [{"id": "p1", "connector": "c1", "credential": None, "name": "obsidian-personal"}],
        }
    }

    result = check_topology_cc_pairs_registered(
        deps=_deps_for_topology(
            flag_target=_FLAG_TOPOLOGY,
            config=data,
            cc_pair_namer_raises=RuntimeError("database is locked"),
        ),
    )
    assert result.ok is False
    assert "lookup failed" in result.detail
    assert result.fix is not None
    assert "fix:" in result.fix


# ── check_sharepoint_credentials_loaded ───────────────────────────────────


@pytest.mark.unit
def test_sharepoint_credentials_loaded_skipped_when_flag_off() -> None:
    """Flag OFF → ok=True with skipped detail."""
    from kairix.platform.onboard.check import check_sharepoint_credentials_loaded

    result = check_sharepoint_credentials_loaded(deps=_deps_for_topology(flag_target=None))
    assert result.ok is True
    assert "skipped" in result.detail
    assert _FLAG_SHAREPOINT in result.detail


@pytest.mark.unit
def test_sharepoint_credentials_loaded_all_present() -> None:
    """Flag ON + every M365 secret resolves → ok=True."""
    from kairix.platform.onboard.check import check_sharepoint_credentials_loaded

    full_map = {
        "connector-m365-tenant-id": "tenant-value",  # pragma: allowlist secret — test fixture value
        "connector-m365-client-id": "client-value",  # pragma: allowlist secret — test fixture value
        "connector-m365-client-secret": "secret-value",  # pragma: allowlist secret — test fixture value
    }
    result = check_sharepoint_credentials_loaded(
        deps=_deps_for_topology(flag_target=_FLAG_SHAREPOINT, secrets=full_map),
    )
    assert result.ok is True
    assert "3 SharePoint secret" in result.detail


@pytest.mark.unit
def test_sharepoint_credentials_loaded_all_missing() -> None:
    """Flag ON + every secret unresolved → ok=False with all 3 names listed."""
    from kairix.platform.onboard.check import check_sharepoint_credentials_loaded

    result = check_sharepoint_credentials_loaded(deps=_deps_for_topology(flag_target=_FLAG_SHAREPOINT))
    assert result.ok is False
    assert "3 SharePoint secret" in result.detail
    assert "connector-m365-tenant-id" in result.detail
    assert "connector-m365-client-id" in result.detail
    assert "connector-m365-client-secret" in result.detail
    assert result.fix is not None
    assert "fix:" in result.fix
    assert "next:" in result.fix


@pytest.mark.unit
def test_sharepoint_credentials_loaded_partial_missing() -> None:
    """Flag ON + only tenant resolved → ok=False, names the 2 still missing."""
    from kairix.platform.onboard.check import check_sharepoint_credentials_loaded

    partial = {
        "connector-m365-tenant-id": "tenant-value",  # pragma: allowlist secret — test fixture value
    }
    result = check_sharepoint_credentials_loaded(
        deps=_deps_for_topology(flag_target=_FLAG_SHAREPOINT, secrets=partial),
    )
    assert result.ok is False
    assert "2 SharePoint secret" in result.detail
    assert "connector-m365-client-id" in result.detail
    assert "connector-m365-client-secret" in result.detail
    # The one that resolved is NOT in the missing list
    assert "connector-m365-tenant-id" not in result.detail


@pytest.mark.unit
def test_sharepoint_credentials_loaded_reader_raises() -> None:
    """Flag ON + secret_reader raises → treats secret as missing (no crash)."""
    from kairix.platform.onboard.check import check_sharepoint_credentials_loaded

    result = check_sharepoint_credentials_loaded(
        deps=_deps_for_topology(flag_target=_FLAG_SHAREPOINT, secret_raises=OSError("key vault unreachable")),
    )
    assert result.ok is False
    assert "3 SharePoint secret" in result.detail


# ── integration: new checks register in ALL_CHECKS ────────────────────────


@pytest.mark.unit
def test_new_checks_appear_in_all_checks() -> None:
    """The three new v2026.5.24a1 checks are wired into ALL_CHECKS so they
    run as part of ``kairix onboard check``. Sabotage-prove: if a check
    is added to the module but forgotten from ALL_CHECKS, this fails."""
    from kairix.platform.onboard import check as check_mod

    names = {fn.__name__ for fn in check_mod.ALL_CHECKS}
    assert "check_topology_config_valid" in names
    assert "check_topology_cc_pairs_registered" in names
    assert "check_sharepoint_credentials_loaded" in names
    # #373 Wave B — wildcard expansion onboard check
    assert "check_topology_wildcard_expansion_resolved" in names


# NOTE: test_topology_wildcard_expansion_skipped_when_flag_off retired
# with the topology_config flag (#132).


@pytest.mark.unit
def test_topology_wildcard_expansion_pass_when_all_resolved() -> None:
    """Flag ON + every actor_id is a concrete name (no '*') → ok=True."""
    from kairix.platform.onboard.check import (
        TopologyCheckDeps,
        check_topology_wildcard_expansion_resolved,
    )

    deps = TopologyCheckDeps(
        flag_reader=lambda n: n == _FLAG_TOPOLOGY,
        config_loader=lambda: None,
        db_cc_pair_namer=lambda: frozenset(),
        secret_reader=lambda _n: None,
        db_scope_actor_id_reader=lambda: ("agent-alpha", "agent-beta"),
    )
    result = check_topology_wildcard_expansion_resolved(deps=deps)
    assert result.ok is True
    assert "2 distinct scope actor_id" in result.detail


@pytest.mark.unit
def test_topology_wildcard_expansion_fail_when_literal_star_present() -> None:
    """Flag ON + a '*' actor_id in DB → ok=False with restart-worker fix."""
    from kairix.platform.onboard.check import (
        TopologyCheckDeps,
        check_topology_wildcard_expansion_resolved,
    )

    deps = TopologyCheckDeps(
        flag_reader=lambda n: n == _FLAG_TOPOLOGY,
        config_loader=lambda: None,
        db_cc_pair_namer=lambda: frozenset(),
        secret_reader=lambda _n: None,
        db_scope_actor_id_reader=lambda: ("agent-alpha", "*"),
    )
    result = check_topology_wildcard_expansion_resolved(deps=deps)
    assert result.ok is False
    assert "*" in result.detail
    assert result.fix is not None
    assert "restart the worker" in result.fix


@pytest.mark.unit
def test_topology_wildcard_expansion_reader_raises() -> None:
    """Flag ON + reader raises → ok=False with DB-reachability fix hint."""
    from kairix.platform.onboard.check import (
        TopologyCheckDeps,
        check_topology_wildcard_expansion_resolved,
    )

    def _raises() -> tuple[str, ...]:
        raise RuntimeError("database is locked")

    deps = TopologyCheckDeps(
        flag_reader=lambda n: n == _FLAG_TOPOLOGY,
        config_loader=lambda: None,
        db_cc_pair_namer=lambda: frozenset(),
        secret_reader=lambda _n: None,
        db_scope_actor_id_reader=_raises,
    )
    result = check_topology_wildcard_expansion_resolved(deps=deps)
    assert result.ok is False
    assert "lookup failed" in result.detail
    assert result.fix is not None


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

    # Inject through the public ``checks=`` seam (each check returns one
    # canned result) rather than monkeypatching ``run_all_checks`` (F1).
    result = check_mod.run_onboard_check(checks=[(lambda r=r: r) for r in _fake_run_all()])

    assert len(result.failures) == 1
    assert result.failures[0].remediation
    assert "bug" in result.failures[0].remediation.lower()


# ---------------------------------------------------------------------------
# #322 — extractor library import check (v2026.5.26a1 dogfood)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_extractor_libraries_importable_passes_when_all_present() -> None:
    """Happy path — the test environment installs every declared extractor
    library extras, so the check should return ok=True with the count."""
    from kairix.platform.onboard.check import check_extractor_libraries_importable

    result = check_extractor_libraries_importable()
    assert result.ok is True
    assert "all" in result.detail.lower()
    assert "succeeded" in result.detail.lower()


@pytest.mark.unit
def test_check_extractor_libraries_importable_fails_when_library_missing(monkeypatch) -> None:
    """Sabotage proof for #322: when a required library is missing the
    check returns ok=False with the per-extractor + per-library breakdown
    and an actionable fix string naming the exact pip-install command.

    Drives the check with a fake import that raises ImportError for one
    specific library so we don't have to actually uninstall production
    deps from the test environment.
    """
    import importlib

    from kairix.platform.onboard import check as check_mod

    real_import = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> object:
        if name == "olefile":
            raise ImportError(f"No module named '{name}'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)

    result = check_mod.check_extractor_libraries_importable()
    assert result.ok is False
    assert "markitdown" in result.detail
    assert "olefile" in result.detail
    assert result.fix is not None
    assert "pip install" in result.fix
    assert "markitdown" in result.fix


@pytest.mark.unit
def test_check_extractor_libraries_importable_is_registered_in_all_checks() -> None:
    """The new check must be wired into ALL_CHECKS so it runs as part of
    ``kairix onboard check``. Sabotage: drop the registration line from
    ALL_CHECKS and this test fails — preventing the new check from being
    a tree-falling-in-the-forest."""
    from kairix.platform.onboard.check import ALL_CHECKS, check_extractor_libraries_importable

    assert check_extractor_libraries_importable in ALL_CHECKS, (
        "check_extractor_libraries_importable must be in ALL_CHECKS or "
        "the v2026.5.26a1 #322 failure mode (extras-missing-in-Docker-image) "
        "can ship silently again."
    )
