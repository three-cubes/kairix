"""Production backends for the web setup wizard's :class:`SetupService`.

:mod:`kairix.platform.setup.service` owns the contract (Protocol + frozen
DTOs); this module owns the side effects behind it. Every collaborator is
injected through :class:`SetupServiceDeps` — production callers construct
through :func:`kairix.platform.setup.service.build_setup_service` with no
arguments and the ``default_factory`` fields wire the real implementations
(lazy-imported at call time). Tests pass fakes at the seams BELOW the
service (fake provider, recorder persistence, tmp-path config files,
scripted index counters) so every branch is reachable without
monkey-patching (F1/F2-clean by construction).

Reused building blocks (one implementation per concern, per #474):

- credential persistence — :func:`kairix.platform.setup.wizard.persist_llm_credentials`
- config writing — :func:`kairix.platform.setup.wizard.write_config_yaml`
- azure plugin split — :func:`kairix.platform.setup.wizard.provider_plugin_name`
- credential probe — :func:`kairix.secrets.probe.llm_credentials_available`
- index run — :func:`kairix.core.embed.use_cases.run_incremental_embed_pipeline`
- index lock — the ``embed.lock`` flock next to the SQLite index, the same
  file :func:`kairix.core.embed.cli.acquire_lock` holds
- search — :func:`kairix.core.factory.build_search_pipeline` (F47 composition)
- handshake — :func:`kairix.agents.mcp.capability_probe.build_capability_probe`

F15: the operator's API key is never logged, and every error string built
from a provider exception is scrubbed of the key before it leaves this
module.
"""

from __future__ import annotations

import fcntl
import importlib.metadata
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from kairix.connect.protocols import ConnectError
from kairix.credentials import Credentials
from kairix.platform.setup.service import (
    PHASE_CONSENT,
    PHASE_DONE,
    PHASE_EXCHANGING,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_STARTING,
    PLUGIN_AZURE_FOUNDRY,
    PLUGIN_AZURE_LEGACY,
    AgentConnectInfo,
    CallbackOutcome,
    ConnectSnippet,
    FolderScan,
    HandshakeResult,
    IndexStatus,
    ProviderValidation,
    SavedSource,
    SearchPreview,
    SearchPreviewHit,
    SecretsWriteError,
    SetupStatus,
    SourceAuthStart,
    SourceAuthStatus,
    SourceHint,
    SourceOption,
    SourceUnit,
    SourceUnits,
    TourBrief,
    TourPrep,
    TourRememberRoundtrip,
    TourTimeline,
    TourTimelineHit,
)
from kairix.platform.setup.source_oauth import (
    DEFAULT_SOURCE_OPTIONS,
    OAUTH_SOURCE_PROVIDERS,
    PICKABLE_PROVIDERS,
    PROVIDER_GITHUB,
    PROVIDER_GMAIL,
    PROVIDER_GOOGLE_CALENDAR,
    PROVIDER_SLACK,
    CapturingBrowser,
    SourceFlowRequest,
    WizardCallbackListener,
    build_source_flow,
    source_secret_leaves,
    topology_updates_for_source,
)
from kairix.platform.setup.wizard import provider_plugin_name, write_config_yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: File suffixes the folder scan counts as indexable text documents.
SCAN_FILE_SUFFIXES = (".md", ".markdown", ".txt")

#: The scan stops counting at this many files so a mistyped path (e.g. "/")
#: can't hang the wizard. The DTO reports the capped count.
SCAN_MAX_FILES = 50_000

#: Word estimation is sample-based: at most this many files are read and
#: the per-file average is extrapolated across the full count.
SCAN_SAMPLE_FILES = 200

#: Embedding price in USD per 1K tokens. Matches the estimate the embed
#: pipeline itself reports (see ``kairix/core/embed/embed.py``) — the
#: text-embedding-3-large list price. One number, grade-8 plain: every
#: thousand tokens of your documents costs about 1/100th of a cent.
EMBED_COST_USD_PER_1K_TOKENS = 0.00013

#: Rough tokens-per-word ratio for English prose — used only for the
#: pre-index cost estimate, never for billing.
TOKENS_PER_WORD = 1.3

#: How many hits the first-search preview shows.
FIRST_SEARCH_TOP_N = 5

#: Fallback probe model when a plugin has no entry in
#: :data:`VALIDATION_PROBE_MODELS`.
DEFAULT_VALIDATION_PROBE_MODEL = "text-embedding-3-large"

#: Probe model per provider plugin for credential validation. The wizard
#: validates BEFORE the operator picks a model, so each plugin gets a
#: widely-available default; a successful authenticated round-trip with it
#: is the proof the key works. The Provider Protocol has no model-listing
#: surface, so the validated probe model is returned as the models tuple.
VALIDATION_PROBE_MODELS: Mapping[str, str] = {
    "anthropic": "claude-3-5-haiku-latest",
    "openai": DEFAULT_VALIDATION_PROBE_MODEL,
    PLUGIN_AZURE_FOUNDRY: DEFAULT_VALIDATION_PROBE_MODEL,
    PLUGIN_AZURE_LEGACY: DEFAULT_VALIDATION_PROBE_MODEL,
    "litellm_proxy": DEFAULT_VALIDATION_PROBE_MODEL,
    "ollama": "nomic-embed-text",
}

#: Default endpoint filled in when the operator leaves it blank. Mirrors
#: the terminal wizard's OpenAI-direct default.
DEFAULT_PLUGIN_ENDPOINTS: Mapping[str, str] = {
    "openai": "https://api.openai.com/v1",
}

#: Plugins that cannot be probed without an operator-supplied endpoint.
ENDPOINT_REQUIRED_PLUGINS = (PLUGIN_AZURE_FOUNDRY, PLUGIN_AZURE_LEGACY)

#: Replacement marker for any API-key occurrence in an error string (F15).
_REDACTED = "[redacted]"

#: Text embedded in the one-call validation probe.
_VALIDATION_PROBE_TEXT = "kairix setup validation"

#: Azure's error code when the resource has no deployment with the
#: requested name. The openai-compat SDK surfaces it inside the error
#: body, so substring detection on the provider's message is reliable.
_AZURE_DEPLOYMENT_NOT_FOUND_CODE = "DeploymentNotFound"

#: Shared reason prefix for a folder the wizard cannot use (F17 — three
#: rejection sites: container scan, bare-metal scan, save_source).
_FOLDER_NOT_FOUND_PREFIX = "Folder not found or not readable: "

#: Shared F21 tail for validation failures (F17 — one definition site).
_VALIDATION_FIX = (
    " fix: the message above is the provider's own error — it can name the"
    " endpoint, the model, or the network, so your key may be fine."
    " next: correct the failing value and validate again."
)

#: Shared F21 tail for indexing failures.
_INDEX_FIX = (
    " fix: check the provider credentials and endpoint, then retry."
    " next: kairix embed status shows what is still pending."
    " run: kairix embed"
)

#: Shared F21 tail for handshake failures.
_HANDSHAKE_FIX = (
    " fix: start the MCP server with kairix mcp serve and check its log. next: verify the connection again."
)

# ---------------------------------------------------------------------------
# Capability tour (#490) — operator-facing failure copy. Grade-8, F21-shaped
# (fix: / next:), and never a raw exception: the underlying error is logged,
# the screen gets guidance.
# ---------------------------------------------------------------------------

#: How many timeline hits the tour shows.
TOUR_TIMELINE_TOP_N = 5

#: Shared F21 tails/fixes (F17 — one definition site each).
_TOUR_RETRY_NEXT = " next: run it again."
_TOUR_PROVIDER_FIX = " fix: check the provider key and endpoint from the provider step."
_TOUR_AGENTS_BLOCK_FIX = " fix: add your agent's name to the agents: section of kairix.config.yaml."

_TOUR_PREP_FAILED = f"The context pack could not be built.{_TOUR_PROVIDER_FIX}{_TOUR_RETRY_NEXT}"
_TOUR_BRIEF_FAILED = f"The briefing could not be generated.{_TOUR_PROVIDER_FIX}{_TOUR_RETRY_NEXT}"
_TOUR_TIMELINE_FAILED = (
    f"The timeline lookup did not finish. fix: check that indexing finished on the indexing step.{_TOUR_RETRY_NEXT}"
)
_TOUR_REMEMBER_FAILED = (
    "The memory could not be saved. fix: check the documents folder from the source step is writable."
    f"{_TOUR_RETRY_NEXT}"
)
_TOUR_REMEMBER_NO_AGENT = (
    "The memory could not be saved because this knowledge store has no agent set up to own it."
    f"{_TOUR_AGENTS_BLOCK_FIX}{_TOUR_RETRY_NEXT}"
)
_TOUR_BRIEF_NO_AGENT = (
    "Briefings are written for a named agent, and this knowledge store doesn't have one set up yet."
    f"{_TOUR_AGENTS_BLOCK_FIX}"
    " next: once your agent is connected, ask it for a brief."
)

