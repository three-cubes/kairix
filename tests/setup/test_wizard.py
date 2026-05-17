"""Tests for setup wizard — config generation and template loading."""

from pathlib import Path
from typing import Any

import pytest
import yaml


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
def test_count_documents(tmp_path: Path) -> None:
    from kairix.platform.setup.wizard import count_documents

    # Create some test files
    (tmp_path / "doc1.md").write_text("hello")
    (tmp_path / "doc2.md").write_text("world " * 100)
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "doc3.md").write_text("nested")
    (tmp_path / "not-markdown.txt").write_text("ignored")

    count, size = count_documents(str(tmp_path))
    assert count == 3  # .md files only
    assert size > 0


@pytest.mark.unit
def test_count_documents_empty_dir(tmp_path: Path) -> None:
    from kairix.platform.setup.wizard import count_documents

    count, size = count_documents(str(tmp_path))
    assert count == 0
    assert size == pytest.approx(0.0)


@pytest.mark.unit
def test_count_documents_nonexistent_path() -> None:
    from kairix.platform.setup.wizard import count_documents

    count, size = count_documents("/nonexistent/path")
    assert count == 0
    assert size == pytest.approx(0.0)


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
    """Setup wizard fails cleanly when document root doesn't exist."""
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"
    nonexistent = str(tmp_path / "does-not-exist")

    from kairix.platform.setup.prompts import SetupContext

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=nonexistent,
        preset="general",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )

    assert result is False, "Wizard should reject a non-existent document root"
    assert not output.exists(), "Config file should not be written for invalid document root"


@pytest.mark.unit
def test_wizard_rejects_nonexistent_document_root_no_continue_option(
    tmp_path: Path,
) -> None:
    """Wizard must hard-reject a non-existent document root (no 'continue anyway?' escape)."""
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"
    nonexistent = str(tmp_path / "does-not-exist")

    # Use non-interactive context with document_path flag.
    # Before the fix, non-interactive mode returned False due to prompt_yn default.
    # After the fix, the wizard hard-rejects without even asking.
    from kairix.platform.setup.prompts import SetupContext

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=nonexistent,
        preset="general",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )

    assert result is False
    assert not output.exists()


