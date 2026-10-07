"""Tests for the terminal setup wizard — the second frontend over SetupService.

The wizard rides the SAME :class:`SetupService` backend the web setup
wizard drives (#review-H3/#review-M6); the primary injection seam is
``WizardDeps(setup_service=lambda: FakeSetupService(...))`` — the
canonical Protocol-compliant fake from ``tests/fakes.py``.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fakes import FakeSetupService

# Deterministic provider menu for scripted interactive runs — index "1"
# is always azure_foundry, "2" openai, "3" litellm_proxy.
_MENU = ("azure_foundry", "openai", "litellm_proxy")


def _deps(tmp_path: Path, service: Any = None, persist: Any = None) -> Any:
    """WizardDeps wired to the canonical fake service + a tmp persistence sink."""
    from kairix.platform.setup.wizard import WizardDeps

    return WizardDeps(
        setup_service=lambda: service if service is not None else FakeSetupService(),
        persist_credentials=persist or (lambda *_a: tmp_path / "unwritten-kairix.env"),
        provider_names=lambda: _MENU,
        index_poll_seconds=0.0,
    )


@pytest.mark.unit
def test_load_template_consulting() -> None:
    from kairix.platform.setup.wizard import load_template

    template = load_template("consulting")
    assert template["name"] == "consulting"
    assert "retrieval" in template


@pytest.mark.unit
def test_load_template_missing_returns_empty() -> None:
    from kairix.platform.setup.wizard import load_template

    template = load_template("nonexistent")
    assert template == {}


@pytest.mark.unit
def test_docker_compose_valid_yaml() -> None:
    """Verify docker-compose.yml is valid YAML."""
    compose_path = Path(__file__).parent.parent.parent / "docker-compose.yml"
    if compose_path.exists():
        with open(compose_path) as f:
            data = yaml.safe_load(f)
        assert "services" in data
        assert "kairix" in data["services"]
        assert "neo4j" in data["services"]


@pytest.mark.unit
def test_wizard_rejects_nonexistent_document_root(tmp_path: Path) -> None:
    """Setup wizard fails cleanly when the document root doesn't exist.

    Uses the REAL backend scan (through ``build_setup_service``) so the
    rejection words match what the web wizard shows for the same path.
    The run stops before any network-touching step (the scan is step 2;
    validation runs only after a successful scan + config write).
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.service import build_setup_service
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"
    nonexistent = str(tmp_path / "does-not-exist")

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=nonexistent,
        preset="general",
        deps=WizardDeps(setup_service=lambda: build_setup_service()),
    )

    assert result is False, "Wizard should reject a non-existent document root"
    assert not output.exists(), "Config file should not be written for invalid document root"


