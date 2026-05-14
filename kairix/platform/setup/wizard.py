"""Interactive setup wizard for first-time kairix configuration.

Walks through LLM credentials, document source, knowledge graph,
search preset, and initial indexing. Produces a kairix.config.yaml.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.platform.setup.prompts import SetupContext, prompt, prompt_choice, prompt_yn

logger = logging.getLogger(__name__)

# Old _prompt, _prompt_choice, _prompt_yn removed — replaced by
# kairix.platform.setup.prompts which supports interactive, non-interactive, and JSON modes.


def _test_llm_connection(provider: str, endpoint: str, api_key: str, embed_model: str) -> bool:
    """Test LLM connectivity with a single embed + chat call."""
    from kairix.secrets import set_llm_api_key, set_llm_endpoint

    try:
        if provider == "azure":
            set_llm_endpoint(endpoint)
            set_llm_api_key(api_key)
        elif provider == "openai":
            set_llm_api_key(api_key)

        from kairix.platform.llm import get_default_backend

        backend = get_default_backend()
        # Test embed
        vec = backend.embed("test connection")
        if not vec or len(vec) < 100:
            print("  Warning: embedding returned fewer dimensions than expected")
            return False
        # Test chat
        response = backend.chat(
            [{"role": "user", "content": "Say 'ok' and nothing else."}],
            max_tokens=5,
        )
        if not response:
            print("  Warning: chat returned empty response")
            return False
        return True
    except Exception as exc:
        logger.warning("wizard: connection check failed — %s", exc)
        print("  Connection failed — check your Azure endpoint and API key.")
        return False


def count_documents(path: str) -> tuple[int, float]:
    """Count markdown files and total size in MB."""
    p = Path(path)
    if not p.is_dir():
        return 0, 0.0
    files = list(p.rglob("*.md"))
    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    return len(files), total_bytes / (1024 * 1024)


def load_template(name: str) -> dict[str, Any]:
    """Load an ontology template by name."""
    template_dir = Path(__file__).parent / "templates"
    template_path = template_dir / f"{name}.yaml"
    if not template_path.exists():
        return {}
    with open(template_path) as f:
        return yaml.safe_load(f) or {}


@dataclass
class WizardDeps:
    """Injectable dependencies for ``run_setup``.

    Replaces the F6-violating ``connection_test_fn=None`` test-only kwarg
    with a typed dataclass. Production code calls ``run_setup`` without
    ``deps`` — the default factory wires the real LLM connection probe.
    Tests construct ``WizardDeps(connection_test=lambda *_a, **_k: True)``
    and pass it through.

    The field is non-Optional with a ``default_factory`` (per CLAUDE.md
    F6 guidance) so mypy sees the production callable directly — no
    ``assert deps.x is not None`` ladder is needed inside the wizard.
    """

    connection_test: Callable[[str, str, str, str], bool] = field(default_factory=lambda: _test_llm_connection)


_USE_CASE_OPTIONS = [
    "Personal knowledge base (notes, journals, research)",
    "Technical documentation (code, runbooks, APIs)",
    "Business / consulting (clients, projects, proposals)",
    "Agent memory (OpenClaw, Claude Code, LangGraph)",
    "Just exploring (use the reference library)",
]
# Maps the use-case index to the matching template preset key.
_USE_CASE_TO_PRESET = ["general", "technical", "consulting", "general", "general"]

_PROVIDER_OPTIONS = [
    "Azure OpenAI (recommended for enterprise)",
    "OpenAI",
    "Other OpenAI-compatible endpoint",
]
_PROVIDER_KEYS = ["azure", "openai", "custom"]

_STORAGE_OPTIONS = [
    "Default location (~/.cache/kairix/) — good for personal use",
    "Custom path — for shared or production deployments",
    "Docker paths (/data/kairix/) — for container deployments",
]

_COLLECTION_OPTIONS = [
    "Search everything — all documents in one collection (simplest)",
    "Use template collections (based on your preset above)",
    "Include agent workspace memories (for agent platforms)",
    "Skip — I'll configure collections later",
]

_AGENT_OPTIONS = [
    "Claude Desktop / Claude Code (stdio MCP)",
    "OpenClaw or similar agent platform (stdio MCP)",
    "Docker / HTTP service (SSE MCP on port 8080)",
    "Direct Python import (no MCP server needed)",
    "Skip — I'll configure this later",
]


def _resolve_preset(ctx: SetupContext, preset: str | None) -> str:
    """Step 0: pick the template preset from CLI flag or use-case survey."""
    if preset is not None:
        return preset if preset != "daily-log" else "general"
    idx = prompt_choice(ctx, "What are you setting up kairix for?", _USE_CASE_OPTIONS, default=0)
    return _USE_CASE_TO_PRESET[idx]


def _prompt_llm_credentials(ctx: SetupContext) -> tuple[str, str, str, str]:
    """Step 1a: gather (provider_key, endpoint, api_key, embed_model)."""
    provider_idx = prompt_choice(ctx, "Which LLM provider are you using?", _PROVIDER_OPTIONS, default=0)
    provider_key = _PROVIDER_KEYS[provider_idx]
    if provider_key == "azure":
        endpoint = prompt(ctx, "Azure OpenAI endpoint")
        api_key = prompt(ctx, "API key")
        embed_model = prompt(ctx, "Embedding model deployment name", "text-embedding-3-large")
        prompt(ctx, "Chat model deployment name", "gpt-4o-mini")  # future config expansion
    elif provider_key == "openai":
        endpoint = ""
        api_key = prompt(ctx, "OpenAI API key")
        embed_model = prompt(ctx, "Embedding model", "text-embedding-3-large")
        prompt(ctx, "Chat model", "gpt-4o-mini")  # future config expansion
    else:
        endpoint = prompt(ctx, "Endpoint URL")
        api_key = prompt(ctx, "API key")
        embed_model = prompt(ctx, "Embedding model name")
        prompt(ctx, "Chat model name")  # future config expansion
    return provider_key, endpoint, api_key, embed_model


def _confirm_llm_connection(
    ctx: SetupContext,
    deps: WizardDeps,
    provider_key: str,
    endpoint: str,
    api_key: str,
    embed_model: str,
) -> bool:
    """Step 1b: test the LLM connection and ask whether to continue on failure."""
    print("\n  Testing connection...")
    if deps.connection_test(provider_key, endpoint, api_key, embed_model):
        print("  ✓ Connected successfully\n")
        return True
    print("  ✗ Connection failed — check your credentials and try again\n")
    # Non-interactive mode is for CI/Docker/scripted bootstrap where the
    # operator can't answer a prompt; default to continuing so a config
    # is still emitted. Interactive operators retain the safer default.
    continue_default = not ctx.interactive
    return prompt_yn(ctx, "Continue anyway?", default=continue_default)


def _resolve_document_root(ctx: SetupContext, document_path: str | None) -> str | None:
    """Step 2: resolve & validate the document root. Returns None on missing dir."""
    if document_path:
        doc_root = os.path.expanduser(document_path)
    else:
        doc_root = prompt(
            ctx,
            "Where are your documents? (path to folder)",
            default=str(Path.home() / "Documents"),
        )
        doc_root = os.path.expanduser(doc_root)
    if not os.path.isdir(doc_root):
        print(f"\n  Error: '{doc_root}' does not exist or is not a directory.")
        print("  Create the folder first, then re-run setup.\n")
        return None
    return doc_root


def _resolve_storage_dir(ctx: SetupContext) -> str:
    """Step 3: pick the data directory based on the storage option."""
    idx = prompt_choice(ctx, "Where should kairix store its data?", _STORAGE_OPTIONS)
    if idx == 0:
        return str(Path.home() / ".cache" / "kairix")
    if idx == 1:
        return os.path.expanduser(prompt(ctx, "Data directory path"))
    return "/data/kairix"


def _prompt_neo4j(ctx: SetupContext) -> tuple[bool, str]:
    """Step 4: knowledge-graph (Neo4j) selection. Returns (enabled, uri)."""
    if not prompt_yn(ctx, "\n  Enable knowledge graph?", default=True):
        return False, ""
    neo4j_uri = prompt(ctx, "Neo4j URI", "bolt://localhost:7687")
    try:
        from kairix.knowledge.graph.client import Neo4jClient

        client = Neo4jClient.__new__(Neo4jClient)
        client._uri = neo4j_uri
        print("  ✓ Neo4j URI configured\n")
    except Exception:
        print("  Note: Neo4j connection will be tested when the service starts\n")
    return True, neo4j_uri


_PRESET_COLLECTIONS: dict[str, list[dict[str, str]]] = {
    "consulting": [
        {"name": "clients", "path": "Clients", "glob": "**/*.md"},
        {"name": "projects", "path": "Projects", "glob": "**/*.md"},
        {"name": "knowledge", "path": "Knowledge", "glob": "**/*.md"},
        {"name": "entities", "path": "Entities", "glob": "**/*.md"},
    ],
    "technical": [
        {"name": "docs", "path": "docs", "glob": "**/*.md"},
        {"name": "runbooks", "path": "runbooks", "glob": "**/*.md"},
        {"name": "reference", "path": "reference", "glob": "**/*.md"},
    ],
}


def _build_workspace_collections() -> dict[str, Any]:
    """Build the all-docs + agent-workspaces collection config."""
    from kairix.paths import workspace_root as _ws_root_fn

    workspace_root = str(_ws_root_fn())
    print(f"  ✓ Documents + agent workspace memories ({workspace_root}) configured.\n")
    return {
        "shared": [
            {"name": "all", "path": ".", "glob": "**/*.md"},
            {"name": "workspaces", "path": workspace_root, "glob": "**/memory/**/*.md"},
        ],
    }


def _resolve_collections(ctx: SetupContext, preset_key: str) -> dict[str, Any] | None:
    """Step 6: collection-organisation choice. Returns None on 'skip'."""
    idx = prompt_choice(ctx, "How do you want to organise your documents?", _COLLECTION_OPTIONS)
    if idx == 0:
        print("  ✓ All documents will be searchable.\n")
        return {"shared": [{"name": "all", "path": ".", "glob": "**/*.md"}]}
    if idx == 1:
        shared = _PRESET_COLLECTIONS.get(preset_key, [{"name": "all", "path": ".", "glob": "**/*.md"}])
        config = {"shared": shared}
        print(f"  ✓ {len(shared)} collections configured.\n")
        return config
    if idx == 2:
        return _build_workspace_collections()
    return None


def _print_claude_desktop_instructions() -> None:
    """Print Claude Desktop / Code MCP wiring instructions."""
    import platform as _platform

    if _platform.system() == "Darwin":
        config_path_hint = "~/Library/Application Support/Claude/claude_desktop_config.json"
    else:
        config_path_hint = "~/.config/Claude/claude_desktop_config.json"
    print(f"\n  To connect Claude Desktop, add this to:\n  {config_path_hint}\n")
    print("  {")
    print('    "mcpServers": {')
    print('      "kairix": {')
    print('        "command": "kairix",')
    print('        "args": ["mcp", "serve"]')
    print("      }")
    print("    }")
    print("  }\n")


def _print_sse_instructions() -> None:
    """Print SSE MCP server (Docker/HTTP) startup hint."""
    from kairix.platform.onboard.ports import find_available_port, is_port_available

    default_port = 8080
    if is_port_available(default_port):
        mcp_port = default_port
    else:
        mcp_port = find_available_port(preferred=default_port)
        print(f"\n  Port {default_port} is in use — suggesting {mcp_port} instead.")
    print(f"\n  MCP endpoint: http://localhost:{mcp_port}")
    print(f"  Start with: kairix mcp serve --transport sse --port {mcp_port}\n")


def _print_agent_instructions(ctx: SetupContext) -> None:
    """Step 7: agent-platform integration hints."""
    idx = prompt_choice(ctx, "Select your agent platform:", _AGENT_OPTIONS)
    if idx == 0:
        _print_claude_desktop_instructions()
    elif idx == 1:
        print('\n  Run: openclaw mcp set mcp-kairix "kairix mcp serve"\n')
    elif idx == 2:
        _print_sse_instructions()
    elif idx == 3:
        print("\n  Import directly in Python:")
        print("  from kairix.agents.mcp.server import tool_search, tool_research\n")


def _build_full_config(
    template: dict[str, Any],
    doc_root: str,
    db_path: str,
    log_dir: str,
    collections_config: dict[str, Any] | None,
    use_neo4j: bool,
    neo4j_uri: str,
) -> dict[str, Any]:
    """Assemble the final ``full_config`` dict from the wizard's collected fields."""
    retrieval = template.get("retrieval") or {"fusion_strategy": "bm25_primary"}
    full_config: dict[str, Any] = {
        "paths": {"document_root": doc_root, "db_path": db_path, "log_dir": log_dir},
    }
    if collections_config:
        full_config["collections"] = collections_config
    full_config["retrieval"] = retrieval
    if use_neo4j:
        full_config["graph"] = {"enabled": True, "uri": neo4j_uri}
    return full_config