#: Prefix of the remember use case's invalid-agent rejection — the one
#: failure that needs agent-setup guidance instead of retry guidance.
_INVALID_AGENT_PREFIX = "InvalidAgent"
#: Shared F21 tail for source-connect failures (#489).
_SOURCE_RETRY = " next: go back to the source step and start the connection again."

#: Honest completion copy when a first-index run found nothing to index
#: (review M1). The wizard's indexing screen renders it instead of
#: spinning forever; ``FakeSetupService`` mirrors it for route tests.
EMPTY_INDEX_MESSAGE = (
    "0 documents indexed — the folder had no readable files."
    " fix: check you picked the right folder."
    " next: go back to the source step and pick a folder with documents in it."
)

# The source sign-in phase vocabulary (PHASE_IDLE … PHASE_FAILED) lives
# in kairix.platform.setup.service — the contract module — and is
# re-imported above (review M11). Re-exported via __all__ for existing
# importers of this module.


# ---------------------------------------------------------------------------
# Public building blocks (each independently testable through this surface)
# ---------------------------------------------------------------------------


def provider_from_credentials(
    plugin_name: str,
    credentials: Credentials,
    *,
    entry_points: Callable[..., Any] = importlib.metadata.entry_points,
) -> Any:
    """Construct a provider plugin against EXPLICIT credentials.

    The wizard validates keys the operator has typed but not yet saved, so
    the plugin must be built from the supplied values — never from the
    process environment (F2/F4). Every first-party plugin factory accepts
    a ``credentials_resolver`` seam; this passes a resolver that returns
    ``credentials`` regardless of purpose. Factories without the seam
    (e.g. bedrock, whose credentials ride the boto3 chain) are called
    bare.

    ``entry_points`` mirrors :class:`kairix.providers.EntryPointRegistry`'s
    constructor seam: production leaves the stdlib default; tests pass a
    fake callable.
    """
    import inspect

    from kairix.providers import ENTRY_POINT_GROUP, EntryPointRegistry, ProviderNotRegistered

    eps = list(entry_points(group=ENTRY_POINT_GROUP, name=plugin_name))
    if not eps:
        registry = EntryPointRegistry(entry_points=entry_points)
        raise ProviderNotRegistered(plugin_name, registry.available())
    factory = eps[0].load()
    if "credentials_resolver" in inspect.signature(factory).parameters:
        return factory(credentials_resolver=lambda _purpose: credentials)
    return factory()


def probe_provider_roundtrip(provider: Any) -> None:
    """Make one cheap authenticated call against ``provider``; raise on failure.

    Embed-capable plugins prove the credential with a one-text
    ``embed_batch``; chat-only plugins (Anthropic) raise
    ``EmbedNotSupported`` before any request goes out, and the probe
    falls back to a single tiny ``chat`` call. Any transport/auth failure
    propagates as the plugin's canonical typed error so the caller can
    surface the provider's message verbatim.
    """
    from kairix.providers import EmbedNotSupported

    try:
        vectors = provider.embed_batch([_VALIDATION_PROBE_TEXT])
    except EmbedNotSupported:
        reply = provider.chat(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
        )
        if not reply:
            raise RuntimeError("The provider accepted the request but returned an empty chat reply.") from None
        return
    if not vectors or not vectors[0]:
        raise RuntimeError("The provider accepted the request but returned an empty embedding.")


def _deep_coerce_mapping(value: Any) -> Any:
    """Recursively coerce ``Mapping`` values to plain dicts for merging."""
    if isinstance(value, Mapping):
        return {key: _deep_coerce_mapping(item) for key, item in value.items()}
    return value


def update_config_file(target: Path, updates: Mapping[str, Any]) -> Path:
    """Merge ``updates`` into the YAML config at ``target`` and write it back.

    Merging is fully recursive (#492 — :func:`kairix.config_layers.deep_merge`,
    the SAME semantics the layered read side applies): nested dict values
    merge key-by-key at every depth, so writing
    ``topology.credentials.slack`` preserves an existing
    ``topology.credentials.github`` sibling; lists and scalars are
    replaced. The write itself goes through the terminal wizard's
    :func:`write_config_yaml` so both setup surfaces emit one file shape.

    Legacy-key cleanup (#725): when the merged mapping declares the
    canonical :data:`~kairix.config.TOPOLOGY_CONFIG_KEY`, the pre-rename
    :data:`~kairix.config.LEGACY_TOPOLOGY_CONFIG_KEY` sibling is dropped.
    An operator's single-file config written before the PLA-287 rename
    carries the block under ``topology_v2:``; the wizard now emits the
    canonical ``topology:`` block (with the legacy sources already folded
    in by :func:`~kairix.platform.setup.source_oauth.topology_updates_for_source`),
    and a raw deep-merge would leave BOTH keys in the file — a dead
    duplicate block plus a foot-gun where hand-edits to the still-visible
    legacy block are silently ignored on read. Dropping the legacy key
    once the canonical key is present keeps the operator's file clean;
    the sources survive because they are carried in the canonical block.
    """
    import yaml

    from kairix.config import LEGACY_TOPOLOGY_CONFIG_KEY, TOPOLOGY_CONFIG_KEY
    from kairix.config_layers import deep_merge

    existing: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    merged = deep_merge(existing, _deep_coerce_mapping(updates))
    if TOPOLOGY_CONFIG_KEY in merged:
        merged.pop(LEGACY_TOPOLOGY_CONFIG_KEY, None)
    return write_config_yaml(target, "setup-wizard", merged)