@pytest.mark.unit
def test_wizard_rejects_relative_document_root(tmp_path: Path) -> None:
    """A relative ``--path`` is rejected with the backend's guidance —
    the old wizard silently resolved it against the server's working
    directory (M6 degraded behaviour)."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.service import build_setup_service
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"
    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path="relative/docs",
        preset="general",
        deps=WizardDeps(setup_service=lambda: build_setup_service()),
    )
    assert result is False
    assert not output.exists()


@pytest.mark.unit
def test_wizard_accepts_valid_document_root(tmp_path: Path) -> None:
    """Setup wizard accepts a valid existing document root."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "test-config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path),
    )

    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_run_setup_generates_config(tmp_path: Path, monkeypatch) -> None:
    """run_setup writes a valid YAML config file through the full
    interactive flow (builtins.input scripted — prompts.py is not a
    kairix-internal seam)."""
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "test-config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    inputs = iter(
        [
            "1",  # Step 0: use case
            "1",  # Step 1: azure_foundry
            "https://test.services.ai.azure.com",  # endpoint
            "test-key",  # API key
            "",  # embed model (default)
            "",  # chat model (default)
            str(doc_dir),  # Step 2: document path
            "1",  # Step 3: default storage
            "n",  # Step 4: skip knowledge graph
            "1",  # Step 6: search everything
            "n",  # don't index now
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs, ""))

    from kairix.platform.setup.prompts import SetupContext

    ctx = SetupContext(interactive=True, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(output_path=str(output), ctx=ctx, deps=_deps(tmp_path))

    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "retrieval" in config
    assert isinstance(config["retrieval"], dict)


@pytest.mark.unit
def test_wizard_deps_default_factory_binds_callable() -> None:
    """``WizardDeps()`` with no overrides binds a real callable for the
    service factory, not ``None``.

    Sabotage proof: regressing to ``Optional[Callable] = None`` without
    a ``__post_init__`` wire-up would leave this None at runtime and
    break ``run_setup`` on its first line.
    """
    from kairix.platform.setup.wizard import WizardDeps

    deps = WizardDeps()
    assert callable(deps.setup_service), (
        f"default_factory must bind a callable; got {deps.setup_service!r}. "
        "Regressing to ``setup_service: Callable | None = None`` without a "
        "post-init wire-up would leave this None and break run_setup."
    )
    assert callable(deps.persist_credentials)
    assert callable(deps.provider_names)
    assert callable(deps.write_config)


@pytest.mark.unit
def test_wizard_routes_validation_through_injected_service(tmp_path: Path) -> None:
    """``run_setup(deps=WizardDeps(setup_service=...))`` drives the
    injected service's provider validation exactly once.

    Sabotage proof: the fake records every call. If the wizard ignored
    ``deps`` and built its own production service, no calls would be
    recorded and this test would fail.
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    service = FakeSetupService()
    output = tmp_path / "test-config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path, service=service),
    )

    assert len(service.validate_calls) == 1, f"expected exactly one validation; got {service.validate_calls}"
    # Non-interactive default pick is the azure_foundry plugin.
    assert service.validate_calls[0][0] == "azure_foundry"


@pytest.mark.unit
def test_wizard_preset_consulting_produces_consulting_collections(tmp_path: Path) -> None:
    """preset='consulting' wires the consulting collection template."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="consulting",
        deps=_deps(tmp_path),
    )
    assert result is True
    assert output.exists()
    config = yaml.safe_load(output.read_text())
    assert "retrieval" in config


@pytest.mark.unit
def test_wizard_preset_technical_produces_technical_retrieval(tmp_path: Path) -> None:
    """preset='technical' uses the technical retrieval template."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="technical",
        deps=_deps(tmp_path),
    )
    assert result is True
    config = yaml.safe_load(output.read_text())
    assert "retrieval" in config


@pytest.mark.unit
def test_wizard_preset_daily_log_aliased_to_general(tmp_path: Path) -> None:
    """preset='daily-log' is aliased to 'general'."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="daily-log",
        deps=_deps(tmp_path),
    )
    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_wizard_connection_test_failure_returns_true_non_interactive(tmp_path: Path) -> None:
    """When validation fails in non-interactive mode, run_setup continues
    so a config is still emitted (scripted-bootstrap contract)."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path, service=FakeSetupService(validate_ok=False)),
    )
    # Non-interactive mode: continues despite failure (continue_default=True)
    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_wizard_validation_failure_prints_backend_guidance(tmp_path: Path, capsys) -> None:
    """The terminal prints the backend's validation error VERBATIM —
    including the Azure deployment-name guidance (#484) that used to be
    web-only (M6 parity)."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    service = FakeSetupService(validate_deployment_missing=True)
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    run_setup(
        output_path=str(tmp_path / "config.yaml"),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path, service=service),
    )
    out = capsys.readouterr().out
    assert "Your key works" in out, f"deployment guidance missing from terminal output:\n{out}"
    assert "no deployment named" in out


@pytest.mark.unit
def test_wizard_json_mode_emits_config_to_stdout(tmp_path: Path, capsys) -> None:
    """json_mode=True emits the config as JSON to stdout and returns True."""
    import json

    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=True, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(tmp_path / "config.yaml"),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path),
    )

    assert result is True
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert "paths" in parsed
    assert "retrieval" in parsed


# ---------------------------------------------------------------------------
# Interactive-flow helper
# ---------------------------------------------------------------------------