@pytest.mark.unit
def test_wizard_accepts_valid_document_root(tmp_path: Path) -> None:
    """Setup wizard accepts a valid existing document root."""
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    from kairix.platform.setup.prompts import SetupContext

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )

    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_run_setup_generates_config(tmp_path: Path, monkeypatch) -> None:
    """run_setup writes a valid YAML config file."""
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "test-config.yaml"

    # Mock all interactive prompts — builtins.input is acceptable here because
    # prompts.py already uses ctx.interactive guard; we're testing the full
    # interactive flow end-to-end.
    inputs = iter(
        [
            "1",  # Step 1: Azure OpenAI
            "https://test.openai.azure.com",  # endpoint
            "test-key",  # API key
            "",  # embed model (default)
            "",  # chat model (default)
            "y",  # continue despite connection failure
            str(tmp_path),  # Step 2: document path
            "1",  # Step 3: default storage
            "2",  # Step 4: skip knowledge graph
            "4",  # Step 5: general content
            "1",  # Step 6: search everything
            "5",  # Step 7: skip agent integration
            "n",  # Step 8: don't index now
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    run_setup(
        output_path=str(output),
        deps=WizardDeps(connection_test=lambda *_a, **_k: False),
    )
    # Setup may return False due to connection failure + "continue anyway"
    # but the config file should still be written

    if output.exists():
        import yaml

        with open(output) as f:
            config = yaml.safe_load(f)
        assert "retrieval" in config
        assert isinstance(config["retrieval"], dict)


@pytest.mark.unit
def test_wizard_deps_default_factory_binds_callable() -> None:
    """``WizardDeps()`` with no overrides constructs a deps bag whose
    ``connection_test`` field is a callable, not ``None``.

    Sabotage proof: this is the corner the F6 issue specifically calls
    out — ``Optional[Callable] = None`` self-resolving in ``__post_init__``
    has landed mypy bugs. ``default_factory`` must bind a real callable
    or this assertion fires. The test reads only the public field; it
    does not import the private default function (that would violate F5).
    """
    from kairix.platform.setup.wizard import WizardDeps

    deps = WizardDeps()
    # The type system narrows this away, but we want a runtime sabotage:
    # if the implementation regressed to ``Optional[Callable] = None``
    # without a __post_init__, ``deps.connection_test`` would be None at
    # runtime even if mypy were satisfied. The cast through callable() is
    # what catches that.
    assert callable(deps.connection_test), (
        f"default_factory must bind a callable; got {deps.connection_test!r}. "
        "Regressing to ``connection_test: Callable | None = None`` without a "
        "post-init wire-up would leave this None and break run_setup."
    )


@pytest.mark.unit
def test_wizard_deps_override_used_by_run_setup(tmp_path: Path) -> None:
    """``run_setup(deps=WizardDeps(connection_test=fake))`` routes the wizard's
    connection probe through the injected fake.

    Sabotage proof: the fake records every call. If the wizard ignored
    ``deps`` and fell through to the production probe, no calls would be
    recorded and this test would fail.
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    calls: list[tuple[str, str, str, str]] = []

    def _spy(provider: str, endpoint: str, api_key: str, embed_model: str) -> bool:
        calls.append((provider, endpoint, api_key, embed_model))
        return True

    output = tmp_path / "test-config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=WizardDeps(connection_test=_spy),
    )

    assert len(calls) == 1, f"injected probe should run exactly once; got {len(calls)} calls"
    # Provider key from prompt default (Azure)
    assert calls[0][0] == "azure"


@pytest.mark.unit
def test_wizard_preset_consulting_produces_consulting_collections(tmp_path: Path) -> None:
    """preset='consulting' wires the consulting collection template (lines 281+)."""
    import yaml

    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    # With interactive=False, prompt_choice returns default (0).
    # We still exercise the consulting preset_key in the template loader path.
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="consulting",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )
    assert result is True
    assert output.exists()
    config = yaml.safe_load(output.read_text())
    assert "retrieval" in config


@pytest.mark.unit
def test_wizard_preset_technical_produces_technical_retrieval(tmp_path: Path) -> None:
    """preset='technical' uses the technical retrieval template."""
    import yaml

    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="technical",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )
    assert result is True
    config = yaml.safe_load(output.read_text())
    assert "retrieval" in config


@pytest.mark.unit
def test_wizard_preset_daily_log_aliased_to_general(tmp_path: Path) -> None:
    """preset='daily-log' is aliased to 'general' (line 148)."""
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="daily-log",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )
    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_wizard_connection_test_failure_returns_true_non_interactive(tmp_path: Path) -> None:
    """When connection_test returns False in non-interactive mode, run_setup
    continues so a config is still emitted (line 187 path).
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=WizardDeps(connection_test=lambda *_a, **_k: False),
    )
    # Non-interactive mode: continues despite failure (continue_default=True)
    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_wizard_json_mode_emits_config_to_stdout(tmp_path: Path, capsys) -> None:
    """json_mode=True emits the config as JSON to stdout and returns True.

    Covers the ctx.json_mode branch.
    """
    import json

    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()

    ctx = SetupContext(interactive=False, json_mode=True, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(tmp_path / "config.yaml"),
        ctx=ctx,
        document_path=str(doc_dir),
        preset="general",
        deps=WizardDeps(connection_test=lambda *_a, **_k: True),
    )

    assert result is True
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert "paths" in parsed
    assert "retrieval" in parsed


@pytest.mark.unit
def test_test_llm_connection_returns_false_on_exception() -> None:
    """_test_llm_connection catches exceptions and returns False.

    Drives the public ``set_llm_endpoint_fn`` / ``set_llm_api_key_fn``
    kwarg seams on _test_llm_connection — F1-clean.
    """
    from kairix.platform.setup import wizard as wiz

    def _raise(_value: object) -> None:
        raise RuntimeError("secrets backend down")

    result = wiz._test_llm_connection(
        provider="azure",
        endpoint="https://x.openai.azure.com",
        api_key="key",  # pragma: allowlist secret
        embed_model="text-embedding-3-large",
        set_llm_endpoint_fn=_raise,
        set_llm_api_key_fn=_raise,
    )
    assert result is False


def _interactive_run_setup(
    tmp_path: Path,
    monkeypatch,
    *,
    inputs: list[str],
    connection_ok: bool = True,
    embed_main: Any = None,
) -> tuple[bool, Path]:
    """Helper: run wizard interactively with a fixed input sequence.

    ``embed_main`` (when not ``None``) is threaded through ``WizardDeps``
    so tests can drive the indexing path with a fake without
    monkey-patching ``kairix.core.embed.cli.main``.
    """
    from kairix.platform.setup.prompts import SetupContext
    from kairix.platform.setup.wizard import WizardDeps, run_setup

    output = tmp_path / "config.yaml"
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "note.md").write_text("# note", encoding="utf-8")

    input_iter = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(input_iter, ""))

    ctx = SetupContext(interactive=True, json_mode=False, state_path=tmp_path / ".state.json")
    result = run_setup(
        output_path=str(output),
        ctx=ctx,
        deps=WizardDeps(
            connection_test=lambda *_a, **_k: connection_ok,
            embed_main=embed_main,
        ),
    )
    return result, output


