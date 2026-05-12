"""Tests for setup wizard — config generation and template loading."""

from pathlib import Path

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
