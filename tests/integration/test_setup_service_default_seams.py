"""F86: the setup wizard's production DI-default seams actually execute.

``SetupServiceDeps`` (and the source-OAuth / onboarding-agent helpers)
bind ``_default_*`` lazy-import delegations a real operator runs when
nothing is injected. Every other setup test injects fakes for exactly
those seams, so — before F86 — none of the production defaults was ever
run (the escape-4 shape: a default that crashes behind a green suite).

Each test here leaves the seam UNINJECTED — a bare ``SetupServiceDeps()``
default binding, ``build_setup_service(deps=None)``, or the public
caller with its default kwarg — and asserts on what the real
implementation returned. Side effects are confined to ``tmp_path``:
the ``isolated_platform`` fixture points ``HOME`` / ``XDG_*`` (not
``KAIRIX_*`` — F2-clean) and the cwd at per-test temp dirs, so config
writes, index files and secrets-bundle probes never touch the session's
shared hermetic dirs or an operator's real install. Seams that would
persist a real secret are driven down their deterministic no-write path
(all-empty values / a non-canonical name).

Sabotage-proofs (executed, one per seam family; each restored):
  * ``_default_write_config`` → pointed ``overlay_path`` at a different
    file than the config-target seam resolves —
    ``test_default_config_write_read_round_trip`` failed (written path !=
    the reported config target).
  * ``_default_run_prep`` → returned ``None`` instead of delegating —
    ``test_default_prep_seam_runs_the_prep_use_case`` failed.
  * ``_default_tools_count`` → returned ``0`` — the handshake test
    failed (``tools_count`` 0).
  * ``_default_search_pipeline`` → returned ``object()`` instead of
    building through the factory —
    ``test_default_search_pipeline_seam_requires_a_configured_provider``
    failed (DID NOT RAISE).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from kairix.connect.protocols import CapturedTokens, ClientCredentials
from kairix.core.search.config_loader import reset_config_cache
from kairix.paths import KairixPaths, clear_cache
from kairix.platform.setup.agent import recommend_from_profile
from kairix.platform.setup.backends import SetupServiceDeps, run_first_index
from kairix.platform.setup.service import build_setup_service
from kairix.platform.setup.source_oauth import discover_source_units_live
from kairix.secrets.loader import SecretNotFoundError
from kairix.secrets.probe import llm_credentials_available
from kairix.use_cases.brief import BriefOutput
from kairix.use_cases.prep import PrepOutput
from kairix.use_cases.remember import RememberResult
from kairix.use_cases.timeline import TimelineResult
from tests.fakes import FakePaths

pytestmark = pytest.mark.integration

# Operator overrides that would route a default seam at a REAL install
# (config file / secrets bundle) instead of this test's tmp dirs. CI never
# sets them; the guard only matters on a developer shell that exports one.
_OPERATOR_OVERRIDES = (
    "KAIRIX_CONFIG_PATH",
    "KAIRIX_CONFIG_OVERLAY_PATH",
    "KAIRIX_CONFIG_BASE_PATH",
    "KAIRIX_SECRETS_FILE",
    "KAIRIX_PROVIDER_LLM_API_KEY",
)


@pytest.fixture
def isolated_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every platform-default location at per-test temp dirs."""
    leaked = [name for name in _OPERATOR_OVERRIDES if os.environ.get(name)]
    if leaked:
        # Skip rationale: a developer shell exported an operator override
        # (never set in CI, where these seams are enforced); running would
        # write wizard config / read secrets at the operator's real install.
        pytest.skip(f"operator override exported in this shell: {', '.join(leaked)}")
    home, xdg_data, xdg_cache, xdg_config = (tmp_path / n for n in ("home", "xdg-data", "xdg-cache", "xdg-config"))
    for directory in (home / ".local" / "share" / "kairix", xdg_data / "kairix", xdg_cache / "kairix", xdg_config):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.chdir(tmp_path)
    clear_cache()
    reset_config_cache()
    yield tmp_path
    clear_cache()
    reset_config_cache()


def _fake_paths(root: Path) -> KairixPaths:
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    return FakePaths(
        document_root=docs,
        db_path=root / "index.sqlite",
        log_dir=root / "logs",
        workspace_root=root / "workspaces",
    )


# ── config write / target / read (the wizard's read-modify-write cycle) ──


def test_default_config_write_read_round_trip(isolated_platform: Path) -> None:
    deps = SetupServiceDeps()

    target = deps.config_target_fn()
    written = deps.write_config_fn({"paths": {"document_root": "/data/文档"}, "provider": "openai"})

    assert written == target
    assert target == isolated_platform / "xdg-config" / "kairix" / "kairix.config.yaml"
    assert deps.read_config_fn() == {"paths": {"document_root": "/data/文档"}, "provider": "openai"}


@pytest.mark.usefixtures("isolated_platform")
def test_default_read_config_on_fresh_install_is_empty() -> None:
    assert SetupServiceDeps().read_config_fn() == {}


# ── status probes ─────────────────────────────────────────────────────────