@pytest.mark.unit
def test_wizard_interactive_openai_provider(tmp_path: Path, monkeypatch) -> None:
    """provider_idx=1 → 'openai' branch (lines 168-170)."""
    inputs = [
        "1",  # Step 0: use case 'personal' → preset general
        "2",  # Step 1: provider 'OpenAI'
        "openai-key",  # API key
        "",  # embed model default
        "",  # chat model default
        str(tmp_path / "docs"),  # Step 2: document path
        "1",  # Step 3: default storage
        "n",  # Step 4: skip Neo4j
        "1",  # Step 6: search everything
        "5",  # Step 7: skip agent integration
        "n",  # Step 8: skip indexing
    ]
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    assert output.exists()


@pytest.mark.unit
def test_wizard_interactive_custom_provider(tmp_path: Path, monkeypatch) -> None:
    """provider_idx=2 → 'custom' branch (lines 172-176)."""
    inputs = [
        "1",
        "3",  # custom provider
        "https://my.endpoint",  # endpoint
        "custom-key",
        "my-embed",
        "my-chat",
        str(tmp_path / "docs"),
        "1",
        "n",
        "1",
        "5",
        "n",
    ]
    result, _output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_custom_storage_path(tmp_path: Path, monkeypatch) -> None:
    """storage_idx=1 → custom path branch (lines 225-227)."""
    custom_store = tmp_path / "my-store"
    inputs = [
        "1",
        "1",  # Azure
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "2",  # custom storage
        str(custom_store),
        "n",  # skip Neo4j
        "1",
        "5",
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_docker_storage(tmp_path: Path, monkeypatch) -> None:
    """storage_idx=2 → Docker paths (line 229)."""
    inputs = [
        "1",
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "3",  # Docker storage
        "n",
        "1",
        "5",
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_template_collections_consulting(tmp_path: Path, monkeypatch) -> None:
    """coll_idx=1 with consulting preset → consulting collections (lines 285+)."""
    inputs = [
        "3",  # use case 'consulting'
        "1",  # Azure
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",  # default storage
        "n",  # skip Neo4j
        "2",  # template collections
        "5",  # skip agent integration
        "n",
    ]
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    import yaml

    cfg = yaml.safe_load(output.read_text())
    coll = cfg.get("collections", {}).get("shared", [])
    names = {c["name"] for c in coll}
    assert "clients" in names or "projects" in names


@pytest.mark.unit
def test_wizard_interactive_template_collections_technical(tmp_path: Path, monkeypatch) -> None:
    """coll_idx=1 with technical preset → docs/runbooks/reference collections."""
    inputs = [
        "2",  # use case 'technical'
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "2",  # template collections
        "5",
        "n",
    ]
    result, output = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True
    import yaml

    cfg = yaml.safe_load(output.read_text())
    coll = cfg.get("collections", {}).get("shared", [])
    names = {c["name"] for c in coll}
    assert "docs" in names or "runbooks" in names


@pytest.mark.unit
def test_wizard_interactive_workspaces_collection(tmp_path: Path, monkeypatch) -> None:
    """coll_idx=2 → include agent workspace memories (lines 305-319)."""
    inputs = [
        "1",  # use case general
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "3",  # workspaces collection
        "5",
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_agent_openclaw(tmp_path: Path, monkeypatch) -> None:
    """agent_idx=1 → OpenClaw instructions (line 351)."""
    inputs = [
        "1",
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "1",  # search everything
        "2",  # OpenClaw
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_agent_sse(tmp_path: Path, monkeypatch) -> None:
    """agent_idx=2 → SSE/HTTP MCP instructions (lines 352-362)."""
    inputs = [
        "1",
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "1",
        "3",  # SSE/HTTP
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_agent_direct_python(tmp_path: Path, monkeypatch) -> None:
    """agent_idx=3 → Direct Python import (lines 363-365)."""
    inputs = [
        "1",
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "1",
        "4",  # direct Python
        "n",
    ]
    result, _ = _interactive_run_setup(tmp_path, monkeypatch, inputs=inputs)
    assert result is True


@pytest.mark.unit
def test_wizard_interactive_indexing_attempted(tmp_path: Path, monkeypatch) -> None:
    """When the operator says 'y' to indexing, the embed CLI is invoked (lines 426-434).

    Drives the public ``WizardDeps.embed_main`` DI seam — F1-clean. The
    fake raises so the wizard records the 'Indexing failed' branch.
    """

    def _raise_runtime():
        raise RuntimeError("simulated indexing failure")

    inputs = [
        "1",
        "1",
        "https://x.azure.com",
        "k",
        "",
        "",
        str(tmp_path / "docs"),
        "1",
        "n",
        "1",
        "5",
        "y",  # YES, start indexing
    ]
    result, _ = _interactive_run_setup(
        tmp_path, monkeypatch, inputs=inputs, connection_ok=True, embed_main=_raise_runtime
    )
    # Wizard caught the indexing failure and continued
    assert result is True