def wizard_config_target(
    overlay_path: str | None,
    config_path: str | None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The ONE config file the wizard reads AND writes (#492).

    Resolution order:

    1. ``overlay_path`` (``KAIRIX_CONFIG_OVERLAY_PATH``) — the shipped
       compose: saves land on the writable overlay, the read-only base
       stays pristine, and every layered reader merges them.
    2. ``config_path`` (``KAIRIX_CONFIG_PATH``) — legacy single-file.
    3. An existing ``./kairix.config.yaml`` — cwd legacy fallback, so
       installs that already keep their config next to the process keep
       updating that file.
    4. ``$XDG_CONFIG_HOME/kairix/kairix.config.yaml`` (fallback
       ``~/.config/kairix/...``) — the pip-install default, the same
       location ``kairix init`` writes and the layered read side probes.

    Used by BOTH :func:`write_config_updates` and
    :func:`read_config_mapping` so the wizard's read-modify-write cycle
    can never split across two files. ``env`` / ``home`` mirror the
    secrets-store test seams.
    """
    if overlay_path:
        return Path(overlay_path).expanduser()
    if config_path:
        return Path(config_path).expanduser()
    cwd_candidate = Path("kairix.config.yaml")
    if cwd_candidate.is_file():
        return cwd_candidate
    from kairix.config_layers import user_config_path

    return user_config_path(env=dict(env) if env is not None else None, home=home)


def write_config_updates(
    updates: Mapping[str, Any],
    *,
    overlay_path: str | None,
    config_path: str | None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Merge ``updates`` into the right config file — overlay-aware (#485).

    When an overlay path is configured (``KAIRIX_CONFIG_OVERLAY_PATH``),
    wizard saves land on the OVERLAY file — the operator's base config
    (read-only-mounted in the stock compose) stays pristine, and the
    layered readers (:func:`kairix.core.search.config_loader.load_config`,
    :func:`kairix.paths.load_top_level_config`, the worker's topology
    boot apply) deep-merge the overlay on top of the base at read time.
    Parent directories are created so a first save on a fresh data
    volume (or a fresh ``~/.config/kairix/``) works.

    Without an overlay, the single-file behaviour applies — see
    :func:`wizard_config_target` for the full resolution order,
    including the pip-install XDG default (#492).
    """
    target = wizard_config_target(overlay_path, config_path, env=env, home=home)
    target.parent.mkdir(parents=True, exist_ok=True)
    return update_config_file(target, updates)


def configured_document_root(
    *,
    override: str | None,
    config_paths: Mapping[str, str],
) -> Path | None:
    """Resolve the EXPLICITLY configured document root, or ``None``.

    The wizard's "source" step is done only when the operator has chosen
    a folder — an env override (``KAIRIX_DOCUMENT_ROOT``) or a config
    ``paths.document_root`` entry. The platform's silent fallback default
    deliberately does NOT count: an unconfigured install must show the
    folder step as pending.
    """
    if override:
        return Path(override).expanduser()
    configured = config_paths.get("document_root")
    if configured:
        return Path(configured).expanduser()
    return None


def count_index_chunks(db_path: Path) -> tuple[int, int]:
    """Return ``(chunks_embedded, chunks_pending)`` from the index database.

    ``chunks_embedded`` counts ``content_vectors`` rows; ``chunks_pending``
    counts documents still awaiting their first embedding, via the same
    :func:`kairix.core.embed.schema.get_pending_chunks` query ``kairix
    embed status`` uses. A missing or not-yet-initialised database reads
    as ``(0, 0)`` — "nothing indexed yet", which is the truthful first-run
    answer, not an error.

    The connection opens through :func:`kairix.core.db.open_db` (F77 —
    one writer-coordinated open site) and is read-only in practice: this
    function only ever issues SELECTs.
    """
    if not db_path.exists():
        return (0, 0)
    from kairix.core.db import open_db
    from kairix.core.embed.schema import get_pending_chunks

    try:
        db = open_db(db_path)
    except sqlite3.Error:
        return (0, 0)
    try:
        embedded = int(db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0])
        pending = len(get_pending_chunks(db))
    except sqlite3.Error:
        return (0, 0)
    finally:
        db.close()
    return (embedded, pending)


def embed_lock_held(lockfile: Path) -> bool:
    """True when another process currently holds the embed flock.

    Probes the same lockfile :func:`kairix.core.embed.cli.acquire_lock`
    takes (``embed.lock`` beside the SQLite index) with a non-blocking
    ``LOCK_EX`` attempt, releasing immediately on success. Opened in
    append mode so the probe never truncates the holder's recorded pid.
    """
    if not lockfile.exists():
        return False
    with open(lockfile, "a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def _default_embed_pipeline(**kwargs: Any) -> Any:
    from kairix.core.embed.use_cases import run_incremental_embed_pipeline

    return run_incremental_embed_pipeline(**kwargs)


def run_first_index(*, pipeline_fn: Callable[..., Any] = _default_embed_pipeline) -> None:
    """Run one full embed pass; raise with an F21 affordance on failed chunks.

    Delegates to the canonical incremental embed pipeline (which scans the
    document root, rebuilds FTS, embeds pending chunks, and holds the
    embed lock for the duration). The recall gate is skipped — there is no
    baseline to regress against on a first index. ``pipeline_fn`` carries
    the production default (per F6, a real callable — never ``None``);
    tests pass a recorder returning a scripted result.
    """
    result = pipeline_fn(skip_recall_check=True)
    failed = int(getattr(result, "failed", 0))
    if failed:
        raise RuntimeError(f"{failed} chunks failed to embed.{_INDEX_FIX}")


# ---------------------------------------------------------------------------
# Production default seams (lazy-import delegation — wired by SetupServiceDeps)
# ---------------------------------------------------------------------------


def _default_persist_credentials(
    api_key: str,
    endpoint: str,
    embed_model: str,
) -> Path | None:
    from kairix.platform.setup.wizard import persist_llm_credentials

    return persist_llm_credentials(api_key, endpoint, embed_model)


def _default_credentials_probe() -> bool:
    from kairix.secrets.probe import llm_credentials_available

    return llm_credentials_available()


def _default_configured_document_root() -> Path | None:
    from kairix.paths import document_root_override, load_paths_from_config

    return configured_document_root(
        override=document_root_override(),
        config_paths=load_paths_from_config(),
    )


# Lazy-import DI-default delegation — the write-target resolution reads
# KAIRIX_CONFIG_OVERLAY_PATH / KAIRIX_CONFIG_PATH through kairix.paths
# (F4); the merge logic lives in write_config_updates. Executed by
# tests/integration/test_setup_service_default_seams.py (F86).
def _default_write_config(updates: Mapping[str, Any]) -> Path:
    from kairix.paths import config_overlay_path_override, config_path_override

    return write_config_updates(
        updates,
        overlay_path=config_overlay_path_override(),
        config_path=config_path_override(),
    )


# Lazy-import DI-default delegation — mirrors _default_write_config; the
# resolution logic lives in wizard_config_target.
def _default_config_target() -> Path:
    from kairix.paths import config_overlay_path_override, config_path_override

    return wizard_config_target(
        config_overlay_path_override(),
        config_path_override(),
    )


def _default_search_pipeline(paths: Any) -> Any:
    from kairix.core.factory import build_search_pipeline

    return build_search_pipeline(paths=paths)


def _default_capability_probe() -> Mapping[str, Any]:
    from kairix.agents.mcp.capability_probe import build_capability_probe

    return build_capability_probe()()


# Lazy-import DI-default delegation — builds the real FastMCP server
# in-process, so it is executed at the integration tier (F86), never unit.
def _default_tools_count() -> int:
    import asyncio

    from kairix.agents.mcp.server import build_server

    server = build_server()
    return len(asyncio.run(server.list_tools()))


def _default_run_prep(query: str) -> Any:
    from kairix.use_cases.prep import run_prep

    return run_prep(query)


def _default_remember(agent: str, content: str) -> Any:
    from kairix.use_cases.remember import remember

    return remember(agent, content)


def _default_run_brief(agent: str) -> Any:
    from kairix.use_cases.brief import run_brief

    return run_brief(agent)


def _default_run_timeline(query: str) -> Any:
    from kairix.use_cases.timeline import run_timeline

    return run_timeline(query)


def _default_top_level_config() -> dict[str, Any] | None:
    from kairix.paths import load_top_level_config

    return load_top_level_config()


def _default_listener_factory(origin: str, expected_state: str | None) -> Any:
    """Production listener — fulfilled by the wizard's callback route (#489)."""
    return WizardCallbackListener(origin=origin, expected_state=expected_state)


def _default_persist_secret(name: str, value: str) -> Any:
    from kairix.secrets.store import set_secret

    return set_secret(name, value)


def _default_discover_units(
    provider: str,
    client: Any,
    tokens: Any,
) -> tuple[SourceUnit, ...]:
    from kairix.platform.setup.source_oauth import discover_source_units_live

    return discover_source_units_live(provider, client, tokens)


def read_config_mapping(
    *,
    overlay_path: str | None,
    config_path: str | None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Read the wizard's write-target config file as a plain mapping.

    Resolves through :func:`wizard_config_target` — the SAME helper
    :func:`write_config_updates` uses (#492) — so the topology upsert in
    :meth:`KairixSetupService.save_oauth_source` merges into the SAME
    file it writes back to. A missing or non-mapping file reads as
    empty — the truthful fresh-install answer.
    """
    import yaml

    target = wizard_config_target(overlay_path, config_path, env=env, home=home)
    if not target.exists():
        return {}
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


# Lazy-import DI-default delegation — the read-target resolution reads
# KAIRIX_CONFIG_OVERLAY_PATH / KAIRIX_CONFIG_PATH through kairix.paths
# (F4); the read logic lives in read_config_mapping.
def _default_read_config() -> Mapping[str, Any]:
    from kairix.paths import config_overlay_path_override, config_path_override

    return read_config_mapping(
        overlay_path=config_overlay_path_override(),
        config_path=config_path_override(),
    )


@dataclass
class SetupServiceDeps:
    """Injectable collaborators for :class:`KairixSetupService`.

    Production callers omit every field — the ``default_factory`` wiring
    lazy-imports the real implementation at call time (the EmbedCliDeps /
    WizardDeps pattern). Tests construct ``SetupServiceDeps(...)`` with
    fakes for exactly the seams a scenario drives.
    """

    # validate_provider — plugin construction from explicit credentials.
    provider_factory: Callable[[str, Credentials], Any] = field(
        default_factory=lambda: provider_from_credentials,
    )
    # save_provider — W1-D's canonical persistence (api_key, endpoint, model).
    persist_credentials_fn: Callable[[str, str, str], Path | None] = field(
        default_factory=lambda: _default_persist_credentials,
    )
    # status — is an LLM credential resolvable through the loader chain?
    credentials_probe: Callable[[], bool] = field(
        default_factory=lambda: _default_credentials_probe,
    )
    # status — the explicitly configured document root, or None.
    configured_document_root_fn: Callable[[], Path | None] = field(
        default_factory=lambda: _default_configured_document_root,
    )
    # save_provider / save_source — merge updates into the runtime config file.
    write_config_fn: Callable[[Mapping[str, Any]], Path] = field(
        default_factory=lambda: _default_write_config,
    )
    # config_file_path — the file wizard saves land in (#492); shown on
    # the save/done screens so operators can verify where config lives.
    config_target_fn: Callable[[], Path] = field(
        default_factory=lambda: _default_config_target,
    )
    # status / index_status — (embedded, pending) chunk counters per db path.
    index_counts_fn: Callable[[Path], tuple[int, int]] = field(
        default_factory=lambda: count_index_chunks,
    )
    # start_index / index_status — is the embed flock held by another process?
    embed_lock_probe_fn: Callable[[Path], bool] = field(
        default_factory=lambda: embed_lock_held,
    )
    # start_index — the actual first-index run (background thread target).
    index_runner_fn: Callable[[], None] = field(
        default_factory=lambda: run_first_index,
    )
    # first_search — factory-built pipeline exposing .search(query).
    search_pipeline_factory: Callable[[Any], Any] = field(
        default_factory=lambda: _default_search_pipeline,
    )
    # verify_agent_handshake — layered capability flags.
    capability_probe_fn: Callable[[], Mapping[str, Any]] = field(
        default_factory=lambda: _default_capability_probe,
    )
    # verify_agent_handshake — registered MCP tool count, in-process.
    tools_count_fn: Callable[[], int] = field(
        default_factory=lambda: _default_tools_count,
    )
    # agent_connect_info — env mapping for KAIRIX_MCP_ENDPOINT resolution.
    # None means os.environ (read inside kairix.paths per F4).
    environ: Mapping[str, str] | None = None
    # ── Capability tour (#490) — one seam per sample run ──────────────
    # tour_prep — the prep use case (search + one grounded LLM call).
    prep_fn: Callable[[str], Any] = field(default_factory=lambda: _default_run_prep)
    # tour_remember_roundtrip — the remember use case (write + immediate index).
    remember_fn: Callable[[str, str], Any] = field(default_factory=lambda: _default_remember)
    # tour_brief — the brief use case (session briefing synthesis).
    brief_fn: Callable[[str], Any] = field(default_factory=lambda: _default_run_brief)
    # tour_timeline — the timeline use case (date-aware retrieval).
    timeline_fn: Callable[[str], Any] = field(default_factory=lambda: _default_run_timeline)
    # tour agent resolution — parsed top-level config (the agents: block).
    top_level_config_fn: Callable[[], dict[str, Any] | None] = field(
        default_factory=lambda: _default_top_level_config,
    )
    # tour_remember_roundtrip — monotonic clock for the round-trip timing.
    clock_fn: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    # start_source_auth (#489) — provider + typed fields → connect flow.
    oauth_flow_factory: Callable[[SourceFlowRequest], Any] = field(
        default_factory=lambda: build_source_flow,
    )
    # start_source_auth — (origin, expected_state) → CallbackListener.
    listener_factory: Callable[[str, str | None], Any] = field(
        default_factory=lambda: _default_listener_factory,
    )
    # save_oauth_source / _source_auth_worker — persist one canonical secret.
    persist_secret_fn: Callable[[str, str], Any] = field(
        default_factory=lambda: _default_persist_secret,
    )
    # discover_source_units — (provider, client, tokens) → picker rows.
    discover_units_fn: Callable[[str, Any, Any], tuple[SourceUnit, ...]] = field(
        default_factory=lambda: _default_discover_units,
    )
    # save_oauth_source — current content of the wizard's config target.
    read_config_fn: Callable[[], Mapping[str, Any]] = field(
        default_factory=lambda: _default_read_config,
    )
    # source_options — the cards on the source step.
    source_options_fn: Callable[[], tuple[SourceOption, ...]] = field(
        default_factory=lambda: lambda: DEFAULT_SOURCE_OPTIONS,
    )


@dataclass
class _SourceAuthState:
    """Mutable per-flow state for one source sign-in (#489).

    The single-slot pending-flow registry: the service holds at most
    ONE of these (the wizard is a single-operator tool). A replacing
    ``start_source_auth`` swaps the pointer; a stale worker thread
    keeps writing to ITS state object, which nothing reads any more.
    All field mutation happens under the service's source lock.
    """

    provider: str
    instance: str
    expected_state: str | None
    listener: Any
    browser: Any
    callback_delivered: bool = False
    done: bool = False
    error: str | None = None
    tokens: Any = None
    client: Any = None


class KairixSetupService:
    """Production :class:`kairix.platform.setup.service.SetupService`.

    Construct through
    :func:`kairix.platform.setup.service.build_setup_service` — the seams
    live on :class:`SetupServiceDeps`; ``paths`` pins the index database
    (and therefore the embed lockfile and the search pipeline) to an
    explicit location, with ``None`` meaning the platform resolution
    chain.
    """

    def __init__(self, *, paths: Any = None, deps: SetupServiceDeps | None = None) -> None:
        self._paths = paths
        self._deps = deps if deps is not None else SetupServiceDeps()
        self._index_state_lock = threading.Lock()
        self._index_thread: threading.Thread | None = None
        self._index_error: str | None = None
        # Source OAuth single-slot registry (#489).
        self._source_lock = threading.Lock()
        self._source_state: _SourceAuthState | None = None
        self._source_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Internal path resolution
    # ------------------------------------------------------------------

    def _db_path(self) -> Path:
        """Index database path — injected ``paths`` or the platform chain."""
        if self._paths is not None:
            return Path(self._paths.db_path)
        from kairix.paths import db_path

        return db_path()

    def _lockfile(self) -> Path:
        """The embed flock lives beside the SQLite index — the same
        ``embed.lock`` path :func:`kairix.core.embed.cli.acquire_lock` uses."""
        return self._db_path().parent / "embed.lock"

    # ------------------------------------------------------------------
    # SetupService Protocol
    # ------------------------------------------------------------------

    def status(self) -> SetupStatus:
        """Derive step completion from what actually exists on this host."""
        provider_done = bool(self._deps.credentials_probe())
        root = self._deps.configured_document_root_fn()
        source_done = root is not None and root.is_dir()
        embedded, _pending = self._deps.index_counts_fn(self._db_path())
        return SetupStatus(
            provider_done=provider_done,
            source_done=source_done,
            index_done=embedded > 0,
        )

    def validate_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        deployment: str | None = None,
    ) -> ProviderValidation:
        """One authenticated round-trip against the SUPPLIED credentials.

        Nothing is persisted and the process environment is never touched
        (F2/F4) — the plugin is constructed against the typed-in values.
        Failures carry the provider's error verbatim (key scrubbed, F15)
        plus an F21 affordance. The Provider Protocol exposes no model
        listing, so success returns the probe model as the models tuple.

        ``deployment`` (#484): Azure routes requests by deployment name,
        so when the operator supplies one it replaces the per-plugin
        probe-model default. A ``DeploymentNotFound`` failure is
        reported as "your key works, this deployment name doesn't" —
        not as a bad key.
        """
        plugin = _normalise_plugin_name(provider, endpoint)
        resolved_endpoint = (endpoint or "").strip() or DEFAULT_PLUGIN_ENDPOINTS.get(plugin, "")
        if plugin in ENDPOINT_REQUIRED_PLUGINS and not resolved_endpoint:
            return ProviderValidation(
                ok=False,
                models=(),
                error=(
                    f"The {plugin} provider needs an endpoint before the key can be checked."
                    " fix: paste your resource endpoint"
                    " (it looks like https://<resource>.services.ai.azure.com)."
                    " next: validate again."
                ),
            )
        model = (deployment or "").strip() or VALIDATION_PROBE_MODELS.get(plugin, DEFAULT_VALIDATION_PROBE_MODEL)
        credentials = Credentials(api_key=api_key, endpoint=resolved_endpoint, model=model)
        try:
            plugin_obj = self._deps.provider_factory(plugin, credentials)
            probe_provider_roundtrip(plugin_obj)
        except Exception as exc:
            message = _scrub_secret(str(exc), api_key)
            if _AZURE_DEPLOYMENT_NOT_FOUND_CODE in message:
                return ProviderValidation(
                    ok=False,
                    models=(),
                    error=_deployment_missing_error(model),
                    deployment_missing=True,
                )
            return ProviderValidation(ok=False, models=(), error=f"{message}{_VALIDATION_FIX}")
        return ProviderValidation(ok=True, models=(model,), error=None)

    def save_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        model: str | None,
        deployment: str | None = None,
    ) -> None:
        """Persist the validated selection through W1-D's canonical path.

        The credential lands in the operator secrets bundle via
        ``persist_llm_credentials`` (the same upsert ``kairix secrets set``
        uses) and the chosen plugin name lands in the config's
        ``provider:`` field — #474's headline defect was a wizard that
        asked and then didn't write it. When no model was chosen, the
        Azure ``deployment`` name (#484) fills the embed-model slot so
        indexing talks to the same deployment that validated.

        A failed credential write raises :class:`SecretsWriteError`
        naming the bundle path (review M2) — distinct from the config
        write's plain ``OSError`` so the wizard prescribes the
        ``KAIRIX_SECRETS_FILE`` rescue, not the config overlay. The
        config write never runs after a failed persist.
        """
        plugin = _normalise_plugin_name(provider, endpoint)
        resolved_endpoint = (endpoint or "").strip() or DEFAULT_PLUGIN_ENDPOINTS.get(plugin, "")
        resolved_model = (model or "").strip() or (deployment or "").strip()
        try:
            self._deps.persist_credentials_fn(api_key, resolved_endpoint, resolved_model)
        except OSError as exc:
            raise SecretsWriteError(str(getattr(exc, "filename", "") or "")) from exc
        self._deps.write_config_fn({"provider": plugin})

    def scan_folder(self, path: str) -> FolderScan:
        """Walk ``path`` and estimate indexing size and cost.

        Counts markdown/text files (capped at :data:`SCAN_MAX_FILES`),
        reads at most :data:`SCAN_SAMPLE_FILES` of them to estimate words,
        and prices the extrapolated token count at
        :data:`EMBED_COST_USD_PER_1K_TOKENS`.

        Relative paths are rejected with a message naming the resolution
        base instead of silently joining the server's working directory
        (#486); inside a container, a not-found error points at the
        mounted document root as the candidate to try.
        """
        candidate = (path or "").strip()
        if not candidate:
            return _failed_scan("No folder path was provided.")
        folder = Path(candidate).expanduser()
        if not folder.is_absolute():
            return _failed_scan(
                _relative_path_reason(candidate),
                fix="enter the full path, starting with / (or ~ for your home folder).",
            )
        if not folder.is_dir():
            mounted = self.source_hint().suggested_path
            if mounted:
                return _failed_scan(
                    f"{_FOLDER_NOT_FOUND_PREFIX}{folder}.",
                    fix=(
                        "this kairix runs in Docker, so it only sees folders mounted"
                        f" into the container — the standard compose mounts your documents at {mounted}."
                    ),
                    next_=f"try {mounted}, then scan once more.",
                )
            return _failed_scan(f"{_FOLDER_NOT_FOUND_PREFIX}{folder}.")
        files, words_estimate = _scan_text_files(folder)
        cost = (words_estimate * TOKENS_PER_WORD / 1000.0) * EMBED_COST_USD_PER_1K_TOKENS
        return FolderScan(
            ok=True,
            files=files,
            words_estimate=words_estimate,
            cost_estimate_usd=round(cost, 4),
            error=None,
        )

    def save_source(self, path: str) -> None:
        """Persist the chosen folder as ``paths.document_root`` in the config.

        Raises:
            ValueError: when the path is relative, the folder does not
                exist, or the pick is shadowed by a ``KAIRIX_DOCUMENT_ROOT``
                env override (#492) — the wizard's scan step gates the
                happy path, so reaching here with a bad path is a hard
                reject, not a silent write against the server's working
                directory (or a save the runtime would silently ignore).
        """
        candidate = (path or "").strip()
        folder = Path(candidate).expanduser()
        if not folder.is_absolute():
            raise ValueError(
                f"{_relative_path_reason(candidate)}"
                " fix: enter the full path, starting with / (or ~ for your home folder)."
                " next: scan the folder again, then save."
            )
        if not folder.is_dir():
            raise ValueError(
                f"{_FOLDER_NOT_FOUND_PREFIX}{folder}."
                " fix: create the folder (or fix the spelling), then scan it again."
                " next: the scan step confirms the folder before it is saved."
            )
        from kairix.paths import document_root_override

        override = document_root_override(self._deps.environ)
        if override and Path(override).expanduser() != folder:
            raise ValueError(
                f"This install reads its document folder from the KAIRIX_DOCUMENT_ROOT"
                f" environment variable ({override}), which overrides anything saved here —"
                f" the pick of {folder} would be silently ignored at runtime."
                f" fix: pick {override}, or change KAIRIX_DOCUMENT_ROOT in your deployment's"
                f" environment (the .env file for Docker compose) and restart kairix."
                " next: scan the folder again, then save."
            )
        self._deps.write_config_fn({"paths": {"document_root": str(folder)}})
        # The platform paths resolution is cached per process — drop it so
        # the very next resolve (the index run this wizard kicks off) sees
        # the folder that was just saved.
        from kairix.paths import clear_cache

        clear_cache()

    def config_file_path(self) -> str:
        """The config file wizard saves land in (#492).

        Resolution lives in :func:`wizard_config_target` (one helper for
        the read AND write side); the env reads stay behind
        ``kairix.paths`` accessors (F4) inside the deps default. Shown
        on the save/done screens so the operator knows where their
        configuration lives — and which file to bring to a new machine.
        """
        return str(self._deps.config_target_fn())

    def source_hint(self) -> SourceHint:
        """Container-aware pre-fill for the folder step (#486).

        Inside a container the only folders kairix can see are the ones
        the operator mounted, so the folder field pre-fills with the
        configured document root (stock compose: ``/data/documents``).
        The env read lives in :func:`kairix.paths.container_source_prefill`
        (F4); ``deps.environ`` is the F2-clean test seam.
        """
        from kairix.paths import container_source_prefill

        prefill = container_source_prefill(self._deps.environ)
        return SourceHint(in_container=prefill is not None, suggested_path=prefill or "")

    def start_index(self) -> None:
        """Kick off the first index run in a background thread.

        Idempotent: a second click while a run is in flight is a no-op.
        When another process (the embed worker, a terminal ``kairix
        embed``) already holds the embed flock, no thread is spawned —
        ``index_status`` reports that run truthfully as the indexer.
        """
        with self._index_state_lock:
            if self._index_thread is not None and self._index_thread.is_alive():
                return
            if self._deps.embed_lock_probe_fn(self._lockfile()):
                return
            self._index_error = None
            thread = threading.Thread(target=self._index_worker, name="setup-wizard-index", daemon=True)
            self._index_thread = thread
            thread.start()

    def _index_worker(self) -> None:
        """Background-thread body — records every failure as an operator message.

        ``SystemExit`` is recorded and then re-raised (Sonar S5754): in a
        worker thread the re-raise is harmless — ``threading`` silently
        swallows ``SystemExit`` from non-main threads — so the interpreter
        keeps running and ``index_status`` reports the recorded message.
        """
        try:
            self._deps.index_runner_fn()
        except SystemExit:  # NOSONAR(python:S5754) — deliberate; see rationale below.
            # acquire_lock exhausts its wait window with sys.exit(3) when a
            # concurrent embed genuinely holds the lock the whole time.
            # Re-raising inside this daemon thread would kill only the
            # thread, not "stop the application" — so we convert it to
            # the operator-facing status the wizard polls instead.
            with self._index_state_lock:
                self._index_error = (
                    "Indexing stopped: another indexing run is already in progress."
                    " fix: wait for it to finish, then start again."
                    " next: kairix worker status shows the active phase."
                )
            raise
        except Exception as exc:
            with self._index_state_lock:
                self._index_error = f"Indexing stopped: {exc}{_INDEX_FIX}"

    def index_status(self) -> IndexStatus:
        """Progress snapshot from the index database's own counters.

        Progress is read from the ``content_vectors`` / pending-documents
        tables rather than a live in-pipeline counter — the embed pipeline
        commits per batch, so the DB count IS the ground truth, and it
        works identically whether this service's thread or the embed
        worker is doing the indexing. ``chunks_total`` is embedded +
        pending documents, so it grows as the scan chunkifies new files.

        An empty corpus completes honestly (review M1): a run that
        finished cleanly with nothing embedded reports ``done=True``
        with :data:`EMPTY_INDEX_MESSAGE` in ``error``, so the indexing
        screen stops polling and explains instead of spinning forever.
        The message rides ``error`` deliberately — it keeps the
        screen's auto-advance from skipping past the explanation.
        """
        with self._index_state_lock:
            thread_alive = self._index_thread is not None and self._index_thread.is_alive()
            ran_to_completion = self._index_thread is not None and not thread_alive
            error = self._index_error
        embedded, pending = self._deps.index_counts_fn(self._db_path())
        external_run = not thread_alive and self._deps.embed_lock_probe_fn(self._lockfile())
        running = thread_alive or external_run
        finished_clean = not running and error is None and pending == 0
        done = finished_clean and (embedded > 0 or ran_to_completion)
        if done and embedded == 0:
            error = EMPTY_INDEX_MESSAGE
        return IndexStatus(
            running=running,
            done=done,
            chunks_done=embedded,
            chunks_total=embedded + pending,
            error=error,
        )

    def first_search(self, query: str) -> SearchPreview:
        """Top-5 hits from the factory-built production pipeline.

        Scores are normalised relative to the best hit (top hit = 1.0) so
        the preview's percentage reads as "how close to the best match",
        which is meaningful across fusion strategies. Any pipeline failure
        returns an empty preview — the screen's empty state guides the
        operator; a stack trace does not.
        """
        try:
            pipeline = self._deps.search_pipeline_factory(self._paths)
            result = pipeline.search(query)
            rows = list(getattr(result, "results", ()))
        except Exception:
            logger.warning("setup wizard: first-search pipeline failed", exc_info=True)
            return SearchPreview(results=())
        return SearchPreview(results=_to_preview_hits(rows))

    def agent_connect_info(self) -> AgentConnectInfo:
        """MCP URL + copy-paste snippets, shapes from connecting-agents.md.

        Includes a stdio variant (Claude Desktop's
        ``claude_desktop_config.json`` shape) so the terminal wizard —
        which renders these same snippets — covers agents that launch
        kairix as a subprocess instead of connecting over HTTP.
        """
        from kairix.paths import mcp_endpoint

        url = mcp_endpoint(environ=self._deps.environ)
        claude_code = {"mcpServers": {"kairix": {"type": "http", "url": url}}}
        claude_desktop = {"mcpServers": {"kairix": {"command": "kairix", "args": ["mcp", "serve"]}}}
        openclaw = {
            "mcp": {
                "servers": {
                    "mcp-kairix": {
                        "command": "kairix",
                        "args": ["mcp", "serve"],
                        "description": "Knowledge base search, research, entity lookup",
                    }
                }
            }
        }
        return AgentConnectInfo(
            mcp_url=url,
            snippets=(
                ConnectSnippet(client="Claude Code (.mcp.json)", config_text=json.dumps(claude_code, indent=2)),
                ConnectSnippet(
                    client="Claude Desktop (claude_desktop_config.json, stdio)",
                    config_text=json.dumps(claude_desktop, indent=2),
                ),
                ConnectSnippet(client="OpenClaw (openclaw.json)", config_text=json.dumps(openclaw, indent=2)),
                ConnectSnippet(client="Generic MCP over HTTP", config_text=url),
            ),
        )

    def verify_agent_handshake(self) -> HandshakeResult:
        """In-process handshake proof: capability probe + registered tool count.

        No HTTP self-call — the wizard runs inside the same process as
        the MCP transport, so the layered capability probe plus the
        actual registered-tool count is the honest signal an agent
        connecting right now would observe.
        """
        try:
            probe = self._deps.capability_probe_fn()
            tools = int(self._deps.tools_count_fn())
        except Exception as exc:
            return HandshakeResult(ok=False, tools_count=0, error=f"MCP handshake check failed: {exc}.{_HANDSHAKE_FIX}")
        if tools <= 0:
            return HandshakeResult(
                ok=False,
                tools_count=0,
                error=f"The MCP server registered no tools.{_HANDSHAKE_FIX}",
            )
        if not probe.get("secrets_loaded", False):
            detail = _probe_detail(probe, "secrets_loaded")
            return HandshakeResult(
                ok=False,
                tools_count=tools,
                error=(
                    f"Agents can connect, but the provider credentials are not loaded: {detail}."
                    " fix: finish the provider step so search returns real results."
                    " next: verify the connection again."
                ),
            )
        return HandshakeResult(ok=True, tools_count=tools, error=None)

    # ------------------------------------------------------------------
    # Capability tour (#490) — thin passthroughs to the canonical use
    # cases. Each catches every failure and returns guidance in the
    # DTO's ``message`` field (the first_search pattern: the screen
    # renders copy, never a stack trace).
    # ------------------------------------------------------------------

    def _tour_agent(self) -> str:
        """The agent the tour writes/briefs as: first configured, else shared.

        ``tour_agent_from_config`` reads the config's ``agents:`` block
        in declaration order; an install with no configured agents falls
        back to the legacy built-in shared agent, which the
        config-driven allowlist (#472) always accepts — so the
        write-then-find loop works on a fresh install out of the box.
        """
        from kairix.core.classify.router import SHARED_AGENT

        return tour_agent_from_config(self._deps.top_level_config_fn()) or SHARED_AGENT

    def tour_prep(self, query: str) -> TourPrep:
        """One real ``prep`` run: retrieval + a single grounded LLM call."""
        try:
            out = self._deps.prep_fn(query)
        except Exception:
            logger.warning("setup wizard: tour prep failed", exc_info=True)
            return TourPrep(summary="", sources=(), message=_TOUR_PREP_FAILED)
        error = str(getattr(out, "error", "") or "")
        if error:
            logger.warning("setup wizard: tour prep reported an error — %s", error)
            return TourPrep(summary="", sources=(), message=_TOUR_PREP_FAILED)
        return TourPrep(
            summary=str(getattr(out, "summary", "") or ""),
            # PLA-274 — prep sources are now resolvable ``SourceRef`` breadcrumbs;
            # surface each one's canonical ``source_uri`` (falls back to path) so
            # the tour shows a re-openable pointer, not a dataclass repr. A plain
            # string source (legacy) passes through ``str(s)`` unchanged.
            sources=tuple(str(getattr(s, "source_uri", None) or s) for s in getattr(out, "sources", ()) or ()),
            message="",
        )

    def tour_remember_roundtrip(self, content: str) -> TourRememberRoundtrip:
        """Write a memory, then run the search leg to show it coming back.

        The find leg goes through :meth:`first_search` — the same
        passthrough the search card uses — and ``found`` is True only
        when one of the returned hits is the just-written file, so the
        screen's "found by search" claim is backed by an actual search.
        """
        agent = self._tour_agent()
        started = self._deps.clock_fn()
        try:
            result = self._deps.remember_fn(agent, content)
        except Exception:
            logger.warning("setup wizard: tour remember failed", exc_info=True)
            return _failed_roundtrip(agent, _TOUR_REMEMBER_FAILED)
        error = str(getattr(result, "error", "") or "")
        if error:
            logger.warning("setup wizard: tour remember rejected — %s", error)
            message = _TOUR_REMEMBER_NO_AGENT if error.startswith(_INVALID_AGENT_PREFIX) else _TOUR_REMEMBER_FAILED
            return _failed_roundtrip(agent, message)
        preview = self.first_search(content)
        elapsed_ms = max(0, int((self._deps.clock_fn() - started) * 1000))
        memory_name = Path(str(getattr(result, "path", ""))).name
        found = bool(memory_name) and any(memory_name in hit.source for hit in preview.results)
        return TourRememberRoundtrip(
            saved=True,
            agent=agent,
            path=str(getattr(result, "path", "")),
            found=found,
            elapsed_ms=elapsed_ms,
            hits=preview.results,
            message="",
        )

    def tour_brief(self) -> TourBrief:
        """One real ``brief`` run; an empty preview is reported honestly."""
        agent = self._tour_agent()
        try:
            out = self._deps.brief_fn(agent)
        except Exception:
            logger.warning("setup wizard: tour brief failed", exc_info=True)
            return TourBrief(agent=agent, preview="", next_action="", message=_TOUR_BRIEF_FAILED)
        next_action = str(getattr(getattr(out, "health", None), "next_action", "") or "")
        error = str(getattr(out, "error", "") or "")
        if error.startswith(_INVALID_AGENT_PREFIX):
            logger.info("setup wizard: tour brief — no brief-capable agent (tried %r)", agent)
            return TourBrief(agent=agent, preview="", next_action=next_action, message=_TOUR_BRIEF_NO_AGENT)
        if error:
            logger.warning("setup wizard: tour brief reported an error — %s", error)
            return TourBrief(agent=agent, preview="", next_action=next_action, message=_TOUR_BRIEF_FAILED)
        return TourBrief(
            agent=agent,
            preview=str(getattr(out, "preview", "") or ""),
            next_action=next_action,
            message="",
        )

    def tour_timeline(self, query: str) -> TourTimeline:
        """One real ``timeline`` run — falls back to plain search inside
        the use case when nothing in the corpus carries dates yet."""
        try:
            out = self._deps.timeline_fn(query)
        except Exception:
            logger.warning("setup wizard: tour timeline failed", exc_info=True)
            return TourTimeline(hits=(), message=_TOUR_TIMELINE_FAILED)
        error = str(getattr(out, "error", "") or "")
        if error:
            logger.warning("setup wizard: tour timeline reported an error — %s", error)
            return TourTimeline(hits=(), message=_TOUR_TIMELINE_FAILED)
        return TourTimeline(hits=_to_tour_timeline_hits(getattr(out, "results", ()) or ()), message="")

    # Source OAuth connect (#489)
    # ------------------------------------------------------------------

    def source_options(self) -> tuple[SourceOption, ...]:
        """The source cards: folder plus the OAuth-connectable providers."""
        return self._deps.source_options_fn()

    def start_source_auth(self, provider: str, fields: Mapping[str, str], origin: str) -> SourceAuthStart:
        """Start one provider sign-in on a background thread.

        Mirrors the :meth:`start_index` worker pattern: the thread runs
        ``flow.authorize`` + secret persistence; errors are recorded
        behind the lock and surfaced by :meth:`source_auth_status`,
        never raised out of the thread. Flow-construction failures
        (unknown provider, missing credential material) surface
        immediately so the connect form can re-render with guidance.
        """
        if provider not in OAUTH_SOURCE_PROVIDERS:
            known = ", ".join(OAUTH_SOURCE_PROVIDERS)
            return SourceAuthStart(
                ok=False,
                error=f"Unknown source provider {provider!r}. fix: pick one of {known}.{_SOURCE_RETRY}",
            )
        nonce = token_urlsafe(16)
        # The GitHub App install redirect carries no ``state`` param, so
        # its correlation is the single-slot registry alone; Slack and
        # Google carry the nonce and the callback verifies it.
        expected_state = None if provider == PROVIDER_GITHUB else nonce
        listener = self._deps.listener_factory(origin, expected_state)
        browser = CapturingBrowser()
        try:
            flow = self._deps.oauth_flow_factory(
                SourceFlowRequest(provider=provider, fields=dict(fields), nonce=nonce, browser=browser)
            )
        except Exception as exc:
            # Constructor errors are F21-shaped already and never carry
            # the typed-in secret values (F15) — surface them verbatim.
            return SourceAuthStart(ok=False, error=str(exc))
        state = _SourceAuthState(
            provider=provider,
            instance=(fields.get("workspace") or "").strip(),
            expected_state=expected_state,
            listener=listener,
            browser=browser,
        )
        with self._source_lock:
            previous = self._source_state
            if previous is not None:
                # Single-slot registry: cancel any stale pending wait so
                # the replaced worker thread exits instead of lingering.
                previous.listener.close()
            self._source_state = state
            thread = threading.Thread(
                target=self._source_auth_worker,
                args=(state, flow),
                name="setup-wizard-source-auth",
                daemon=True,
            )
            self._source_thread = thread
            thread.start()
        return SourceAuthStart(ok=True, error=None)

    def _source_auth_worker(self, state: _SourceAuthState, flow: Any) -> None:
        """Background-thread body — records every failure as an operator message.

        On success the captured tokens persist under their canonical
        secret names (values never logged — F15) and the state flips to
        done so the status poll advances the wizard to the picker.
        """
        try:
            tokens = flow.authorize(listener=state.listener)
            client = flow.discover_client_credentials()
            instance = state.instance if state.provider == PROVIDER_SLACK else None
            for name, value in source_secret_leaves(state.provider, instance, client, tokens):
                self._deps.persist_secret_fn(name, value)
        except ConnectError as exc:
            # Denial / timeout — the listener's messages are already
            # F21-shaped operator guidance.
            with self._source_lock:
                state.error = str(exc)
            return
        except Exception as exc:
            with self._source_lock:
                state.error = (
                    f"The source connection failed: {exc}"
                    f" fix: check the connection details and the provider app settings.{_SOURCE_RETRY}"
                )
            return
        with self._source_lock:
            state.tokens = tokens
            state.client = client
            state.done = True

    def source_auth_status(self) -> SourceAuthStatus:
        """Phase snapshot: idle → starting → consent → exchanging → done|failed."""
        with self._source_lock:
            state = self._source_state
            if state is None:
                return SourceAuthStatus(provider="", phase=PHASE_IDLE, authorize_url=None, error=None)
            authorize_url = state.browser.authorize_url
            phase = _source_phase(state, authorize_url)
            return SourceAuthStatus(
                provider=state.provider,
                phase=phase,
                authorize_url=authorize_url,
                error=state.error,
            )

    def complete_source_callback(self, state: str | None, params: Mapping[str, str]) -> CallbackOutcome:
        """Deliver the provider redirect to the pending flow — or reject it.

        Security posture (the callback route is exempt from the
        operator-token guard — a provider redirect cannot carry custom
        headers): the protection is the single-use pending nonce. A
        callback is rejected when no flow is pending or when the
        redirect's ``state`` mismatches; the slot is consumed on first
        accepted delivery. The authorization code in ``params`` is
        never logged (F15).
        """
        with self._source_lock:
            pending = self._source_state
            if pending is None or pending.done or pending.error is not None or pending.callback_delivered:
                return CallbackOutcome(
                    ok=False,
                    error=(
                        "No source connection is waiting for a sign-in response."
                        f" fix: start the connection from the wizard's source step.{_SOURCE_RETRY}"
                    ),
                )
            if pending.expected_state is not None and state != pending.expected_state:
                return CallbackOutcome(
                    ok=False,
                    error=(
                        "The sign-in response does not match the connection this wizard started."
                        f" fix: use the consent screen the wizard opened — not an old link.{_SOURCE_RETRY}"
                    ),
                )
            pending.callback_delivered = True
            listener = pending.listener
        listener.deliver(params)
        return CallbackOutcome(ok=True, error=None)

    def _connected_source(self, provider: str) -> _SourceAuthState | None:
        """The completed auth state for ``provider``, or ``None``."""
        with self._source_lock:
            state = self._source_state
            if state is not None and state.done and state.provider == provider:
                return state
        return None

    def discover_source_units(self, provider: str) -> SourceUnits:
        """Picker payload: channels / repos for pickable providers,
        confirm copy for the rest. Discovery failures render as F21
        guidance, never a stack trace."""
        pickable = provider in PICKABLE_PROVIDERS
        state = self._connected_source(provider)
        if state is None:
            return SourceUnits(
                provider=provider,
                units=(),
                pickable=pickable,
                error=f"This source is not connected yet. fix: finish the sign-in first.{_SOURCE_RETRY}",
            )
        if not pickable:
            return SourceUnits(provider=provider, units=(), pickable=False, note=_confirm_note(provider))
        try:
            units = self._deps.discover_units_fn(provider, state.client, state.tokens)
        except Exception as exc:
            return SourceUnits(
                provider=provider,
                units=(),
                pickable=True,
                error=(
                    f"Could not list what this source offers: {exc}"
                    f" fix: check the connection is still valid, then reload this page.{_SOURCE_RETRY}"
                ),
            )
        return SourceUnits(provider=provider, units=units, pickable=True)

    def save_oauth_source(self, provider: str, instance: str, picks: tuple[str, ...]) -> SavedSource:
        """Emit the connector + collection config for the picked units.

        Writes ``topology`` entries through the overlay-aware config
        path (#485). The returned summary states what will be fetched
        BEFORE any spend; deep volumetrics (message counts, byte sizes)
        are deferred to the KFEAT-022 counters. ``OSError`` from a
        read-only config propagates so the route renders the rescue
        banner.
        """
        state = self._connected_source(provider)
        if state is None:
            return SavedSource(
                ok=False,
                summary="",
                error=f"This source is not connected yet. fix: finish the sign-in first.{_SOURCE_RETRY}",
            )
        resolved_instance = (instance or "").strip() or state.instance
        validation_error = _validate_source_picks(provider, resolved_instance, picks)
        if validation_error:
            return SavedSource(ok=False, summary="", error=validation_error)
        updates = topology_updates_for_source(provider, resolved_instance, picks, self._deps.read_config_fn())
        written = self._deps.write_config_fn(updates)
        return SavedSource(
            ok=True,
            summary=_source_summary(provider, resolved_instance, picks),
            error=None,
            config_file=str(written),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_plugin_name(provider: str, endpoint: str | None) -> str:
    """Reuse the wizard's azure endpoint-shape split; other names pass through.

    A legacy ``<r>.openai.azure.com`` endpoint must ride ``azure_legacy``
    even when the operator clicked ``azure_foundry`` (and vice versa) —
    :func:`provider_plugin_name` owns that mapping, so it is not
    reimplemented here. Non-azure picks are concrete plugin names from
    the installed registry and pass through verbatim.
    """
    if provider in (PLUGIN_AZURE_FOUNDRY, PLUGIN_AZURE_LEGACY) and endpoint:
        return provider_plugin_name("azure", endpoint)
    return provider


def _scrub_secret(text: str, secret: str) -> str:
    """Replace any occurrence of ``secret`` in ``text`` with a marker (F15)."""
    if secret and secret in text:
        return text.replace(secret, _REDACTED)
    return text


def _source_phase(state: _SourceAuthState, authorize_url: str | None) -> str:
    """Derive the operator-facing phase from one auth state's flags."""
    if state.error is not None:
        return PHASE_FAILED
    if state.done:
        return PHASE_DONE
    if state.callback_delivered:
        return PHASE_EXCHANGING
    if authorize_url:
        return PHASE_CONSENT
    return PHASE_STARTING


def _confirm_note(provider: str) -> str:
    """Confirm-screen copy for sources with no sub-unit picker.

    Google Drive joins Gmail/Calendar here because kairix has no Drive
    folder/drive listing surface yet — a Drive folder picker is
    KFEAT-022 territory.
    """
    if provider == PROVIDER_GMAIL:
        return "kairix will index email from this mailbox. Enter the mailbox address to confirm."
    if provider == PROVIDER_GOOGLE_CALENDAR:
        return "kairix will index events from this account's calendar. Leave the calendar id blank for the main one."
    return "kairix will index the files this Google account can see in Drive."


def _validate_source_picks(provider: str, instance: str, picks: tuple[str, ...]) -> str | None:
    """Reject saves that would emit an unusable connector config."""
    if provider in PICKABLE_PROVIDERS and not picks:
        return f"Nothing is selected yet. fix: tick at least one item to index.{_SOURCE_RETRY}"
    if provider == PROVIDER_GMAIL and not instance:
        return f"The mailbox address is required. fix: enter the Gmail address this sign-in belongs to.{_SOURCE_RETRY}"
    return None


def _source_summary(provider: str, instance: str, picks: tuple[str, ...]) -> str:
    """Plain-language pre-spend statement of what will be fetched.

    Unit counts only — deep volumetrics (message counts, byte sizes)
    are out of scope until the KFEAT-022 counters land.
    """
    if provider == PROVIDER_SLACK:
        noun = "channel" if len(picks) == 1 else "channels"
        return f"{len(picks)} {noun} selected — kairix will fetch and index messages from these channels."
    if provider == PROVIDER_GITHUB:
        noun = "repository" if len(picks) == 1 else "repositories"
        return f"{len(picks)} {noun} selected — kairix will fetch and index code and issues from them."
    if provider == PROVIDER_GMAIL:
        return f"Mailbox {instance} connected — kairix will fetch and index its email."
    if provider == PROVIDER_GOOGLE_CALENDAR:
        return f"Calendar {instance or 'primary'} connected — kairix will fetch and index its events."
    return "Google Drive connected — kairix will fetch and index the files this account can see."


#: Default F21 markers for a failed scan; specific failures (relative
#: path, container not-found) override with sharper guidance.
_SCAN_DEFAULT_FIX = "check the folder path exists on this machine and kairix can read it."
_SCAN_DEFAULT_NEXT = "enter the path again, then scan once more."


def _failed_scan(reason: str, *, fix: str = _SCAN_DEFAULT_FIX, next_: str = _SCAN_DEFAULT_NEXT) -> FolderScan:
    """A failed FolderScan with the F21 affordance attached."""
    return FolderScan(
        ok=False,
        files=0,
        words_estimate=0,
        cost_estimate_usd=0.0,
        error=f"{reason} fix: {fix} next: {next_}",
    )


def _relative_path_reason(candidate: str) -> str:
    """Why a relative path is rejected — NAMES the resolution base (#486).

    Silently joining the server's working directory surprises operators
    (the wizard runs server-side, often inside a container, so "here"
    is not the browser's folder).
    """
    return (
        f"That looks like a relative path: {candidate!r}. kairix would resolve it"
        f" against the server's working folder ({Path.cwd()}), which is rarely what you meant."
    )


def _deployment_missing_error(probe_model: str) -> str:
    """Azure DeploymentNotFound guidance (#484) — the key WORKED.

    Azure authenticated the request and then reported that no deployment
    carries the probed name, so key-blame guidance would send the
    operator in the wrong direction.
    """
    return (
        f"Your key works, but this Azure resource has no deployment named '{probe_model}'."
        " fix: enter one of your deployment names in the deployment field —"
        " they are listed in the Azure portal under your resource's Deployments page."
        " next: validate again."
    )


def _iter_text_files(folder: Path) -> Iterator[Path]:
    """Yield indexable files under ``folder``, stopping at :data:`SCAN_MAX_FILES`."""
    yielded = 0
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            if not name.lower().endswith(SCAN_FILE_SUFFIXES):
                continue
            yield Path(dirpath) / name
            yielded += 1
            if yielded >= SCAN_MAX_FILES:
                return


def _sample_words(candidate: Path) -> int | None:
    """Word count of one file, or ``None`` when it cannot be read."""
    try:
        return len(candidate.read_text(encoding="utf-8", errors="replace").split())
    except OSError:
        logger.debug("setup wizard: scan could not read %s", candidate)
        return None


def _scan_text_files(folder: Path) -> tuple[int, int]:
    """Count indexable files under ``folder`` and estimate their total words.

    Returns ``(files, words_estimate)``. Counting stops at
    :data:`SCAN_MAX_FILES`; words are read from at most
    :data:`SCAN_SAMPLE_FILES` files and the average is extrapolated.
    Unreadable files (broken symlinks, permission holes) are skipped from
    the word sample but still counted as files.
    """
    files = 0
    sampled = 0
    sampled_words = 0
    for candidate in _iter_text_files(folder):
        files += 1
        if sampled >= SCAN_SAMPLE_FILES:
            continue
        words = _sample_words(candidate)
        if words is not None:
            sampled += 1
            sampled_words += words
    if sampled == 0:
        return (files, 0)
    average = sampled_words / sampled
    return (files, int(average * files))


def _to_preview_hits(rows: Sequence[Any]) -> tuple[SearchPreviewHit, ...]:
    """Map pipeline ``BudgetedResult`` rows to preview hits, top-N, scores
    normalised against the best hit so the top result reads 100%."""
    top = list(rows)[:FIRST_SEARCH_TOP_N]
    raw_scores = [_row_score(row) for row in top]
    best = max(raw_scores, default=0.0)
    hits = []
    for row, raw in zip(top, raw_scores, strict=True):
        fused = getattr(row, "result", row)
        snippet = getattr(row, "content", "") or getattr(fused, "snippet", "")
        hits.append(
            SearchPreviewHit(
                title=str(getattr(fused, "title", "") or getattr(fused, "path", "")),
                snippet=str(snippet),
                source=str(getattr(fused, "path", "")),
                score=(raw / best) if best > 0 else 0.0,
            )
        )
    return tuple(hits)


def _row_score(row: Any) -> float:
    """Best available relevance signal on a pipeline result row."""
    fused = getattr(row, "result", row)
    boosted = float(getattr(fused, "boosted_score", 0.0) or 0.0)
    if boosted:
        return boosted
    return float(getattr(fused, "rrf_score", 0.0) or 0.0)


def _probe_detail(probe: Mapping[str, Any], key: str) -> str:
    """Human detail string for a failed capability, with a safe default."""
    detail = probe.get("detail")
    if isinstance(detail, Mapping):
        text = detail.get(key)
        if text:
            return str(text)
    return "no detail reported"


def tour_agent_from_config(config: Mapping[str, Any] | None) -> str | None:
    """First agent name declared in the config ``agents:`` block, or ``None``.

    Mirrors the shape-tolerance of the allowlist reader in
    :mod:`kairix.core.classify.router` (mapping schema and legacy list
    schema both parse) but preserves declaration order — the tour runs
    as the FIRST agent the operator configured, not an arbitrary set
    member. A missing or malformed block yields ``None`` and the caller
    falls back to the legacy shared agent.
    """
    if not config:
        return None
    agents_raw = config.get("agents")
    if isinstance(agents_raw, Mapping):
        for name in agents_raw:
            return str(name)
    if isinstance(agents_raw, list):
        for item in agents_raw:
            if isinstance(item, Mapping) and item.get("name"):
                return str(item["name"])
    return None


def _failed_roundtrip(agent: str, message: str) -> TourRememberRoundtrip:
    """A failed write-then-find DTO with guidance attached."""
    return TourRememberRoundtrip(
        saved=False,
        agent=agent,
        path="",
        found=False,
        elapsed_ms=0,
        hits=(),
        message=message,
    )


def _to_tour_timeline_hits(rows: Sequence[Any]) -> tuple[TourTimelineHit, ...]:
    """Map TimelineHit rows onto the tour DTO, top-N."""
    return tuple(
        TourTimelineHit(
            title=str(getattr(row, "title", "") or ""),
            snippet=str(getattr(row, "snippet", "") or ""),
            source=str(getattr(row, "path", "") or ""),
            date=str(getattr(row, "date", "") or ""),
        )
        for row in list(rows)[:TOUR_TIMELINE_TOP_N]
    )


__all__ = [
    "DEFAULT_PLUGIN_ENDPOINTS",
    "DEFAULT_VALIDATION_PROBE_MODEL",
    "EMBED_COST_USD_PER_1K_TOKENS",
    "EMPTY_INDEX_MESSAGE",
    "ENDPOINT_REQUIRED_PLUGINS",
    "FIRST_SEARCH_TOP_N",
    "PHASE_CONSENT",
    "PHASE_DONE",
    "PHASE_EXCHANGING",
    "PHASE_FAILED",
    "PHASE_IDLE",
    "PHASE_STARTING",
    "SCAN_FILE_SUFFIXES",
    "SCAN_MAX_FILES",
    "SCAN_SAMPLE_FILES",
    "TOKENS_PER_WORD",
    "TOUR_TIMELINE_TOP_N",
    "VALIDATION_PROBE_MODELS",
    "KairixSetupService",
    "SetupServiceDeps",
    "configured_document_root",
    "count_index_chunks",
    "embed_lock_held",
    "probe_provider_roundtrip",
    "provider_from_credentials",
    "read_config_mapping",
    "run_first_index",
    "tour_agent_from_config",
    "update_config_file",
    "wizard_config_target",
    "write_config_updates",
]