def _interactive_run_setup(
    tmp_path: Path,
    monkeypatch,
    *,
    inputs: list[str],
    service: Any = None,
    persist: Any = None,
) -> tuple[bool, Path]:
    """Helper: run wizard interactively with a fixed input sequence.

    ``service`` (when not ``None``) is the FakeSetupService instance the
    wizard drives; ``persist_credentials`` is always faked here because
    the interactive sequences supply non-empty API keys and the
    production default would otherwise write to the developer's real
    secrets bundle.
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir(exist_ok=True)
    (doc_dir / "note.md").write_text("# note", encoding="utf-8")

    input_iter = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(input_iter, ""))

    ctx = SetupContext(interactive=True, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        deps=_deps(tmp_path, service=service, persist=persist),
    )
    return result, output


# Interactive answer script (menu choice "1" = azure_foundry in _MENU):
#   use-case, provider, endpoint, api key, embed model, chat model,
#   doc path, storage, neo4j?, collections, index?
def _script(tmp_path: Path, **overrides: str) -> list[str]:
    answers = {
        "use_case": "1",
        "provider": "1",
        "endpoint": "https://x.services.ai.azure.com",
        "api_key": "k",
        "embed_model": "",
        "chat_model": "",
        "doc_path": str(tmp_path / "docs"),
        "storage": "1",
        "neo4j": "n",
        "collections": "1",
        "index": "n",
    }
    answers.update(overrides)
    return list(answers.values())


@pytest.mark.unit
def test_wizard_interactive_openai_provider(tmp_path: Path, monkeypatch) -> None:
    """Menu pick 2 → the openai plugin; blank endpoint resolves to the
    OpenAI default base URL before persistence."""
    recorded: list[tuple[str, ...]] = []

    def _persist(*args: str) -> Path:
        recorded.append(args)
        return tmp_path / "kairix.env"

    inputs = _script(tmp_path, provider="2", endpoint="", api_key="openai-key")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, persist=_persist)
    assert result is True
    assert output.exists()
    assert recorded[0][1] == "https://api.openai.com/v1"
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config["provider"] == "openai"


@pytest.mark.unit
def test_wizard_interactive_third_party_provider(tmp_path: Path, monkeypatch) -> None:
    """Menu pick 3 → a non-azure registry plugin passes through verbatim."""
    inputs = _script(tmp_path, provider="3", endpoint="https://proxy.example.invalid/v1", api_key="proxy-key")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config["provider"] == "litellm_proxy"


@pytest.mark.unit
def test_wizard_interactive_legacy_azure_endpoint_remaps_plugin(tmp_path: Path, monkeypatch) -> None:
    """An azure_foundry pick with a legacy-shaped endpoint rides the
    azure_legacy plugin — the same remap the web wizard backend applies."""
    inputs = _script(tmp_path, endpoint="https://res.openai.azure.com")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config["provider"] == "azure_legacy"


@pytest.mark.unit
def test_wizard_interactive_custom_storage_path(tmp_path: Path, monkeypatch) -> None:
    """storage choice 2 → custom data-directory path."""
    custom_store = tmp_path / "my-store"
    inputs = _script(tmp_path, storage="2")
    # The custom-storage branch asks one extra question (the path) right
    # after the storage choice.
    inputs.insert(inputs.index("2", 6) + 1, str(custom_store))
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config["paths"]["db_path"].startswith(str(custom_store))


@pytest.mark.unit
def test_wizard_interactive_fhs_storage(tmp_path: Path, monkeypatch) -> None:
    """storage choice 3 → FHS /var/lib/kairix paths."""
    inputs = _script(tmp_path, storage="3")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config["paths"]["db_path"] == "/var/lib/kairix/index.sqlite"


@pytest.mark.unit
def test_wizard_interactive_template_collections_consulting(tmp_path: Path, monkeypatch) -> None:
    """collections choice 2 with the consulting preset → consulting collections."""
    inputs = _script(tmp_path, use_case="3", collections="2")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    cfg = yaml.safe_load(output.read_text())
    coll = cfg.get("collections", {}).get("shared", [])
    names = {c["name"] for c in coll}
    assert "clients" in names or "projects" in names


@pytest.mark.unit
def test_wizard_interactive_template_collections_technical(tmp_path: Path, monkeypatch) -> None:
    """collections choice 2 with the technical preset → docs/runbooks/reference."""
    inputs = _script(tmp_path, use_case="2", collections="2")
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    cfg = yaml.safe_load(output.read_text())
    coll = cfg.get("collections", {}).get("shared", [])
    names = {c["name"] for c in coll}
    assert "docs" in names or "runbooks" in names


@pytest.mark.unit
def test_wizard_interactive_workspaces_collection(tmp_path: Path, monkeypatch) -> None:
    """collections choice 3 → include agent workspace memories."""
    inputs = _script(tmp_path, collections="3")
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_agent_step_renders_backend_connect_snippets(tmp_path: Path, monkeypatch, capsys) -> None:
    """Step 7 renders the MCP URL + snippets from the backend's
    ``agent_connect_info()`` — no terminal-only static instructions, no
    port probing (M6 parity)."""
    from kairix.platform.setup.service import AgentConnectInfo, ConnectSnippet

    service = FakeSetupService(
        mcp_url="http://127.0.0.1:8765/mcp",
        connect_snippets=(
            ConnectSnippet(client="Claude Code", config_text='{"mcpServers": {}}'),
            ConnectSnippet(client="Generic MCP over HTTP", config_text="http://127.0.0.1:8765/mcp"),
        ),
    )
    inputs = _script(tmp_path)
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, service=service)
    assert result is True
    out = capsys.readouterr().out
    assert "MCP endpoint: http://127.0.0.1:8765/mcp" in out
    assert "Claude Code" in out
    assert "Generic MCP over HTTP" in out
    # The connect info DTO round-trips — guard the import is real.
    assert isinstance(service.agent_connect_info(), AgentConnectInfo)


@pytest.mark.unit
def test_wizard_provider_menu_lists_registry_plugins(tmp_path: Path, monkeypatch, capsys) -> None:
    """M6 parity: the provider menu lists the installed plugin registry
    (the same source the web wizard renders), not a hardcoded trio."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup
    from kairix.providers import EntryPointRegistry

    registry_names = tuple(EntryPointRegistry().available())
    assert "azure_foundry" in registry_names
    assert "openai" in registry_names

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    # Defaults all the way through: provider pick falls to azure_foundry.
    inputs = iter(["", "", "", "", "", "", "1", "n", "1", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs, ""))

    ctx = SetupContext(interactive=True, json_mode=False, state_path=tmp_path / ".state.json")
    deps = WizardDeps(
        setup_service=lambda: FakeSetupService(),
        persist_credentials=lambda *_a: tmp_path / "unwritten-kairix.env",
        index_poll_seconds=0.0,
    )  # provider_names left at the production default — the real registry
    result = run_setup(
        output_path=str(tmp_path / "config.yaml"),
        ctx=ctx,
        document_path=str(doc_dir),
        deps=deps,
    )
    assert result is True
    out = capsys.readouterr().out
    for name in registry_names:
        assert name in out, f"registry plugin {name!r} missing from the provider menu:\n{out}"