def test_default_status_seams_reflect_the_platform(isolated_platform: Path) -> None:
    service = build_setup_service(paths=_fake_paths(isolated_platform))

    status = service.status()

    assert status.provider_done is llm_credentials_available()
    # The session-wide hermetic KAIRIX_DOCUMENT_ROOT is an explicit
    # override, so the default document-root seam reports it.
    expected_root = Path(os.environ["KAIRIX_DOCUMENT_ROOT"]).expanduser()
    assert SetupServiceDeps().configured_document_root_fn() == expected_root
    assert status.source_done is expected_root.is_dir()
    assert status.index_done is False


# ── credential + secret persistence (no-write paths) ─────────────────────


def test_default_persist_credentials_with_nothing_to_store_writes_nothing(isolated_platform: Path) -> None:
    assert SetupServiceDeps().persist_credentials_fn("", "", "") is None
    assert not (isolated_platform / "xdg-config" / "kairix" / "secrets").exists()


def test_default_persist_secret_rejects_non_canonical_name(isolated_platform: Path) -> None:
    with pytest.raises(ValueError, match="fix:"):
        SetupServiceDeps().persist_secret_fn("not-a-canonical-name", "value")
    assert not (isolated_platform / "xdg-config" / "kairix" / "secrets").exists()


# ── search / handshake ────────────────────────────────────────────────────


def test_default_search_pipeline_seam_requires_a_configured_provider(isolated_platform: Path) -> None:
    """On a fresh install (no ``provider:``) the factory fails loud with an F21 affordance."""
    paths = _fake_paths(isolated_platform)

    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        SetupServiceDeps().search_pipeline_factory(paths)
    # The wizard's first-search card degrades to its empty state instead.
    assert build_setup_service(paths=paths).first_search("kickoff notes").results == ()


def test_default_handshake_seams_build_the_real_mcp_server(isolated_platform: Path) -> None:
    handshake = build_setup_service(paths=_fake_paths(isolated_platform)).verify_agent_handshake()

    # tools_count > 0 proves BOTH defaults ran: the capability probe is
    # called first, and any exception would zero the count.
    assert handshake.tools_count > 0
    if not handshake.ok:
        assert "provider credentials are not loaded" in (handshake.error or "")


# ── capability-tour use cases ─────────────────────────────────────────────


@pytest.mark.usefixtures("isolated_platform")
def test_default_prep_seam_runs_the_prep_use_case() -> None:
    out = SetupServiceDeps().prep_fn("project kickoff")

    # Fresh install: retrieval can't build without a provider, so the use
    # case returns its structured error envelope (never raises).
    assert isinstance(out, PrepOutput)
    assert out.summary == ""
    assert "provider" in out.error


@pytest.mark.usefixtures("isolated_platform")
def test_default_remember_seam_runs_the_remember_use_case() -> None:
    result = SetupServiceDeps().remember_fn("agent-alpha", "   ")

    assert isinstance(result, RememberResult)
    assert (result.error or "").startswith("EmptyContent")


@pytest.mark.usefixtures("isolated_platform")
def test_default_brief_seam_runs_the_brief_use_case() -> None:
    out = SetupServiceDeps().brief_fn("")

    assert isinstance(out, BriefOutput)
    assert (out.error or "").startswith("InvalidAgent")


@pytest.mark.usefixtures("isolated_platform")
def test_default_timeline_seam_runs_the_timeline_use_case() -> None:
    out = SetupServiceDeps().timeline_fn("what happened last week")

    assert isinstance(out, TimelineResult)
    assert list(out.results) == []


# ── first index ───────────────────────────────────────────────────────────


@pytest.mark.usefixtures("isolated_platform")
def test_default_embed_pipeline_seam_requires_llm_credentials() -> None:
    # run_first_index() with no pipeline_fn binds the production embed
    # pipeline; it scans + opens the (isolated) index, then the embed
    # stage fails loud naming the missing credential and its fix.
    with pytest.raises(SecretNotFoundError, match="kairix-provider-llm-api-key"):
        run_first_index()


# ── source OAuth client factories ─────────────────────────────────────────


def test_default_slack_client_seam_rejects_an_empty_bot_token() -> None:
    tokens = CapturedTokens(refresh_token="", access_token="", token_uri="", bot_token="")
    with pytest.raises(ValueError, match="non-empty workspace bot token"):
        discover_source_units_live("slack", ClientCredentials(client_id="cid", client_secret="cs"), tokens)


def test_default_github_client_seam_fails_closed_on_a_bad_private_key() -> None:
    tokens = CapturedTokens(refresh_token="", access_token="", token_uri="", metadata={"installation-id": "70000"})
    client = ClientCredentials(client_id="42", client_secret="not-a-pem-key")
    with pytest.raises(Exception, match="refresh failed"):
        discover_source_units_live("github", client, tokens)


# ── onboarding agent ──────────────────────────────────────────────────────


@pytest.mark.usefixtures("isolated_platform")
def test_default_onboarding_chat_seam_degrades_to_rule_based_advice() -> None:
    rec = recommend_from_profile(
        total_docs=10,
        format_counts={"md": 10},
        date_file_pct=0.5,
        procedural_pct=0.0,
        entity_pct=0.0,
        api_key="fake-key-for-tests",  # pragma: allowlist secret — fixture value, not a real key
        endpoint="https://example.test",
    )

    assert rec is not None
    assert rec["temporal_boost"] is True
    # No provider is configured on the isolated platform, so the default
    # chat seam raises inside and the LLM leg is skipped — never a crash.
    assert "llm_advice" not in rec
