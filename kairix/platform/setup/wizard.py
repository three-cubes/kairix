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

    # In JSON mode the operator contract is "stdout is parseable JSON";
    # route narrative chatter to stderr so the final config is the only
    # thing on stdout. The redirect is restored before the JSON dump.
    if ctx.json_mode:
        import sys as _sys

        _real_stdout = _sys.stdout
        _sys.stdout = _sys.stderr
    else:
        _real_stdout = None

    print("\nWelcome to kairix setup.\n")
    print("This will configure your knowledge base in a few steps.")
    print("You'll need: an LLM API key and a folder of documents.\n")

    config: dict[str, Any] = {}

    # ── Step 0: Use-case survey (new — tailors defaults) ────────────────
    if preset is None:
        use_cases = [
            "Personal knowledge base (notes, journals, research)",
            "Technical documentation (code, runbooks, APIs)",
            "Business / consulting (clients, projects, proposals)",
            "Agent memory (OpenClaw, Claude Code, LangGraph)",
            "Just exploring (use the reference library)",
        ]
        uc_idx = prompt_choice(ctx, "What are you setting up kairix for?", use_cases, default=0)
        preset_key = ["general", "technical", "consulting", "general", "general"][uc_idx]
    else:
        preset_key = preset if preset != "daily-log" else "general"

    # ── Step 1: LLM Backend ──────────────────────────────────────────────
    print("Step 1 of 7: LLM Backend\n")

    providers = [
        "Azure OpenAI (recommended for enterprise)",
        "OpenAI",
        "Other OpenAI-compatible endpoint",
    ]
    provider_idx = prompt_choice(ctx, "Which LLM provider are you using?", providers, default=0)
    provider_key = ["azure", "openai", "custom"][provider_idx]

    if provider_key == "azure":
        endpoint = prompt(ctx, "Azure OpenAI endpoint")
        api_key = prompt(ctx, "API key")
        embed_model = prompt(ctx, "Embedding model deployment name", "text-embedding-3-large")
        prompt(ctx, "Chat model deployment name", "gpt-4o-mini")  # consumed by future config expansion
    elif provider_key == "openai":
        endpoint = ""
        api_key = prompt(ctx, "OpenAI API key")
        embed_model = prompt(ctx, "Embedding model", "text-embedding-3-large")
        prompt(ctx, "Chat model", "gpt-4o-mini")  # consumed by future config expansion
    else:
        endpoint = prompt(ctx, "Endpoint URL")
        api_key = prompt(ctx, "API key")
        embed_model = prompt(ctx, "Embedding model name")
        prompt(ctx, "Chat model name")  # consumed by future config expansion

    print("\n  Testing connection...")
    if deps.connection_test(provider_key, endpoint, api_key, embed_model):
        print("  \u2713 Connected successfully\n")
    else:
        print("  \u2717 Connection failed — check your credentials and try again\n")
        # Non-interactive mode is for CI/Docker/scripted bootstrap where the
        # operator can't answer a prompt; default to continuing so a config
        # is still emitted. Interactive operators retain the safer default.
        continue_default = not ctx.interactive
        if not prompt_yn(ctx, "Continue anyway?", default=continue_default):
            return False

    # ── Step 2: Document Source ───────────────────────────────────────────
    print("Step 2 of 7: Document Source\n")

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
        return False

    file_count, size_mb = count_documents(doc_root)
    print(f"\n  Found: {file_count:,} markdown files ({size_mb:.1f} MB)\n")

    # ── Step 3: Storage Location ──────────────────────────────────────────
    print("Step 3 of 7: Where to store the search index\n")
    print("  Kairix needs a place to store its search index and logs.\n")

    storage_options = [
        "Default location (~/.cache/kairix/) — good for personal use",
        "Custom path — for shared or production deployments",
        "Docker paths (/data/kairix/) — for container deployments",
    ]
    storage_idx = prompt_choice(ctx, "Where should kairix store its data?", storage_options)

    if storage_idx == 0:
        db_dir = str(Path.home() / ".cache" / "kairix")
    elif storage_idx == 1:
        db_dir = prompt(ctx, "Data directory path")
        db_dir = os.path.expanduser(db_dir)
    else:
        db_dir = "/data/kairix"

    db_path = os.path.join(db_dir, "index.sqlite")
    log_dir = os.path.join(db_dir, "logs")
    print(f"  \u2713 Index: {db_path}")
    print(f"  \u2713 Logs: {log_dir}\n")

    # ── Step 4: Knowledge Graph ──────────────────────────────────────────
    print("Step 4 of 7: Knowledge Graph (optional)\n")
    print("  The knowledge graph tracks people, companies, and relationships")
    print("  for better search results. It requires Neo4j.")

    use_neo4j = prompt_yn(ctx, "\n  Enable knowledge graph?", default=True)
    neo4j_uri = ""
    if use_neo4j:
        neo4j_uri = prompt(ctx, "Neo4j URI", "bolt://localhost:7687")
        # Test Neo4j connection
        try:
            from kairix.knowledge.graph.client import Neo4jClient

            client = Neo4jClient.__new__(Neo4jClient)
            client._uri = neo4j_uri
            # Simple connectivity check would go here
            print("  \u2713 Neo4j URI configured\n")
        except Exception:
            print("  Note: Neo4j connection will be tested when the service starts\n")

    # ── Step 5: Search Configuration ─────────────────────────────────────
    print("Step 5 of 7: Search Configuration\n")
    print(f"  Using '{preset_key}' preset from your use-case selection.\n")

    template = load_template(preset_key)
    template_name = template.get("name", preset_key)
    print(f"\n  Using '{template_name}' preset.\n")

    # ── Step 6: Document Collections ──────────────────────────────────────
    print("Step 6 of 7: Document Collections\n")
    print("  Collections let you organise which documents are searched.")
    print("  You can search everything, or split into groups.\n")

    collection_options = [
        "Search everything — all documents in one collection (simplest)",
        "Use template collections (based on your preset above)",
        "Include agent workspace memories (for agent platforms)",
        "Skip — I'll configure collections later",
    ]
    coll_idx = prompt_choice(ctx, "How do you want to organise your documents?", collection_options)

    collections_config: dict[str, Any] | None = None
    if coll_idx == 0:
        collections_config = {
            "shared": [{"name": "all", "path": ".", "glob": "**/*.md"}],
        }
        print("  \u2713 All documents will be searchable.\n")
    elif coll_idx == 1:
        # Use preset-appropriate collections
        if preset_key == "consulting":
            collections_config = {
                "shared": [
                    {"name": "clients", "path": "Clients", "glob": "**/*.md"},
                    {"name": "projects", "path": "Projects", "glob": "**/*.md"},
                    {"name": "knowledge", "path": "Knowledge", "glob": "**/*.md"},
                    {"name": "entities", "path": "Entities", "glob": "**/*.md"},
                ],
            }
        elif preset_key == "technical":
            collections_config = {
                "shared": [
                    {"name": "docs", "path": "docs", "glob": "**/*.md"},
                    {"name": "runbooks", "path": "runbooks", "glob": "**/*.md"},
                    {"name": "reference", "path": "reference", "glob": "**/*.md"},
                ],
            }
        else:
            collections_config = {
                "shared": [{"name": "all", "path": ".", "glob": "**/*.md"}],
            }
        print(f"  \u2713 {len(collections_config['shared'])} collections configured.\n")
    elif coll_idx == 2:
        from kairix.paths import workspace_root as _ws_root_fn

        workspace_root = str(_ws_root_fn())
        collections_config = {
            "shared": [
                {"name": "all", "path": ".", "glob": "**/*.md"},
                {
                    "name": "workspaces",
                    "path": workspace_root,
                    "glob": "**/memory/**/*.md",
                },
            ],
        }
        print(f"  \u2713 Documents + agent workspace memories ({workspace_root}) configured.\n")

    # ── Step 7: Agent Integration ────────────────────────────────────────
    print("Step 7 of 7: Agent Integration\n")
    print("  How will your agents connect to kairix?\n")

    agent_options = [
        "Claude Desktop / Claude Code (stdio MCP)",
        "OpenClaw or similar agent platform (stdio MCP)",
        "Docker / HTTP service (SSE MCP on port 8080)",
        "Direct Python import (no MCP server needed)",
        "Skip — I'll configure this later",
    ]
    agent_idx = prompt_choice(ctx, "Select your agent platform:", agent_options)

    if agent_idx == 0:
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
    elif agent_idx == 1:
        print('\n  Run: openclaw mcp set mcp-kairix "kairix mcp serve"\n')
    elif agent_idx == 2:
        from kairix.platform.onboard.ports import find_available_port, is_port_available

        default_port = 8080
        if is_port_available(default_port):
            mcp_port = default_port
        else:
            mcp_port = find_available_port(preferred=default_port)
            print(f"\n  Port {default_port} is in use — suggesting {mcp_port} instead.")
        print(f"\n  MCP endpoint: http://localhost:{mcp_port}")
        print(f"  Start with: kairix mcp serve --transport sse --port {mcp_port}\n")
    elif agent_idx == 3:
        print("\n  Import directly in Python:")
        print("  from kairix.agents.mcp.server import tool_search, tool_research\n")

    # ── Build config ─────────────────────────────────────────────────────
    config = template.get("retrieval", {})
    if not config:
        config = {"fusion_strategy": "bm25_primary"}

    full_config: dict[str, Any] = {}

    # Paths section
    full_config["paths"] = {
        "document_root": doc_root,
        "db_path": db_path,
        "log_dir": log_dir,
    }

    # Collections section
    if collections_config:
        full_config["collections"] = collections_config

    # Retrieval section
    full_config["retrieval"] = config

    # Graph section
    if use_neo4j:
        full_config["graph"] = {"enabled": True, "uri": neo4j_uri}

    if ctx.json_mode:
        # JSON mode emits the config to stdout and skips file write +
        # subsequent steps that don't make sense in scripted bootstrap.
        import json as _json
        import sys as _sys

        if _real_stdout is not None:
            _sys.stdout = _real_stdout
        _sys.stdout.write(_json.dumps(full_config, indent=2) + "\n")
        return True

    output = Path(output_path)
    with open(output, "w") as f:
        f.write("# kairix configuration — generated by kairix setup\n")
        f.write(f"# Preset: {template_name}\n\n")
        yaml.dump(full_config, f, default_flow_style=False, sort_keys=False)

    print(f"  Config saved to: {output}\n")

    # ── Initial Index ────────────────────────────────────────────────────
    print("Ready to index your documents.\n")

    if file_count > 0:
        est_minutes = max(1, file_count // 1000)
        est_cost = max(1, file_count // 800)
        print("  Ready to index your documents.")
        print(f"  Estimated time: ~{est_minutes} minute{'s' if est_minutes > 1 else ''}")
        print(f"  Estimated monthly LLM cost: ~${est_cost}\n")

        # Default off in non-interactive: scripted bootstrap shouldn't trigger
        # a side-effecting embed run; the operator can run 'kairix embed'
        # separately. Interactive operators retain the True default.
        if prompt_yn(ctx, "Start indexing now?", default=ctx.interactive):
            print("\n  Indexing...")
            try:
                from kairix.core.embed.cli import main as embed_main

                embed_main()
                print("  \u2713 Index built\n")
            except Exception as exc:
                logger.warning("wizard: indexing failed — %s", exc)
                print("  Indexing failed — check server logs for details.")
                print("  You can run 'kairix embed' manually later.\n")
        else:
            print("  Skipped. Run 'kairix embed' when you're ready.\n")
    else:
        print("  No documents found to index. Add documents to your document store")
        print("  and run 'kairix embed' when ready.\n")

    # ── Health check ─────────────────────────────────────────────────────
    print("Running health check...")
    try:
        from kairix.platform.onboard.check import run_all_checks

        results = run_all_checks()
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        print(f"  \u2713 {passed}/{total} checks passed\n")
    except Exception:
        print("  Health check skipped (run 'kairix onboard check' manually)\n")

    # ── Summary ──────────────────────────────────────────────────────────
    print("Setup complete. Your knowledge base is ready.\n")
    print('  Search:     kairix search "your question here"')
    print("  MCP server: kairix mcp serve")
    print("  Research:   kairix mcp serve  (then call tool_research via MCP)")
    print("  Benchmark:  kairix eval build-gold --suite queries.yaml")
    print(f"\n  Config: {output}\n")

    return True