# ---------------------------------------------------------------------------
# #474 — provider in config, credential persistence, honest connection test,
# next-step epilogue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_plugin_name_maps_survey_answer_to_plugin() -> None:
    """The azure endpoint shape resolves to a registered plugin name."""
    from kairix.platform.setup.wizard import provider_plugin_name

    assert provider_plugin_name("openai", "") == "openai"
    assert provider_plugin_name("azure", "https://res.services.ai.azure.com") == "azure_foundry"
    assert provider_plugin_name("azure", "https://res.openai.azure.com") == "azure_legacy"
    assert provider_plugin_name("azure", "") == "azure_foundry"
    assert provider_plugin_name("custom", "https://proxy.example.invalid/v1") == "openai"


@pytest.mark.unit
def test_picked_provider_plugin_remaps_azure_by_endpoint_shape() -> None:
    """Registry picks pass through; azure picks re-route by endpoint shape."""
    from kairix.platform.setup.wizard import picked_provider_plugin

    assert picked_provider_plugin("openai", "https://api.openai.com/v1") == "openai"
    assert picked_provider_plugin("anthropic", "") == "anthropic"
    assert picked_provider_plugin("azure_foundry", "https://res.openai.azure.com") == "azure_legacy"
    assert picked_provider_plugin("azure_legacy", "https://res.services.ai.azure.com") == "azure_foundry"
    assert picked_provider_plugin("azure_foundry", "") == "azure_foundry"


@pytest.mark.unit
def test_wizard_config_includes_provider_and_validates_clean(tmp_path: Path) -> None:
    """#474 defect 1: the generated config MUST carry the ``provider:``
    key (factory construction fails without it) and must be
    ``kairix config validate``-clean."""
    from kairix.core.search.config_validator import validate_config
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path),
    )

    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config.get("provider") == "azure_foundry", f"provider key missing/wrong: {config}"
    errors = validate_config(config)
    assert errors == [], f"generated config is not validate-clean: {errors}"