def _write_config_yaml(output_path: str, template_name: str, full_config: dict[str, Any]) -> Path:
    """Write the YAML config file and return its path."""
    output = Path(output_path)
    with open(output, "w") as f:
        f.write("# kairix configuration — generated by kairix setup\n")
        f.write(f"# Preset: {template_name}\n\n")
        yaml.dump(full_config, f, default_flow_style=False, sort_keys=False)
    print(f"  Config saved to: {output}\n")
    return output


def _maybe_run_initial_index(ctx: SetupContext, file_count: int) -> None:
    """Offer to run the initial embed pass; never raises."""
    print("Ready to index your documents.\n")
    if file_count <= 0:
        print("  No documents found to index. Add documents to your document store")
        print("  and run 'kairix embed' when ready.\n")
        return
    est_minutes = max(1, file_count // 1000)
    est_cost = max(1, file_count // 800)
    print("  Ready to index your documents.")
    print(f"  Estimated time: ~{est_minutes} minute{'s' if est_minutes > 1 else ''}")
    print(f"  Estimated monthly LLM cost: ~${est_cost}\n")
    # Default off in non-interactive: scripted bootstrap shouldn't trigger
    # a side-effecting embed run; the operator can run 'kairix embed' separately.
    if not prompt_yn(ctx, "Start indexing now?", default=ctx.interactive):
        print("  Skipped. Run 'kairix embed' when you're ready.\n")
        return
    print("\n  Indexing...")
    try:
        from kairix.core.embed.cli import main as embed_main

        embed_main()
        print("  ✓ Index built\n")
    except Exception as exc:
        logger.warning("wizard: indexing failed — %s", exc)
        print("  Indexing failed — check server logs for details.")
        print("  You can run 'kairix embed' manually later.\n")


def _run_health_check_summary() -> None:
    """Run onboarding health checks and print a one-line summary; never raises."""
    print("Running health check...")
    try:
        from kairix.platform.onboard.check import run_all_checks

        results = run_all_checks()
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        print(f"  ✓ {passed}/{total} checks passed\n")
    except Exception:
        print("  Health check skipped (run 'kairix onboard check' manually)\n")


def _print_setup_summary(output: Path) -> None:
    """Print the closing 'setup complete' summary block."""
    print("Setup complete. Your knowledge base is ready.\n")
    print('  Search:     kairix search "your question here"')
    print("  MCP server: kairix mcp serve")
    print("  Research:   kairix mcp serve  (then call tool_research via MCP)")
    print("  Benchmark:  kairix eval build-gold --suite queries.yaml")
    print(f"\n  Config: {output}\n")


def _redirect_for_json_mode(json_mode: bool) -> Any:
    """In JSON mode, route narrative chatter to stderr; return the real stdout."""
    if not json_mode:
        return None
    import sys as _sys

    real_stdout = _sys.stdout
    _sys.stdout = _sys.stderr
    return real_stdout


def _emit_json_config(real_stdout: Any, full_config: dict[str, Any]) -> None:
    """Restore stdout and write the JSON config blob for scripted bootstrap."""
    import json as _json
    import sys as _sys

    if real_stdout is not None:
        _sys.stdout = real_stdout
    _sys.stdout.write(_json.dumps(full_config, indent=2) + "\n")


def run_setup(
    output_path: str = "kairix.config.yaml",
    ctx: SetupContext | None = None,
    preset: str | None = None,
    document_path: str | None = None,
    deps: WizardDeps | None = None,
) -> bool:
    """Run the setup wizard.

    Supports interactive (terminal), non-interactive (flags/defaults),
    and JSON output modes via SetupContext.

    Args:
        deps: Injectable dependencies. Tests construct
              ``WizardDeps(connection_test=fake)``; production omits the kwarg
              and the default factory wires ``_test_llm_connection``.

    Returns True if setup completed successfully.
    """
    deps = deps if deps is not None else WizardDeps()
    if ctx is None:
        ctx = SetupContext.auto_detect()

    real_stdout = _redirect_for_json_mode(ctx.json_mode)

    print("\nWelcome to kairix setup.\n")
    print("This will configure your knowledge base in a few steps.")
    print("You'll need: an LLM API key and a folder of documents.\n")

    # Step 0: use-case -> preset
    preset_key = _resolve_preset(ctx, preset)

    # Step 1: LLM backend
    print("Step 1 of 7: LLM Backend\n")
    provider_key, endpoint, api_key, embed_model = _prompt_llm_credentials(ctx)
    if not _confirm_llm_connection(ctx, deps, provider_key, endpoint, api_key, embed_model):
        return False

    # Step 2: document root
    print("Step 2 of 7: Document Source\n")
    doc_root = _resolve_document_root(ctx, document_path)
    if doc_root is None:
        return False
    file_count, size_mb = count_documents(doc_root)
    print(f"\n  Found: {file_count:,} markdown files ({size_mb:.1f} MB)\n")

    # Step 3: storage location
    print("Step 3 of 7: Where to store the search index\n")
    print("  Kairix needs a place to store its search index and logs.\n")
    db_dir = _resolve_storage_dir(ctx)
    db_path = os.path.join(db_dir, "index.sqlite")
    log_dir = os.path.join(db_dir, "logs")
    print(f"  ✓ Index: {db_path}")
    print(f"  ✓ Logs: {log_dir}\n")

    # Step 4: knowledge graph
    print("Step 4 of 7: Knowledge Graph (optional)\n")
    print("  The knowledge graph tracks people, companies, and relationships")
    print("  for better search results. It requires Neo4j.")
    use_neo4j, neo4j_uri = _prompt_neo4j(ctx)

    # Step 5: search preset (purely informational)
    print("Step 5 of 7: Search Configuration\n")
    print(f"  Using '{preset_key}' preset from your use-case selection.\n")
    template = load_template(preset_key)
    template_name = template.get("name", preset_key)
    print(f"\n  Using '{template_name}' preset.\n")

    # Step 6: collections
    print("Step 6 of 7: Document Collections\n")
    print("  Collections let you organise which documents are searched.")
    print("  You can search everything, or split into groups.\n")
    collections_config = _resolve_collections(ctx, preset_key)

    # Step 7: agent integration
    print("Step 7 of 7: Agent Integration\n")
    print("  How will your agents connect to kairix?\n")
    _print_agent_instructions(ctx)

    # Build config
    full_config = _build_full_config(template, doc_root, db_path, log_dir, collections_config, use_neo4j, neo4j_uri)

    if ctx.json_mode:
        # JSON mode emits the config to stdout and skips file write +
        # subsequent steps that don't make sense in scripted bootstrap.
        _emit_json_config(real_stdout, full_config)
        return True

    output = _write_config_yaml(output_path, template_name, full_config)
    _maybe_run_initial_index(ctx, file_count)
    _run_health_check_summary()
    _print_setup_summary(output)
    return True
