"""Interactive setup wizard for first-time kairix configuration.

The terminal frontend over the SAME :class:`SetupService` backend the
web setup wizard drives (#review-H3/#review-M6): provider validation,
folder scanning, the first index run, and agent connect snippets all
come from the service, so the two surfaces cannot drift apart. The
wizard owns only the terminal rendering, the survey steps the web
wizard doesn't have (preset, storage, knowledge graph, collections),
and the final config save — a MERGE into the existing config, never a
whole-file overwrite.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.platform.setup.prompts import SetupContext, prompt, prompt_choice, prompt_yn
from kairix.platform.setup.service import AZURE_PROVIDER_NAMES, PLUGIN_AZURE_FOUNDRY

logger = logging.getLogger(__name__)


def _default_setup_service() -> Any:  # pragma: no cover  # lazy-import DI-default delegation
    """Production seam — the same SetupService backend the web wizard drives.

    Lazy call-time import: ``backends`` imports this module at load time
    (the canonical config writer + azure plugin split live here), so the
    dependency back up to the service factory must resolve at call time
    only — never at module import.
    """
    from kairix.platform.setup.service import build_setup_service

    return build_setup_service()


def _default_provider_names() -> tuple[str, ...]:
    """Production seam — provider plugin names from the installed registry.

    The same source the web wizard's provider screen renders, so a newly
    installed provider plugin shows up in both surfaces without a wizard
    change.
    """
    from kairix.providers import EntryPointRegistry

    return tuple(EntryPointRegistry().available())


def _default_write_config(updates: Mapping[str, Any], output_path: str | None) -> Path:
    """Production seam — the web wizard backend's merge-write, shared (#485).

    With no explicit ``output_path`` the save lands on the SAME target
    the web wizard resolves (config overlay when configured, else the
    runtime config file) with the same merge semantics — re-running
    ``kairix setup`` keeps ``topology``/``agents`` blocks written by
    the web wizard or the operator. An explicit ``--output`` keeps a
    direct file write, but it too reads the existing file and deep-merges
    rather than overwriting.

    Lazy call-time import (see :func:`_default_setup_service` for why).
    """
    from kairix.platform.setup.backends import update_config_file, write_config_updates

    if output_path:
        return update_config_file(Path(output_path), updates)
    # pragma rationale: lazy-import DI-default delegation — the no-output
    # branch resolves KAIRIX_CONFIG_OVERLAY_PATH / KAIRIX_CONFIG_PATH
    # through kairix.paths (F4); the testable merge logic lives in
    # kairix.platform.setup.backends.write_config_updates.
    from kairix.paths import (  # pragma: no cover — lazy-import DI-default delegation (rationale block above)
        config_overlay_path_override,
        config_path_override,
    )

    return write_config_updates(  # pragma: no cover  # lazy-import DI-default delegation
        updates,
        overlay_path=config_overlay_path_override(),
        config_path=config_path_override(),
    )


def _default_hydrate(path: Path) -> int:
    """Production seam — hydrate the just-written bundle into the process env.

    Routes through :func:`kairix.secrets.refresh_secrets` so the
    in-process connection test resolves the persisted canonical values
    through the standard loader chain.
    """
    from kairix.secrets import refresh_secrets

    return refresh_secrets(path)


# Legacy (pre-Foundry) Azure endpoint host fragments — mirrors the
# detection the credentials layer uses for SDK-shape routing.
_LEGACY_AZURE_ENDPOINT_FRAGMENTS = ("openai.azure.com", "cognitiveservices.azure.com")
_FOUNDRY_ENDPOINT_FRAGMENT = "services.ai.azure.com"
_OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"

# The azure plugin names (owned by service.py — F85) as they appear in the
# provider registry; picks of either re-route through the endpoint-shape
# split below.


def provider_plugin_name(provider_key: str, endpoint: str) -> str:
    """Map the wizard's provider survey answer to a provider plugin name.

    The returned name is what lands in the generated config's
    ``provider:`` field and what the connection test resolves through
    the plugin registry. Azure answers are split by endpoint shape:
    ``<r>.openai.azure.com`` / ``<r>.cognitiveservices.azure.com`` ride
    the ``azure_legacy`` plugin; everything else (including the
    recommended ``<r>.services.ai.azure.com``) rides ``azure_foundry``.
    "Other OpenAI-compatible endpoint" rides the ``openai`` plugin,
    which takes the stored endpoint verbatim as its base URL.
    """
    if provider_key == "azure":
        ep = endpoint.lower()
        is_legacy = any(fragment in ep for fragment in _LEGACY_AZURE_ENDPOINT_FRAGMENTS)
        if is_legacy and _FOUNDRY_ENDPOINT_FRAGMENT not in ep:
            return "azure_legacy"
        return PLUGIN_AZURE_FOUNDRY
    return "openai"


def picked_provider_plugin(picked: str, endpoint: str) -> str:
    """Map a registry pick + endpoint shape to the plugin name to persist.

    Azure picks re-route through :func:`provider_plugin_name` so a legacy
    ``<r>.openai.azure.com`` endpoint rides ``azure_legacy`` even when the
    operator picked ``azure_foundry`` (and vice versa) — the same remap
    the web wizard backend applies. Every other registry name passes
    through verbatim.
    """
    if picked in AZURE_PROVIDER_NAMES and endpoint:
        return provider_plugin_name("azure", endpoint)
    return picked


_LLM_API_KEY_SECRET = "kairix-provider-llm-api-key"  # noqa: S105 — secret SLOT name, not a value  # pragma: allowlist secret
_LLM_ENDPOINT_SECRET = "kairix-provider-llm-endpoint"  # noqa: S105 — secret SLOT name, not a value  # pragma: allowlist secret
_EMBED_MODEL_SECRET = "kairix-provider-embed-model"  # noqa: S105 — secret SLOT name, not a value  # pragma: allowlist secret
_LLM_MODEL_SECRET = "kairix-provider-llm-model"  # noqa: S105 — secret SLOT name, not a value  # pragma: allowlist secret


def persist_llm_credentials(
    api_key: str,
    endpoint: str,
    embed_model: str,
    llm_model: str = "",
    *,
    bundle_path: Path | None = None,
    hydrate_fn: Callable[[Path], int] = _default_hydrate,
) -> Path | None:
    """Persist the wizard's collected credentials under canonical names.

    Routes every value through :func:`kairix.secrets.set_secret` — the
    same use-case function behind ``kairix secrets set`` — so the wizard
    and the CLI share one persistence path (#473/#474). ``llm_model`` is
    the chat model/deployment answer; it lands in the
    ``kairix-provider-llm-model`` slot the credentials resolver reads,
    overriding its built-in default. Empty values are skipped. After the
    last write the bundle is hydrated into the process env
    (``hydrate_fn`` seam; production = ``refresh_secrets``) so the
    connection test resolves the stored values.

    Returns the bundle path written to, or ``None`` when every value
    was empty (nothing persisted, nothing hydrated).
    """
    from kairix.secrets import set_secret

    pairs = (
        (_LLM_API_KEY_SECRET, api_key),
        (_LLM_ENDPOINT_SECRET, endpoint),
        (_EMBED_MODEL_SECRET, embed_model),
        (_LLM_MODEL_SECRET, llm_model),
    )
    path: Path | None = None
    for name, value in pairs:
        if value:
            path = set_secret(name, value, bundle_path=bundle_path)
    if path is not None:
        hydrate_fn(path)
    return path


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

    Production code calls ``run_setup`` without ``deps`` — every field's
    ``default_factory`` wires the real implementation (non-Optional per
    CLAUDE.md F6 guidance, so no ``assert deps.x is not None`` ladder is
    needed inside the wizard). Tests construct
    ``WizardDeps(setup_service=lambda: FakeSetupService(...))`` and pass
    it through — the service is the primary seam, mirroring how the web
    wizard's routes are tested.
    """

    # The SetupService backend every side-effecting step rides: provider
    # validation, folder scanning, the first index run, agent connect
    # snippets. Tests inject ``lambda: FakeSetupService(...)``.
    setup_service: Callable[[], Any] = field(default_factory=lambda: _default_setup_service)
    # Public DI seam for credential persistence (#474). The production
    # default writes the collected (api_key, endpoint, embed_model,
    # chat_model) through kairix.secrets.set_secret and hydrates the
    # bundle; tests inject a recorder so no real bundle file or process
    # env is touched.
    persist_credentials: Callable[[str, str, str, str], Path | None] = field(
        default_factory=lambda: persist_llm_credentials,
    )
    # Provider plugin names for the Step 1 menu — the installed plugin
    # registry, same source as the web wizard's provider screen.
    provider_names: Callable[[], tuple[str, ...]] = field(
        default_factory=lambda: _default_provider_names,
    )
    # The final config save — (updates, explicit_output_path) → written
    # path. The production default merges into the web wizard's config
    # target (never a whole-file overwrite).
    write_config: Callable[[Mapping[str, Any], str | None], Path] = field(
        default_factory=lambda: _default_write_config,
    )
    # Seconds between index-progress polls. Tests pass 0.0 so a scripted
    # FakeSetupService run finishes without sleeping.
    index_poll_seconds: float = 0.5
    # The wizard's final step — run the onboarding health checks and print
    # a one-line summary. The production default runs ~18 real probes
    # (sockets/subprocess); tests inject ``lambda: None`` (or a recorder)
    # so a scripted run doesn't pay the probe tax. This was the last
    # un-seamed side-effecting step in ``run_setup`` — closing it keeps
    # every step of the orchestrator injectable.
    health_check: Callable[[], None] = field(default_factory=lambda: _run_health_check_summary)


_USE_CASE_OPTIONS = [
    "Personal knowledge base (notes, journals, research)",
    "Technical documentation (code, runbooks, APIs)",
    "Business / consulting (clients, projects, proposals)",
    "Agent memory (OpenClaw, Claude Code, LangGraph)",
    "Just exploring (use the reference library)",
]
# Maps the use-case index to the matching template preset key.
_USE_CASE_TO_PRESET = ["general", "technical", "consulting", "general", "general"]

_STORAGE_OPTIONS = [
    "Default location (~/.cache/kairix/) — good for personal use",
    "Custom path — for shared or production deployments",
    "FHS layout (/var/lib/kairix) — for system installs and v2026.6.8+ containers",
]

_COLLECTION_OPTIONS = [
    "Search everything — all documents in one collection (simplest)",
    "Use template collections (based on your preset above)",
    "Include agent workspace memories (for agent platforms)",
    "Skip — I'll configure collections later",
]


def _resolve_preset(ctx: SetupContext, preset: str | None) -> str:
    """Step 0: pick the template preset from CLI flag or use-case survey."""
    if preset is not None:
        return preset if preset != "daily-log" else "general"
    idx = prompt_choice(ctx, "What are you setting up kairix for?", _USE_CASE_OPTIONS, default=0)
    return _USE_CASE_TO_PRESET[idx]


def _prompt_llm_credentials(ctx: SetupContext, provider_names: tuple[str, ...]) -> tuple[str, str, str, str, str]:
    """Step 1a: pick a provider plugin and collect its credential fields.

    The menu lists the installed provider plugin registry — the same
    list the web wizard's provider screen renders. Returns
    ``(picked, endpoint, api_key, embed_model, chat_model)``; the chat
    model answer is PERSISTED (to the llm-model secret slot), not
    discarded.
    """
    names = list(provider_names) or ["openai"]
    default_idx = names.index(PLUGIN_AZURE_FOUNDRY) if PLUGIN_AZURE_FOUNDRY in names else 0
    idx = prompt_choice(ctx, "Which LLM provider are you using?", names, default=default_idx)
    picked = names[idx]
    endpoint = prompt(ctx, "Endpoint URL (blank for the provider's default)")
    api_key = prompt(ctx, "API key")
    embed_model = prompt(ctx, "Embedding model or deployment name", "text-embedding-3-large")
    chat_model = prompt(ctx, "Chat model or deployment name", "gpt-4o-mini")
    return picked, endpoint, api_key, embed_model, chat_model


def _confirm_llm_connection(
    ctx: SetupContext,
    service: Any,
    provider_plugin: str,
    endpoint: str,
    api_key: str,
    embed_model: str,
) -> bool:
    """Validate the persisted credentials through the SetupService backend.

    Runs AFTER the config write + credential persistence so the probe
    exercises what the operator will actually run with. The backend's
    validation message already carries the provider's own error verbatim
    plus the Azure deployment-name guidance (#484) — print it as-is,
    never re-blame the operator's credentials.
    """
    print("\n  Testing connection to your provider...")
    validation = service.validate_provider(provider_plugin, api_key, endpoint or None, embed_model or None)
    if validation.ok:
        print("  ✓ Connected successfully\n")
        return True
    print(f"  ✗ {validation.error}")
    print("  Your config and stored credentials are saved; nothing you entered is lost.\n")
    # Non-interactive mode is for CI/Docker/scripted bootstrap where the
    # operator can't answer a prompt; default to continuing so a config
    # is still emitted. Interactive operators retain the safer default.
    continue_default = not ctx.interactive
    return prompt_yn(ctx, "Continue anyway?", default=continue_default)


def _read_back_credentials(
    secrets_path: Path | None,
    endpoint: str,
    api_key: str,
) -> tuple[str, str]:
    """Read the persisted credentials back from the bundle file.

    The connection test must validate what was STORED (what every later
    command resolves), not the in-memory copies — a persistence bug
    would otherwise pass the probe and fail on first real use. Falls
    back to the collected values when nothing was persisted.
    """
    if secrets_path is None or not secrets_path.exists():
        return endpoint, api_key
    from kairix.secrets import load_secrets_file

    stored = load_secrets_file(secrets_path)
    return (
        stored.get("KAIRIX_PROVIDER_LLM_ENDPOINT") or endpoint,
        stored.get("KAIRIX_PROVIDER_LLM_API_KEY") or api_key,
    )


def _resolve_document_root(ctx: SetupContext, document_path: str | None, service: Any) -> str:
    """Step 2: resolve the candidate document root.

    Inside a container the prompt pre-fills with the mounted documents
    folder the backend suggests (#486, stock compose: ``/data/documents``);
    on bare metal it falls back to ``~/Documents``. Validation —
    existence, the absolute-path requirement, and the container-aware
    guidance — happens in the backend's ``scan_folder``, so both setup
    surfaces reject bad paths with the same words.
    """
    if document_path:
        return os.path.expanduser(document_path)
    hint = service.source_hint()
    default_root = hint.suggested_path or str(Path.home() / "Documents")
    doc_root = prompt(ctx, "Where are your documents? (path to folder)", default=default_root)
    return os.path.expanduser(doc_root)


def _resolve_storage_dir(ctx: SetupContext) -> str:
    """Step 3: pick the data directory based on the storage option."""
    idx = prompt_choice(ctx, "Where should kairix store its data?", _STORAGE_OPTIONS)
    if idx == 0:
        return str(Path.home() / ".cache" / "kairix")
    if idx == 1:
        return os.path.expanduser(prompt(ctx, "Data directory path"))
    # Docker / FHS — the v2026.6.8+ unified container + system install both
    # land here per Plan 1's kairix init.
    return "/var/lib/kairix"


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


_MARKDOWN_GLOB = "**/*.md"
_PRESET_COLLECTIONS: dict[str, list[dict[str, str]]] = {
    "consulting": [
        {"name": "clients", "path": "Clients", "glob": _MARKDOWN_GLOB},
        {"name": "projects", "path": "Projects", "glob": _MARKDOWN_GLOB},
        {"name": "knowledge", "path": "Knowledge", "glob": _MARKDOWN_GLOB},
        {"name": "entities", "path": "Entities", "glob": _MARKDOWN_GLOB},
    ],
    "technical": [
        {"name": "docs", "path": "docs", "glob": _MARKDOWN_GLOB},
        {"name": "runbooks", "path": "runbooks", "glob": _MARKDOWN_GLOB},
        {"name": "reference", "path": "reference", "glob": _MARKDOWN_GLOB},
    ],
}


def _build_workspace_collections() -> dict[str, Any]:
    """Build the all-docs + agent-workspaces collection config."""
    from kairix.paths import workspace_root as _ws_root_fn

    workspace_root = str(_ws_root_fn())
    print(f"  ✓ Documents + agent workspace memories ({workspace_root}) configured.\n")
    return {
        "shared": [
            {"name": "all", "path": ".", "glob": _MARKDOWN_GLOB},
            {"name": "workspaces", "path": workspace_root, "glob": "**/memory/**/*.md"},
        ],
    }


def _resolve_collections(ctx: SetupContext, preset_key: str) -> dict[str, Any] | None:
    """Step 6: collection-organisation choice. Returns None on 'skip'."""
    idx = prompt_choice(ctx, "How do you want to organise your documents?", _COLLECTION_OPTIONS)
    if idx == 0:
        print("  ✓ All documents will be searchable.\n")
        return {"shared": [{"name": "all", "path": ".", "glob": _MARKDOWN_GLOB}]}
    if idx == 1:
        shared = _PRESET_COLLECTIONS.get(preset_key, [{"name": "all", "path": ".", "glob": _MARKDOWN_GLOB}])
        config = {"shared": shared}
        print(f"  ✓ {len(shared)} collections configured.\n")
        return config
    if idx == 2:
        return _build_workspace_collections()
    return None


def _print_agent_instructions(service: Any) -> None:
    """Step 7: print the MCP endpoint + per-client connect snippets.

    Rendered from the backend's ``agent_connect_info()`` — the same
    source the web wizard's connect screen uses — so the terminal and
    web surfaces can never drift apart on connect instructions.
    """
    info = service.agent_connect_info()
    print(f"  MCP endpoint: {info.mcp_url}\n")
    for snippet in info.snippets:
        print(f"  {snippet.client}:")
        for line in snippet.config_text.splitlines():
            print(f"    {line}")
        print()


def _build_full_config(
    template: dict[str, Any],
    provider: str,
    doc_root: str,
    db_path: str,
    log_dir: str,
    collections_config: dict[str, Any] | None,
    use_neo4j: bool,
    neo4j_uri: str,
) -> dict[str, Any]:
    """Assemble the final ``full_config`` dict from the wizard's collected fields.

    ``provider`` is mandatory in the emitted config — factory
    construction fails without it, which was #474's headline defect
    (the wizard asked for the provider and then didn't write it).

    A Neo4j opt-out writes an explicit ``graph: {enabled: false}``
    marker rather than omitting the key: the wizard's save is a MERGE
    into any existing config, so omission would leave a previously
    enabled graph block switched on.
    """
    retrieval = template.get("retrieval") or {"fusion_strategy": "bm25_primary"}
    full_config: dict[str, Any] = {
        "provider": provider,
        "paths": {"document_root": doc_root, "db_path": db_path, "log_dir": log_dir},
    }
    if collections_config:
        full_config["collections"] = collections_config
    full_config["retrieval"] = retrieval
    if use_neo4j:
        full_config["graph"] = {"enabled": True, "uri": neo4j_uri}
    else:
        full_config["graph"] = {"enabled": False}
    return full_config


def write_config_yaml(output_path: str | Path, template_name: str, full_config: dict[str, Any]) -> Path:
    """Write the kairix YAML config file and return its path.

    The single config-writing routine shared by the terminal wizard and
    the web setup wizard backend (#474) — both surfaces emit the exact
    same file shape, so there is one place a config-format change lands.
    Silent on purpose: terminal callers print their own confirmation
    (:func:`_write_config_yaml`); the web backend must not write to
    stdout at all.
    """
    output = Path(output_path)
    with open(output, "w") as f:
        f.write("# kairix configuration — generated by kairix setup\n")
        f.write(f"# Preset: {template_name}\n\n")
        yaml.dump(full_config, f, default_flow_style=False, sort_keys=False)
    return output


def _run_index_to_completion(service: Any, poll_seconds: float) -> Any:
    """Kick off the backend's first-index run and poll it to completion.

    The index runs on the backend's own worker (the same code path the
    web wizard's indexing screen drives) — a lock-contention
    ``SystemExit`` or a provider failure lands in the returned status's
    ``error`` field instead of killing the wizard process, which is the
    #review-H3 crash fix.
    """
    service.start_index()
    status = service.index_status()
    reported = -1
    while status.running:
        if status.chunks_total > 0 and status.chunks_done != reported:
            print(f"  ... {status.chunks_done:,}/{status.chunks_total:,} chunks embedded")
            reported = status.chunks_done
        if poll_seconds > 0:
            time.sleep(poll_seconds)
        status = service.index_status()
    return status


def _maybe_run_initial_index(ctx: SetupContext, service: Any, scan: Any, *, poll_seconds: float) -> None:
    """Offer to run the first index through the backend; never raises.

    The size and cost lines come from the backend's folder scan — the
    same token-priced ONE-TIME estimate the web wizard shows, replacing
    the old per-month guess.
    """
    print("Ready to index your documents.\n")
    if scan.files <= 0:
        print("  No documents found to index. Add documents to your document store")
        print("  and run 'kairix embed' when ready.\n")
        return
    est_minutes = max(1, scan.files // 1000)
    print(f"  Estimated time: ~{est_minutes} minute{'s' if est_minutes > 1 else ''}")
    print(f"  Estimated one-time indexing cost: ~${scan.cost_estimate_usd:.2f}\n")
    # Default off in non-interactive: scripted bootstrap shouldn't trigger
    # a side-effecting embed run; the operator can run 'kairix embed' separately.
    if not prompt_yn(ctx, "Start indexing now?", default=ctx.interactive):
        print("  Skipped. Run 'kairix embed' when you're ready.\n")
        return
    print("\n  Indexing...")
    status = _run_index_to_completion(service, poll_seconds)
    if status.error:
        logger.warning("wizard: indexing failed — %s", status.error)
        print(f"  {status.error}")
        print("  You can run 'kairix embed' manually later.\n")
    elif status.done:
        print(f"  ✓ Index built ({status.chunks_done:,} chunks)\n")
    else:
        print("  Indexing finished without embedding any chunks.")
        print("  Run 'kairix embed' to retry once documents are in place.\n")


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


def _print_setup_summary(output: Path, secrets_path: Path | None) -> None:
    """Print the closing 'setup complete' summary block.

    Names exactly where the config + secrets were written and the three
    commands the operator runs next, in order (#474 defect 4 — the
    epilogue is the onboarding hand-off, not a feature tour).
    """
    print("Setup complete. Your knowledge base is ready.\n")
    print(f"  Config written to:  {output}")
    if secrets_path is not None:
        print(f"  Secrets written to: {secrets_path}")
    else:
        print("  Secrets: none stored (no API key was entered)")
    print("\nNext steps:")
    print("  1. kairix embed          — build the search index from your documents")
    print("  2. kairix onboard check  — verify the deployment end to end")
    print("  3. kairix mcp serve      — connect your agents over MCP")
    print()


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
    output_path: str | None = None,
    ctx: SetupContext | None = None,
    preset: str | None = None,
    document_path: str | None = None,
    deps: WizardDeps | None = None,
) -> bool:
    """Run the setup wizard.

    Supports interactive (terminal), non-interactive (flags/defaults),
    and JSON output modes via SetupContext.

    Args:
        output_path: Explicit config file to write. ``None`` — the
              production default — saves to the same merge target the
              web wizard uses (config overlay when configured, else the
              runtime config file).
        deps: Injectable dependencies. Tests construct
              ``WizardDeps(setup_service=lambda: FakeSetupService(...))``;
              production omits the kwarg and the default factories wire
              the real SetupService backend.

    Returns True if setup completed successfully.
    """
    deps = deps if deps is not None else WizardDeps()
    if ctx is None:
        ctx = SetupContext.auto_detect()
    service = deps.setup_service()

    real_stdout = _redirect_for_json_mode(ctx.json_mode)

    print("\nWelcome to kairix setup.\n")
    print("This will configure your knowledge base in a few steps.")
    print("You'll need: an LLM API key and a folder of documents.\n")

    # Step 0: use-case -> preset
    preset_key = _resolve_preset(ctx, preset)

    # Step 1: LLM backend — collect, then PERSIST. The connection test
    # runs later, against the just-written config + secrets (#474).
    print("Step 1 of 7: LLM Backend\n")
    picked, endpoint, api_key, embed_model, chat_model = _prompt_llm_credentials(ctx, deps.provider_names())
    if picked == "openai" and not endpoint:
        # OpenAI-direct: the plugin takes the endpoint verbatim, so the
        # persisted value must be the real base URL, not "".
        endpoint = _OPENAI_DEFAULT_ENDPOINT
    provider_plugin = picked_provider_plugin(picked, endpoint)
    secrets_path: Path | None = None
    if api_key:
        secrets_path = deps.persist_credentials(api_key, endpoint, embed_model, chat_model)
        if secrets_path is not None:
            print(f"  ✓ Credentials stored in {secrets_path} (0600)\n")

    # Step 2: document root — validated + sized by the backend's scan,
    # the same counts and token-priced estimate the web wizard shows.
    print("Step 2 of 7: Document Source\n")
    doc_root = _resolve_document_root(ctx, document_path, service)
    scan = service.scan_folder(doc_root)
    if not scan.ok:
        print(f"\n  Error: {scan.error}\n")
        return False
    print(f"\n  Found: {scan.files:,} documents (~{scan.words_estimate:,} words)\n")

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
    _print_agent_instructions(service)

    # Build config
    full_config = _build_full_config(
        template, provider_plugin, doc_root, db_path, log_dir, collections_config, use_neo4j, neo4j_uri
    )

    if ctx.json_mode:
        # JSON mode emits the config to stdout and skips file write +
        # subsequent steps that don't make sense in scripted bootstrap.
        _emit_json_config(real_stdout, full_config)
        return True

    # MERGE the wizard's answers into the config target — blocks the
    # wizard doesn't manage (topology, agents, …) survive a re-run.
    output = deps.write_config(full_config, output_path)
    print(f"  Config saved to: {output}\n")

    # Connection test — against the just-written config + the secrets
    # read back from the bundle (#474: the probe validates what later
    # commands will actually resolve, and it can pass on a fresh
    # machine because everything it needs is now on disk).
    endpoint_rb, api_key_rb = _read_back_credentials(secrets_path, endpoint, api_key)
    if not _confirm_llm_connection(ctx, service, provider_plugin, endpoint_rb, api_key_rb, embed_model):
        return False

    _maybe_run_initial_index(ctx, service, scan, poll_seconds=deps.index_poll_seconds)
    deps.health_check()
    _print_setup_summary(output, secrets_path)
    return True