@pytest.mark.unit
def test_rerun_setup_preserves_existing_connector_config(tmp_path: Path) -> None:
    """#review-H3 clobber regression: a config that already carries
    ``topology`` + ``agents`` blocks (written by the web wizard or an
    operator) survives a terminal setup save against the same file —
    merge, not overwrite.

    Sabotage proof (executed): against the pre-fix whole-file-overwrite
    ``_write_config_yaml`` path this failed with
    ``AssertionError: topology clobbered: {...}`` /
    ``assert None == {'connectors': [...]}``; it passes after the save
    routes through the backend's merge write.
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "kairix.config.yaml"
    output.write_text(
        yaml.dump(
            {
                "topology": {"connectors": [{"type": "slack", "instance": "agent-alpha-workspace"}]},
                "agents": {"agent-alpha": {"role": "research"}},
            }
        ),
        encoding="utf-8",
    )
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path),
    )
    assert result is True
    config = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert config.get("topology") == {"connectors": [{"type": "slack", "instance": "agent-alpha-workspace"}]}, (
        f"topology clobbered: {config}"
    )
    assert config.get("agents") == {"agent-alpha": {"role": "research"}}, f"agents clobbered: {config}"
    # And the wizard's own answers still landed.
    assert config.get("provider") == "azure_foundry"
    assert "retrieval" in config


@pytest.mark.unit
def test_neo4j_opt_out_writes_explicit_disable_marker(tmp_path: Path, monkeypatch) -> None:
    """Answering 'no' to the knowledge graph writes an explicit
    ``graph: {enabled: false}`` marker — a merge save that simply omitted
    the key would leave a previously enabled graph switched on."""
    output = tmp_path / "kairix.config.yaml"
    output.write_text(
        yaml.dump({"graph": {"enabled": True, "uri": "bolt://old-host:7687"}}),
        encoding="utf-8",
    )
    inputs = _script(tmp_path, neo4j="n")
    result, output_written = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    config = yaml.safe_load(output_written.read_text(encoding="utf-8"))
    assert config["graph"]["enabled"] is False, f"graph opt-out not recorded: {config.get('graph')}"


@pytest.mark.unit
def test_persist_llm_credentials_writes_canonical_names(tmp_path: Path) -> None:
    """The wizard's persistence use-case writes the canonical secret
    lines through the same code path as ``kairix secrets set`` and then
    hydrates the bundle (seam-recorded here)."""
    from kairix.platform.setup.wizard import persist_llm_credentials

    bundle = tmp_path / "kairix.env"
    hydrated: list[Path] = []

    path = persist_llm_credentials(
        "example-credential-value",  # pragma: allowlist secret — generic fixture
        "https://example-resource.services.ai.azure.com",
        "text-embedding-3-large",
        "gpt-4o-mini",
        bundle_path=bundle,
        hydrate_fn=lambda p: hydrated.append(p) or 0,
    )

    assert path == bundle
    content = bundle.read_text(encoding="utf-8")
    assert "KAIRIX_PROVIDER_LLM_API_KEY=example-credential-value" in content  # pragma: allowlist secret
    assert "KAIRIX_PROVIDER_LLM_ENDPOINT=https://example-resource.services.ai.azure.com" in content
    # pragma: allowlist secret — model-name fixtures below, not credentials
    assert "KAIRIX_PROVIDER_EMBED_MODEL=text-embedding-3-large" in content  # pragma: allowlist secret
    # M6: the chat-model answer lands in the llm-model slot (NAME under
    # test — the slot the credentials resolver reads).
    assert "KAIRIX_PROVIDER_LLM_MODEL=" in content
    assert hydrated == [bundle], "persisted bundle must be hydrated for the in-process connection test"


@pytest.mark.unit
def test_persist_llm_credentials_skips_empty_values(tmp_path: Path) -> None:
    """Empty endpoint / model fields are skipped, not written as blank lines."""
    from kairix.platform.setup.wizard import persist_llm_credentials

    bundle = tmp_path / "kairix.env"
    path = persist_llm_credentials(
        "example-credential-value",  # pragma: allowlist secret — generic fixture
        "",
        "",
        "",
        bundle_path=bundle,
        hydrate_fn=lambda _p: 0,
    )
    assert path == bundle
    content = bundle.read_text(encoding="utf-8")
    assert "KAIRIX_PROVIDER_LLM_API_KEY=" in content
    assert "KAIRIX_PROVIDER_LLM_ENDPOINT" not in content
    assert "KAIRIX_PROVIDER_EMBED_MODEL" not in content
    assert "KAIRIX_PROVIDER_LLM_MODEL" not in content


@pytest.mark.unit
def test_wizard_persists_collected_credentials_including_chat_model(tmp_path: Path, monkeypatch) -> None:
    """#474 defect 2 + M6: the credentials collected in Step 1 — chat
    model included — flow into the persistence use-case (recorded through
    the WizardDeps seam). The old wizard discarded the chat-model answer
    in every branch."""
    recorded: list[tuple[str, str, str, str]] = []

    def _fake_persist(api_key: str, endpoint: str, embed_model: str, llm_model: str) -> Path:
        recorded.append((api_key, endpoint, embed_model, llm_model))
        return tmp_path / "kairix.env"

    inputs = _script(
        tmp_path,
        endpoint="https://res.services.ai.azure.com",
        api_key="typed-credential-value",  # pragma: allowlist secret — fake fixture value
        embed_model="embed-deployment",
        chat_model="chat-deployment",
    )
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, persist=_fake_persist)

    assert result is True
    assert recorded == [
        ("typed-credential-value", "https://res.services.ai.azure.com", "embed-deployment", "chat-deployment"),
    ], f"collected credentials did not reach the persistence use-case: {recorded}"


@pytest.mark.unit
def test_wizard_connection_test_uses_read_back_persisted_values(tmp_path: Path, monkeypatch) -> None:
    """#474 defect 3 (half 1): validation runs against what was PERSISTED
    (read back from the bundle), not against in-memory copies."""
    bundle = tmp_path / "kairix.env"

    def _fake_persist(_api_key: str, _endpoint: str, _embed_model: str, _llm_model: str) -> Path:
        # Simulates a store that normalised the values on write — the
        # probe must see the STORED values, proving read-back happens.
        bundle.write_text(
            "KAIRIX_PROVIDER_LLM_API_KEY=persisted-credential-value\n"  # pragma: allowlist secret
            "KAIRIX_PROVIDER_LLM_ENDPOINT=https://persisted.example.invalid\n",
            encoding="utf-8",
        )
        return bundle

    service = FakeSetupService()
    inputs = _script(tmp_path, endpoint="https://typed.example.invalid", api_key="typed-credential-value")
    _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, service=service, persist=_fake_persist)

    assert len(service.validate_calls) == 1
    _provider, api_key, endpoint, _deployment = service.validate_calls[0]
    assert api_key == "persisted-credential-value", (  # pragma: allowlist secret — generic fixture value
        f"validation used the typed value, not the stored one: {service.validate_calls}"
    )
    assert endpoint == "https://persisted.example.invalid"


class _ConfigSpyService(FakeSetupService):
    """FakeSetupService that records whether the config existed at
    validation time — proves the write-then-validate ordering (#474)."""

    def __init__(self, output: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._output = output
        self.config_exists_at_validate: bool | None = None

    def validate_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        deployment: str | None = None,
    ) -> Any:
        self.config_exists_at_validate = self._output.exists()
        return super().validate_provider(provider, api_key, endpoint, deployment)


@pytest.mark.unit
def test_wizard_connection_test_runs_after_config_write(tmp_path: Path) -> None:
    """#474 defect 3 (half 2): on a fresh machine the connection test
    runs against the just-written config — the config file must already
    exist when the probe fires."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    service = _ConfigSpyService(output)

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path, service=service),
    )

    assert result is True
    assert service.config_exists_at_validate is True, (
        "the connection test fired before the config was written — it cannot "
        "validate the just-written config on a fresh machine in that order"
    )


@pytest.mark.unit
def test_wizard_epilogue_lists_next_commands_and_paths(tmp_path: Path, capsys) -> None:
    """#474 defect 4: the epilogue prints the exact next commands and
    where the config + secrets were written."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=_deps(tmp_path),
    )

    assert result is True
    out = capsys.readouterr().out
    assert "kairix embed" in out
    assert "kairix onboard check" in out
    assert "kairix mcp serve" in out
    assert str(output) in out
    # No API key entered in non-interactive mode → the epilogue says so
    # instead of pointing at an unwritten secrets file.
    assert "Secrets: none stored" in out


@pytest.mark.unit
def test_wizard_epilogue_names_secrets_path_when_persisted(tmp_path: Path, monkeypatch, capsys) -> None:
    """When credentials were persisted, the epilogue names the bundle file."""
    bundle = tmp_path / "kairix.env"
    inputs = _script(tmp_path, endpoint="https://res.services.ai.azure.com", api_key="typed-credential-value")
    result, _ = _interactive_run_setup(
        tmp_path,
        monkeypatch,
        inputs=inputs,
        persist=lambda *_a: bundle,
    )

    assert result is True
    out = capsys.readouterr().out
    assert str(bundle) in out, f"epilogue does not say where secrets were written:\n{out}"


# ---------------------------------------------------------------------------
# Indexing through the backend (#review-H3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wizard_indexing_drives_backend_and_reports_done(tmp_path: Path, monkeypatch, capsys) -> None:
    """Answering 'y' to indexing kicks off the backend's index run and
    polls it to completion — the same code path the web wizard's
    indexing screen drives."""
    service = FakeSetupService(chunks_total=100, chunks_per_tick=50)
    inputs = _script(tmp_path, index="y")
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, service=service)
    assert result is True
    assert service.start_index_calls == 1, "wizard did not kick off the backend index run"
    out = capsys.readouterr().out
    assert "Index built" in out, f"indexing success not reported:\n{out}"


@pytest.mark.unit
def test_wizard_indexing_failure_reports_error_and_continues(tmp_path: Path, monkeypatch, capsys) -> None:
    """An index-run failure surfaces the backend's operator message and
    the wizard finishes instead of crashing (#review-H3)."""
    service = FakeSetupService(index_error="Indexing stopped: provider rejected the request.")
    inputs = _script(tmp_path, index="y")
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, service=service)
    assert result is True, "an indexing failure must not abort setup"
    out = capsys.readouterr().out
    assert "Indexing stopped" in out
    assert "kairix embed" in out


@pytest.mark.unit
def test_wizard_index_prompt_shows_one_time_cost_estimate(tmp_path: Path, monkeypatch, capsys) -> None:
    """M6 parity: the pre-index estimate is the backend scan's
    token-priced ONE-TIME cost — not the old per-month guess."""
    service = FakeSetupService(scan_files=2000, scan_words=1_000_000, scan_cost_usd=0.17)
    inputs = _script(tmp_path, index="n")
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs, service=service)
    assert result is True
    out = capsys.readouterr().out
    assert "one-time indexing cost: ~$0.17" in out, f"scan-priced estimate missing:\n{out}"
    assert "monthly" not in out.lower(), "the per-month cost guess must be gone"


@pytest.fixture
def _restored_environ() -> Any:
    """Snapshot the process environment and restore it after the test.

    The production hydrate seam's JOB is to load the bundle into
    ``os.environ``; this fixture undoes that side effect so it can't leak into
    later tests. It never sets a value to influence a production read (F2).
    """
    import os

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.mark.unit
@pytest.mark.usefixtures("_restored_environ")
def test_persist_llm_credentials_hydrates_bundle_with_the_production_default(tmp_path: Path) -> None:
    """With no ``hydrate_fn`` injected, the production default
    (``refresh_secrets``) loads the just-written bundle into the process env,
    so the in-process connection test resolves the stored values. A variable
    the operator already exported keeps priority (sidecar secrets are fallback).

    Sabotage proof: make ``_default_hydrate`` return 0 without calling
    ``refresh_secrets`` — the endpoint/model values never reach ``os.environ``
    and the assertions below fail. Restored.
    """
    import os

    from kairix.platform.setup.wizard import persist_llm_credentials

    written = {
        "KAIRIX_PROVIDER_LLM_API_KEY": "example-credential-value",  # pragma: allowlist secret — generic fixture
        "KAIRIX_PROVIDER_LLM_ENDPOINT": "https://example-resource.services.ai.azure.com",
        "KAIRIX_PROVIDER_EMBED_MODEL": "text-embedding-3-large",  # pragma: allowlist secret — model name
        "KAIRIX_PROVIDER_LLM_MODEL": "gpt-4o-mini",  # pragma: allowlist secret — model name
    }
    before = {name: os.environ.get(name) for name in written}
    bundle = tmp_path / "kairix.env"

    path = persist_llm_credentials(
        written["KAIRIX_PROVIDER_LLM_API_KEY"],
        written["KAIRIX_PROVIDER_LLM_ENDPOINT"],
        written["KAIRIX_PROVIDER_EMBED_MODEL"],
        written["KAIRIX_PROVIDER_LLM_MODEL"],
        bundle_path=bundle,
    )

    assert path == bundle
    for name, value in written.items():
        expected = before[name] if before[name] is not None else value
        assert os.environ.get(name) == expected, name
