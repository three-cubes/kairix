"""
kairix.platform.onboard.check — deployment health checks.

Each check is independent and returns a CheckResult with:
  name   — short identifier
  ok     — True if the check passed
  detail — human-readable explanation of status
  fix    — actionable remediation hint (None when ok=True)

run_all_checks() returns the full list. Checks are ordered from most-fundamental
(PATH, secrets) to most-dependent (vector search, entity graph) so failures are
diagnosed from the bottom up.

run_onboard_check() wraps run_all_checks() and returns a structured
OnboardResult — the canonical surface for ``kairix onboard check --json``
and for any caller (CI, docker-compose healthcheck, MCP probe) that needs
to act on individual failures programmatically.

Failure modes:
  - Checks never raise; exceptions are caught and surfaced as failed CheckResult.
  - Checks that require live external services (Neo4j, Azure KV) degrade gracefully.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.paths import Mode
from kairix.paths import mcp_port as _mcp_port

logger = logging.getLogger(__name__)

# F17 — check names appear as registry keys, CheckResult names emitted from each
# check, and remediation lookups; one constant per name keeps the check identifier
# in a single edit site.
_CHECK_QUERY_CACHE_STATS = "query_cache_stats"
_CHECK_EMBED_CACHE_STATS = "embed_cache_stats"
_CHECK_KAIRIX_ON_PATH = "kairix_on_path"
_CHECK_WRAPPER_INSTALLED = "wrapper_installed"
_CHECK_SECRETS_LOADED = "secrets_loaded"  # pragma: allowlist secret — check-name string, not a credential
_CHECK_DOCUMENT_ROOT_CONFIGURED = "document_root_configured"
_CHECK_VECTOR_SEARCH_WORKING = "vector_search_working"
_CHECK_NEO4J_REACHABLE = "neo4j_reachable"
_CHECK_AGENT_KNOWLEDGE_POPULATED = "agent_knowledge_populated"
_CHECK_CHUNK_DATE_POPULATED = "chunk_date_populated"
_CHECK_MCP_SERVICE = "mcp_service"
_CHECK_TOPOLOGY_CONFIG_VALID = "topology_config_valid"
_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED = "topology_cc_pairs_registered"
# GH #373 — schema-migration check for the per-entry default_in_scope column.
_CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT = "topology_default_in_scope_field_present"
# GH #373 Wave B — config-loader check that wildcard applies_to was expanded.
_CHECK_TOPOLOGY_WILDCARD_EXPANSION_RESOLVED = "topology_wildcard_expansion_resolved"
_CHECK_SHAREPOINT_CREDENTIALS_LOADED = (
    "sharepoint_credentials_loaded"  # pragma: allowlist secret — check-name string, not a credential
)
_CHECK_MAINTENANCE_LOOP_TICKING = "maintenance_loop_ticking"
_CHECK_EXTRACTOR_LIBRARIES_IMPORTABLE = "extractor_libraries_importable"
_CHECK_AGENT_MEMORY_WRITABLE = "agent_memory_writable"


@dataclass
class CheckResult:
    """Result of a single deployment check."""

    name: str
    ok: bool
    detail: str
    fix: str | None = field(default=None)


@dataclass(frozen=True)
class CheckFailure:
    """Single failed check, structured for machine consumption.

    Every failure carries:
      check       — short ID matching the underlying CheckResult.name
      detail      — one-line explanation of what's wrong
      remediation — exact operator-actionable command/check the operator
                    should run NOW (never empty)

    Design principle: the remediation string must hand the operator their
    next concrete step. "Run `<command>`" or "Check `<path>` exists" — not
    a description of the failure state.
    """

    check: str
    detail: str
    remediation: str


@dataclass(frozen=True)
class OnboardResult:
    """Structured result of a full onboard check run.

    Fields:
      passed       — number of checks that returned ok=True
      total        — total number of checks executed
      failures     — list of CheckFailure, one per failed check, in
                     dependency order (most fundamental first)
      fully_passed — True iff passed == total (derived)

    The CLI's ``--json`` flag emits this directly; the human-readable
    output renders the same data with icons + indented remediations.

    Exit-code semantics: 0 when fully_passed is True, 1 otherwise.
    """

    passed: int
    total: int
    failures: list[CheckFailure]
    fully_passed: bool


# ---------------------------------------------------------------------------
# Canonical remediations
# ---------------------------------------------------------------------------
# Each check has a canonical operator-actionable remediation string. When a
# check returns a CheckResult with fix=None (or a structurally empty fix),
# the canonical remediation is substituted so every CheckFailure surfaces a
# concrete next step. The per-check CheckResult.fix strings remain the
# detailed multi-line guidance for human readers; CheckFailure.remediation
# is the one-line "run this now" command an agent or healthcheck can act on.

# F17 — shared remediation building blocks. `kairix init verify` is the
# canonical "diagnose my install" entry point (the old strings named
# scripts/deploy-vm.sh, which no longer exists — GH #473). The Neo4j
# strings are deployment-aware (GH #476 — the old remediation curl'd a
# stale fork URL) and keep the production-required framing that landed
# in v2026.5.24a3.
_INSTALL_VERIFY_FIX = (
    "fix: run `kairix init verify` — it reports any missing install element "
    "with a remediation. next: re-run `kairix onboard check`."
)
_NEO4J_REQUIRED_FRAMING = (
    "Neo4j is required for production — entity boost, multi-hop, alias "
    "resolution, and briefing all rely on it. The system loads without it "
    "but entity-heavy queries degrade significantly."
)
_NEO4J_FIX_CONTAINER = (
    "fix: the bundled compose ships Neo4j — set "
    "`KAIRIX_NEO4J_URI=bolt://neo4j:7687` in your .env. "
    "next: `docker compose up -d && kairix onboard check`. " + _NEO4J_REQUIRED_FRAMING
)
_NEO4J_FIX_DEFAULT = (
    "fix: run the bundled docker-compose.yml (Neo4j is included) OR install "
    "Neo4j and set `KAIRIX_NEO4J_URI=bolt://localhost:7687`. "
    "next: `kairix onboard check`. " + _NEO4J_REQUIRED_FRAMING
)

# GH #477 — the document_root + secrets remediations used to hard-code
# /opt/kairix/service.env, a path from a single historical VM layout that
# does NOT exist on a fresh Docker or pip install. README tells agents to
# surface these remediations verbatim, so a container/pip operator was sent
# to edit a phantom file. The strings below name the config surface that
# actually exists per deployment mode (same Mode.detect() seam the Neo4j
# remediation already uses, GH #476):
#   container — the operator `.env` beside docker-compose.yml
#   system    — /etc/kairix/.env (FHS layout laid down by `kairix init --system`)
#   user/pip  — kairix.config.yaml (or the KAIRIX_DOCUMENT_ROOT env var)
_CONFIG_LOCATION_BY_MODE: dict[Mode, str] = {
    Mode.container: "the operator `.env` beside docker-compose.yml",
    Mode.system: "/etc/kairix/.env",
    Mode.user: "kairix.config.yaml (or set the KAIRIX_DOCUMENT_ROOT env var)",
}


def _config_location_for(env: Mapping[str, str] | None = None) -> str:
    """Return the deployment-appropriate config-file location phrase.

    Resolved through :meth:`kairix.paths.Mode.detect` so container, system,
    and pip operators each see the surface that exists on their install
    rather than a single-VM path (GH #477). ``env`` is the same DI seam
    :func:`check_document_root_configured` already exposes; production
    callers leave it ``None`` and the live environment is read at the F4
    boundary in paths.py.
    """
    return _CONFIG_LOCATION_BY_MODE.get(Mode.detect(env), _CONFIG_LOCATION_BY_MODE[Mode.user])


# The canonical (one-line) document_root remediation stays deployment-neutral
# — it can't know the operator's mode at registry-build time, so it names the
# env var + the config file without the phantom /opt path.
_DOCUMENT_ROOT_FIX = (
    "Set `KAIRIX_DOCUMENT_ROOT=/your/docs/path` in your kairix.config.yaml "
    "(or as an env var) and ensure the directory exists."
)

CANONICAL_REMEDIATIONS: dict[str, str] = {
    _CHECK_QUERY_CACHE_STATS: (
        "Diagnostic check — no remediation required. Cache hit-rate is informational; "
        "tune `KAIRIX_QUERY_CACHE_MAX_ENTRIES` / `KAIRIX_QUERY_CACHE_MAX_AGE_S` if needed."
    ),
    _CHECK_EMBED_CACHE_STATS: (
        "Diagnostic check — no remediation required. Cache hit-rate is informational; "
        "tune `KAIRIX_EMBED_CACHE_MAX_ENTRIES` / `KAIRIX_EMBED_CACHE_MAX_AGE_S` if needed."
    ),
    _CHECK_KAIRIX_ON_PATH: _INSTALL_VERIFY_FIX,
    _CHECK_WRAPPER_INSTALLED: _INSTALL_VERIFY_FIX,
    _CHECK_SECRETS_LOADED: (
        "Set `KAIRIX_PROVIDER_LLM_API_KEY=...` and `KAIRIX_PROVIDER_LLM_ENDPOINT=...` "
        "in your environment, or write them as KEY=VALUE lines in a secrets file and "
        "point `KAIRIX_SECRETS_FILE` at it. Run `kairix secrets verify` to confirm "
        "every credential resolves."
    ),
    _CHECK_DOCUMENT_ROOT_CONFIGURED: _DOCUMENT_ROOT_FIX,
    _CHECK_VECTOR_SEARCH_WORKING: (
        "Run `docker logs kairix-worker-1` for embed-pipeline errors; confirm "
        "`kairix onboard check secrets_loaded` passes; then run `kairix embed --limit 20` "
        "to test the embed pipeline."
    ),
    _CHECK_NEO4J_REACHABLE: _NEO4J_FIX_DEFAULT,
    _CHECK_AGENT_KNOWLEDGE_POPULATED: (
        "Run `kairix embed` to populate the agent knowledge store from the document root, "
        "or create at least one memory file at "
        "$KAIRIX_DOCUMENT_ROOT/04-Agent-Knowledge/<agent>/memory/YYYY-MM-DD.md."
    ),
    _CHECK_CHUNK_DATE_POPULATED: (
        "Run `kairix embed --rebuild-canaries` to refresh the chunk_date index. "
        "If chunk_date is missing entirely, run `kairix embed` to trigger the migration."
    ),
    _CHECK_MCP_SERVICE: (
        "Register kairix with at least one MCP consumer harness: "
        '`openclaw mcp set mcp-kairix \'{"type":"stdio","command":"/path/to/kairix-start.sh"}\'`, '
        "add to ~/Library/Application Support/Claude/claude_desktop_config.json, or run "
        "`sudo systemctl enable --now kairix-mcp.service`."
    ),
    _CHECK_TOPOLOGY_CONFIG_VALID: (
        "fix: open kairix.config.yaml and resolve the topology cross-reference "
        "failures (every cc_pair must reference a declared connector + credential; "
        "every collection source / scope_profile entry / skill source must reference "
        "a declared cc_pair / collection). next: run `kairix config validate` to "
        "re-run the validator."
    ),
    _CHECK_TOPOLOGY_CC_PAIRS_REGISTERED: (
        "fix: restart the worker (e.g. `docker compose restart kairix-worker` or "
        "`systemctl restart kairix-worker`) — the apply-bridge runs at boot when "
        "`topology_config` is on and materialises declared cc_pairs idempotently. "
        "next: re-run `kairix onboard check` to confirm every declared cc_pair has a row."
    ),
    _CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT: (
        "fix: restart the kairix worker / API process — the GH #373 schema migration "
        "adds the `default_in_scope` column to `topology_scope_entries` at boot via "
        "kairix.core.db.schema.migrate. Existing rows back-fill to default_in_scope=1 "
        "(back-compat — every row surfaces in default search). "
        "next: re-run `kairix onboard check topology_default_in_scope_field_present` "
        "to confirm the column is present. "
        "run: docker compose restart kairix-worker kairix-1 (Docker) "
        "OR systemctl restart kairix-worker kairix-mcp (systemd)."
    ),
    _CHECK_TOPOLOGY_WILDCARD_EXPANSION_RESOLVED: (
        "fix: re-run the topology config loader (restart the worker) so any "
        '`applies_to: ["*"]` wildcards in kairix.config.yaml expand to concrete '
        "actor_id rows in topology_scope_entries. A literal `*` actor_id in the DB "
        "means the loader did not run (or the YAML edit post-dates the last apply). "
        "next: docker compose restart kairix-worker. "
        "run: kairix config validate."
    ),
    _CHECK_SHAREPOINT_CREDENTIALS_LOADED: (
        "fix: set the three M365 client-credentials secrets that the SharePoint "
        "connector resolves via kairix.secrets.get_secret — "
        "`connector-m365-tenant-id`, `connector-m365-client-id`, and "
        "`connector-m365-client-secret`. Docker operators write them as "
        "KEY=VALUE lines in /run/secrets/kairix.env (uppercase + dash → underscore: "
        "CONNECTOR_M365_TENANT_ID etc.); pip operators set them in "
        "~/.kairix/secrets.env or as plain env vars. next: re-run "
        "`kairix onboard check sharepoint_credentials_loaded`."
    ),
    _CHECK_MAINTENANCE_LOOP_TICKING: (
        "fix: confirm the kairix worker process is running (`docker ps | grep "
        "kairix-worker`); confirm the worker state JSON is being written "
        "(`kairix worker status`); confirm KAIRIX_MAINTENANCE_INTERVAL_S "
        "is sensible. next: tail worker logs for `event=maintenance_tick_completed` "
        "lines — absence means the loop isn't firing. "
        "run: kairix worker maintenance (to fire a one-shot tick on demand)."
    ),
    _CHECK_EXTRACTOR_LIBRARIES_IMPORTABLE: (
        "fix: install the missing extras into the runtime image — "
        "`pip install 'Kairix-agentic-knowledge-mgt[markitdown,pdf_fallback,docx,pptx,xlsx]'`. "
        "next: rebuild the Docker image with the extras in the Dockerfile install "
        "line, then redeploy. run: docker exec app-kairix-1 python3 -c "
        "'import markitdown.converters; print(\"ok\")' to verify."
    ),
    _CHECK_AGENT_MEMORY_WRITABLE: (
        "fix: give the kairix process at least one writable memory destination. This check "
        "only fails when NEITHER the 04-Agent-Knowledge overlay NOR the writable data-dir "
        "fallback can be written — on a hardened read-only-root box the read-only overlay is "
        "expected and the data-dir fallback carries agent memory, so a failure means that "
        "fallback is also unwritable (e.g. the disk is full or the data dir is not owned by "
        "the kairix user). next: re-run `kairix onboard check agent_memory_writable` after "
        "freeing space / fixing data-dir ownership. run: df -h and confirm the kairix data "
        "dir is owned by the kairix user."
    ),
}

_UNKNOWN_CHECK_REMEDIATION = (
    "Report this failure as a bug in kairix.platform.onboard.check — the check has no canonical remediation registered."
)


def _remediation_for(check_name: str, fix: str | None) -> str:
    """Return the canonical remediation for *check_name*.

    Falls back to the per-check ``fix`` string only when the check name has
    no canonical entry (which should never happen for production checks —
    the dict above is the source of truth). Never returns empty.
    """
    canonical = CANONICAL_REMEDIATIONS.get(check_name)
    if canonical:
        return canonical
    if fix and fix.strip():
        return fix.strip()
    return _UNKNOWN_CHECK_REMEDIATION


def _default_is_docker() -> bool:
    """Production ``is_docker`` — defers to ``kairix.paths.is_docker_runtime_check``."""
    from kairix.paths import is_docker_runtime_check as _impl

    return _impl()


@dataclass
class OnboardChecksDeps:
    """Injectable dependencies for the onboard health checks.

    Each field defaults to a production implementation; tests construct
    ``OnboardChecksDeps(which=fake_which, is_docker=lambda: True)``
    rather than threading per-check ``*_fn=None`` substitution kwargs
    through the public health-check signatures.
    """

    which: Callable[[str], str | None] = field(default_factory=lambda: shutil.which)
    is_docker: Callable[[], bool] = field(default_factory=lambda: _default_is_docker)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_kairix_on_path(deps: OnboardChecksDeps | None = None) -> CheckResult:
    """kairix is findable via PATH.

    ``deps.which`` is the DI seam (defaults to ``shutil.which``); tests
    pass a ``OnboardChecksDeps`` with a callable returning the desired
    result so the live PATH never needs mutating.
    """
    d = deps if deps is not None else OnboardChecksDeps()
    path = d.which("kairix")
    if path is None:
        return CheckResult(
            name=_CHECK_KAIRIX_ON_PATH,
            ok=False,
            detail="kairix not found on PATH",
            fix=(
                "Add the directory holding the kairix entry point to PATH "
                "(pipx: ~/.local/bin; venv: <venv>/bin).\n" + _INSTALL_VERIFY_FIX
            ),
        )
    return CheckResult(name=_CHECK_KAIRIX_ON_PATH, ok=True, detail=f"kairix found at {path}")


def check_wrapper_installed(deps: OnboardChecksDeps | None = None) -> CheckResult:
    """The kairix symlink points to a shell wrapper, not the raw Python binary.

    ``deps.is_docker`` and ``deps.which`` are the DI seams; production
    callers leave ``deps=None`` and defaults wire to ``kairix.paths.is_docker_runtime_check``
    and ``shutil.which``.
    """
    d = deps if deps is not None else OnboardChecksDeps()

    if d.is_docker():
        return CheckResult(
            name=_CHECK_WRAPPER_INSTALLED,
            ok=True,
            detail="Running in Docker — wrapper check skipped (pip install in image)",
        )

    path = d.which("kairix")
    if path is None:
        return CheckResult(
            name=_CHECK_WRAPPER_INSTALLED,
            ok=False,
            detail="kairix not on PATH — cannot check wrapper",
            fix=_INSTALL_VERIFY_FIX,
        )

    resolved = Path(path).resolve()

    # Check if the binary is a shell script (starts with shebang that isn't python)
    try:
        with open(resolved, "rb") as f:
            header = f.read(128)
        first_line = header.split(b"\n")[0].decode("utf-8", errors="replace").strip()

        if first_line.startswith("#!") and "python" in first_line:
            return CheckResult(
                name=_CHECK_WRAPPER_INSTALLED,
                ok=False,
                detail=f"kairix symlink points to raw Python binary: {resolved}",
                fix=(
                    "The kairix command should resolve to a shell wrapper that loads "
                    "secrets before exec'ing Python, not the raw Python binary.\n" + _INSTALL_VERIFY_FIX
                ),
            )
        if first_line.startswith("#!") and ("bash" in first_line or "sh" in first_line):
            return CheckResult(
                name=_CHECK_WRAPPER_INSTALLED,
                ok=True,
                detail=f"wrapper installed at {resolved}",
            )

        return CheckResult(
            name=_CHECK_WRAPPER_INSTALLED,
            ok=False,
            detail=f"kairix binary has unexpected format (header: {first_line[:60]})",
            fix=_INSTALL_VERIFY_FIX,
        )
    except Exception as exc:
        return CheckResult(
            name=_CHECK_WRAPPER_INSTALLED,
            ok=False,
            detail=f"Cannot read kairix binary at {resolved}: {exc}",
            fix="Check file permissions on the kairix binary.",
        )


# Canonical names drive every user-facing message; the legacy pair is
# still accepted because deployed secrets bundles (vault-agent sidecar,
# GH #479) emit it. Retirement of the legacy pair is tracked in GH #369.
_CANONICAL_SECRETS = ("KAIRIX_PROVIDER_LLM_API_KEY", "KAIRIX_PROVIDER_LLM_ENDPOINT")
_LEGACY_SECRETS = ("KAIRIX_LLM_API_KEY", "KAIRIX_LLM_ENDPOINT")
_REQUIRED_SECRETS = _CANONICAL_SECRETS
_ROTATION_NOTE = (
    "legacy KAIRIX_LLM_* names resolved — rotate to KAIRIX_PROVIDER_LLM_API_KEY / "
    "KAIRIX_PROVIDER_LLM_ENDPOINT (run `kairix secrets verify` for the canonical table)"
)
_SECRETS_FILE_PROBE_PATHS = (
    "/run/secrets/kairix.env",
    "/opt/kairix/secrets.env",
)


def _secrets_file_keys_present(path: Path, keys: tuple[str, ...]) -> set[str]:
    """Return the subset of *keys* found as KEY= entries in a secrets file."""
    found: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k = line.split("=", 1)[0].strip()
            if k in keys:
                found.add(k)
    except OSError:
        pass
    return found


def _secrets_from_env(env: Mapping[str, str]) -> CheckResult | None:
    """Tier 1: return ok when a full credential pair sits in the env.

    Canonical pair first; the legacy pair still passes (deployed bundles
    emit it, GH #479) but the detail carries a rotation note. ``None``
    means neither generation is complete — caller falls through to the
    file probe.
    """
    for pair, note in ((_CANONICAL_SECRETS, ""), (_LEGACY_SECRETS, f" — {_ROTATION_NOTE}")):
        api_key = env.get(pair[0], "")
        endpoint = env.get(pair[1], "")
        if api_key and endpoint:
            masked_key = api_key[:8] + "..." if len(api_key) > 8 else "***"
            return CheckResult(
                name=_CHECK_SECRETS_LOADED,
                ok=True,
                detail=f"LLM credentials present (key: {masked_key}, endpoint: {endpoint[:40]}...){note}",
            )
    return None


def _secrets_from_file(probe: str) -> CheckResult:
    """Tier 2: judge an existing secrets file — either generation passes whole."""
    found = _secrets_file_keys_present(Path(probe), _CANONICAL_SECRETS + _LEGACY_SECRETS)
    canonical_complete = all(k in found for k in _CANONICAL_SECRETS)
    legacy_complete = all(k in found for k in _LEGACY_SECRETS)
    if canonical_complete or legacy_complete:
        note = "" if canonical_complete else f" ({_ROTATION_NOTE})"
        return CheckResult(
            name=_CHECK_SECRETS_LOADED,
            ok=True,
            detail=(
                f"Secrets file found at {probe} — credentials will be active on first search call. "
                f"Run `kairix search` to confirm.{note}"
            ),
        )
    # File exists but neither generation is complete — give specific guidance
    missing_in_file = [k for k in _CANONICAL_SECRETS if k not in found]
    return CheckResult(
        name=_CHECK_SECRETS_LOADED,
        ok=False,
        detail=f"Secrets file at {probe} is missing required keys: {', '.join(missing_in_file)}",
        fix=(
            f"Add the missing keys to {probe}:\n"
            + "".join(f"  {k}=<value>\n" for k in missing_in_file)
            + "Set KAIRIX_PROVIDER_LLM_API_KEY and KAIRIX_PROVIDER_LLM_ENDPOINT "
            "in your env or secrets file."
        ),
    )


def check_secrets_loaded(env: Mapping[str, str] | None = None) -> CheckResult:
    """LLM credentials are available in the environment or a secrets file.

    ``env`` is a DI seam (defaults to ``os.environ``); tests pass an
    explicit mapping rather than monkeypatching the process environment.
    """
    if env is None:
        env = os.environ

    # Tier 1 — credentials in process environment (wrapper loaded them).
    # Canonical KAIRIX_PROVIDER_LLM_* first; the legacy pair still passes
    # (deployed bundles emit it, GH #479) but carries a rotation note.
    env_result = _secrets_from_env(env)
    if env_result is not None:
        return env_result

    # Tier 2 — probe secrets file directly (credentials present but not yet in env;
    # load_secrets() is called lazily on first provider plugin construction)
    secrets_file_env = env.get("KAIRIX_SECRETS_FILE", "")
    probe_paths: tuple[str, ...] = (
        (secrets_file_env, *_SECRETS_FILE_PROBE_PATHS) if secrets_file_env else _SECRETS_FILE_PROBE_PATHS
    )
    for probe in probe_paths:
        if Path(probe).exists():
            return _secrets_from_file(probe)

    # Tier 3 — nothing found (read through the env seam, not os.environ,
    # so callers passing an explicit mapping get a truthful missing list)
    missing_env = [k for k in _REQUIRED_SECRETS if not env.get(k)]
    return CheckResult(
        name=_CHECK_SECRETS_LOADED,
        ok=False,
        detail=f"LLM credentials not found in environment or secrets file: {', '.join(missing_env)}",
        fix=(
            "fix: set the missing keys named above in your environment, or write "
            "them as KEY=VALUE lines in a secrets file and point KAIRIX_SECRETS_FILE "
            "at it. next: run `kairix secrets verify` to confirm every credential "
            "resolves, then re-run `kairix onboard check`."
        ),
    )


def check_document_root_configured(env: Mapping[str, str] | None = None) -> CheckResult:
    """KAIRIX_DOCUMENT_ROOT is set and the directory exists.

    ``env`` is a DI seam (defaults to ``os.environ``); tests pass an
    explicit mapping rather than monkeypatching the process environment.
    The same ``env`` mapping drives :func:`_config_location_for` so the
    failure remediation names the config surface that exists on the
    operator's deployment mode (container `.env` / `/etc/kairix/.env` /
    `kairix.config.yaml`) rather than the phantom `/opt/kairix/service.env`
    VM path the old strings hard-coded (GH #477).
    """
    if env is None:
        env = os.environ
    config_location = _config_location_for(env)
    doc_root = env.get("KAIRIX_DOCUMENT_ROOT", "")
    if not doc_root:
        return CheckResult(
            name=_CHECK_DOCUMENT_ROOT_CONFIGURED,
            ok=False,
            detail="KAIRIX_DOCUMENT_ROOT is not set",
            fix=(f"Set KAIRIX_DOCUMENT_ROOT in {config_location}:\n  KAIRIX_DOCUMENT_ROOT=/your/docs/path"),
        )
    p = Path(doc_root)
    if not p.exists():
        return CheckResult(
            name=_CHECK_DOCUMENT_ROOT_CONFIGURED,
            ok=False,
            detail=f"KAIRIX_DOCUMENT_ROOT directory does not exist: {doc_root}",
            fix=(
                f"Create the directory or update KAIRIX_DOCUMENT_ROOT in {config_location}.\n"
                "If your documents are at a different path, set: KAIRIX_DOCUMENT_ROOT=/your/docs/path"
            ),
        )
    md_count = sum(1 for _ in p.rglob("*.md") if not _.name.startswith("."))
    return CheckResult(
        name=_CHECK_DOCUMENT_ROOT_CONFIGURED,
        ok=True,
        detail=f"Document root: {doc_root} ({md_count:,} .md files found)",
    )


# Backwards-compat alias


def _vector_search_verdict(result: Any) -> CheckResult:
    """Judge one probe search result: vec leg failed, empty index, or working."""
    vec_count = getattr(result, "vec_count", None)
    bm25_count = getattr(result, "bm25_count", None)
    vec_failed = getattr(result, "vec_failed", None)
    result_count = len(result.results) if hasattr(result, "results") else 0

    if vec_failed:
        return CheckResult(
            name=_CHECK_VECTOR_SEARCH_WORKING,
            ok=False,
            detail=(
                f"Vector search failed (vec_failed=True). Results: {result_count} (BM25 only). bm25={bm25_count}, vec=0"
            ),
            fix=(
                "Vector search failure usually means Azure credentials aren't loaded.\n"
                "Check: kairix onboard check  — look at secrets_loaded result.\n"
                "If secrets are loaded, check the embed ran:\n"
                "  kairix search 'test query'\n"
                "  If vec=0: run kairix embed --limit 20 to test."
            ),
        )

    if vec_count is not None and vec_count == 0 and result_count == 0:
        return CheckResult(
            name=_CHECK_VECTOR_SEARCH_WORKING,
            ok=False,
            detail="Search returned 0 results (vec=0, bm25=0) — vault may not be embedded yet",
            fix=(
                "Run: kairix embed --limit 20  (test embed)\n"
                "Then: kairix embed             (full vault embed)\n"
                "See OPERATIONS.md §First-Run Sequence for full steps."
            ),
        )

    detail_parts = [f"results={result_count}"]
    if vec_count is not None:
        detail_parts.append(f"vec={vec_count}")
    if bm25_count is not None:
        detail_parts.append(f"bm25={bm25_count}")

    return CheckResult(
        name=_CHECK_VECTOR_SEARCH_WORKING,
        ok=True,
        detail=f"Vector search working ({', '.join(detail_parts)})",
    )


def check_vector_search_working(pipeline: Any | None = None) -> CheckResult:
    """Vector search returns results with vec_count > 0 (not BM25-only fallback).

    Args:
        pipeline: Injectable search pipeline for testing. Defaults to the
                  production ``build_search_pipeline()``.
    """
    try:
        if pipeline is None:
            from kairix.core.factory import build_search_pipeline

            pipeline = build_search_pipeline()
        result = pipeline.search(query="knowledge management", budget=500)
        return _vector_search_verdict(result)

    except Exception as exc:
        return CheckResult(
            name=_CHECK_VECTOR_SEARCH_WORKING,
            ok=False,
            detail=f"Search raised an exception: {exc}",
            fix=(
                "Check KAIRIX_LLM_API_KEY and KAIRIX_LLM_ENDPOINT are set.\n"
                "Run: kairix onboard check  to see secrets_loaded status."
            ),
        )


def check_neo4j_reachable(
    neo4j_client: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """Neo4j is reachable and contains entities.

    The "client unavailable" remediation is deployment-aware (GH #476):
    inside the kairix container (``KAIRIX_CONTAINER=1``) it names the
    bundled compose sidecar URI (``bolt://neo4j:7687``); everywhere else
    it points at the bundled docker-compose.yml or a local install.

    Args:
        neo4j_client: Injectable Neo4j client for testing.
                      Defaults to the production client.
        env: DI seam forwarded to :meth:`kairix.paths.Mode.detect` so tests
             drive the container / non-container remediation branches with
             an explicit mapping. Production callers leave it ``None`` and
             the live environment is read at the F4 boundary in paths.py.
    """
    try:
        if neo4j_client is not None:
            client = neo4j_client
        else:
            from kairix.knowledge.graph.client import get_client

            client = get_client()
        if not getattr(client, "available", False):
            in_container = Mode.detect(env) is Mode.container
            return CheckResult(
                name=_CHECK_NEO4J_REACHABLE,
                ok=False,
                detail="Neo4j client unavailable (KAIRIX_NEO4J_URI not set or connection refused)",
                fix=_NEO4J_FIX_CONTAINER if in_container else _NEO4J_FIX_DEFAULT,
            )

        rows = client.cypher("MATCH (n) RETURN count(n) AS total LIMIT 1")
        total = rows[0]["total"] if rows else 0

        if total == 0:
            return CheckResult(
                name=_CHECK_NEO4J_REACHABLE,
                ok=False,
                detail="Neo4j reachable but empty — document crawler has not run",
                fix=(
                    "Populate the entity graph:\n"
                    "  kairix store crawl --document-root $KAIRIX_DOCUMENT_ROOT\n"
                    "Expected: ≥ 50 nodes for a typical document store."
                ),
            )

        return CheckResult(
            name=_CHECK_NEO4J_REACHABLE,
            ok=True,
            detail=f"Neo4j reachable — {total:,} nodes in graph",
        )

    except Exception as exc:
        return CheckResult(
            name=_CHECK_NEO4J_REACHABLE,
            ok=False,
            detail=f"Neo4j check failed: {exc}",
            fix=(
                "fix: verify KAIRIX_NEO4J_URI and the Neo4j credentials resolve "
                "(run `kairix secrets verify`). kairix degrades gracefully when "
                "Neo4j is unavailable — entity boost and multi-hop are disabled "
                "but search still works. next: re-run `kairix onboard check`."
            ),
        )


def check_agent_knowledge_populated(
    document_root_path: Path | None = None,
    *,
    agent_knowledge_dir: str | None = None,
    memory_glob: str | None = None,
) -> CheckResult:
    """At least one agent has memory logs (required for briefing pipeline).

    Args:
        document_root_path: Override for the document root. Defaults to
                            ``kairix.paths.document_root()``.
        agent_knowledge_dir: Directory name under ``document_root`` that
                             holds the agent-knowledge tree. Defaults to
                             :func:`kairix.paths.agent_knowledge_dir_name`
                             (which reads ``paths.agent_knowledge_dir`` from
                             ``kairix.config.yaml``).
        memory_glob: Glob pattern (relative to the agent-knowledge dir)
                     that identifies memory log files. Defaults to
                     :func:`kairix.paths.agent_memory_glob` (which reads
                     ``paths.agent_memory_glob`` from ``kairix.config.yaml``).

    Both ``agent_knowledge_dir`` and ``memory_glob`` are test seams and
    operator escape hatches — different vaults arrange agent memory under
    different conventions, so this check pulls the layout from config
    rather than hard-coding it.
    """
    if document_root_path is None:
        from kairix.paths import document_root

        document_root_path = document_root()

    if agent_knowledge_dir is None:
        from kairix.paths import agent_knowledge_dir_name

        agent_knowledge_dir = agent_knowledge_dir_name()

    if memory_glob is None:
        from kairix.paths import agent_memory_glob

        memory_glob = agent_memory_glob()

    agent_knowledge = document_root_path / agent_knowledge_dir
    if not agent_knowledge.exists():
        return CheckResult(
            name=_CHECK_AGENT_KNOWLEDGE_POPULATED,
            ok=False,
            detail=f"Agent knowledge directory not found: {agent_knowledge}",
            fix=(
                f"Create the directory:\n"
                f"  mkdir -p {agent_knowledge}/<agent-name>\n"
                "Agents write daily memory logs here during sessions.\n"
                "To use a different directory name, set "
                "``paths.agent_knowledge_dir`` in kairix.config.yaml."
            ),
        )

    memory_files = list(agent_knowledge.rglob(memory_glob))
    if not memory_files:
        return CheckResult(
            name=_CHECK_AGENT_KNOWLEDGE_POPULATED,
            ok=False,
            detail=f"No agent memory logs found under {agent_knowledge} (glob={memory_glob!r})",
            fix=(
                "Agent memory logs are written by agents during sessions.\n"
                f"Searched: {agent_knowledge}/{memory_glob}\n"
                "Briefing synthesis (kairix brief) requires at least some memory content.\n"
                "If your vault uses a different layout, set "
                "``paths.agent_memory_glob`` in kairix.config.yaml "
                "(default: ``**/*.md``)."
            ),
        )

    return CheckResult(
        name=_CHECK_AGENT_KNOWLEDGE_POPULATED,
        ok=True,
        detail=f"Agent memory logs found: {len(memory_files)} files under {agent_knowledge}",
    )


def _query_chunk_date_counts(db: Any) -> tuple[int, int] | CheckResult:
    """Inspect content_vectors for chunk_date coverage. Returns (total, dated) or an error CheckResult.

    Extracted from ``check_chunk_date_populated`` so the column-missing
    early-return doesn't have to live inside the outer try/except — that
    nesting pushed the parent over F16's ceiling.
    """
    cols = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    if "chunk_date" not in cols:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail="chunk_date column missing from content_vectors",
            fix=(
                "Run kairix embed to add the column and populate dates.\nThe migration is automatic on next embed run."
            ),
        )
    total = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    dated = db.execute("SELECT COUNT(*) FROM content_vectors WHERE chunk_date IS NOT NULL").fetchone()[0]
    return total, dated


def _grade_chunk_date_coverage(total: int, dated: int) -> CheckResult:
    """Turn raw chunk_date counts into a CheckResult with the right severity.

    Extracted from ``check_chunk_date_populated`` to flatten the if/if/if
    severity-ladder out of the outer try-block. Same F16 motivation as
    ``_query_chunk_date_counts``.
    """
    if total == 0:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail="content_vectors is empty — vault has not been embedded",
            fix="Run: kairix embed",
        )
    pct = 100 * dated / total
    if dated == 0:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail=f"chunk_date: 0/{total} chunks dated (0%) — TMP-7B temporal boost is inert",
            fix=(
                "Run kairix embed to populate chunk_date from document frontmatter and filenames.\n"
                "Documents need 'date: YYYY-MM-DD' in frontmatter or a date in their filename."
            ),
        )
    if pct < 20:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail=f"chunk_date: {dated}/{total} chunks dated ({pct:.0f}%) — low coverage, temporal boost degraded",
            fix=(
                "Add 'date: YYYY-MM-DD' frontmatter to more documents, or use dated filenames.\n"
                "Re-run kairix embed after updating documents."
            ),
        )
    return CheckResult(
        name=_CHECK_CHUNK_DATE_POPULATED,
        ok=True,
        detail=f"chunk_date: {dated}/{total} chunks dated ({pct:.0f}%)",
    )


def check_chunk_date_populated(
    db_path: Path | None = None,
    *,
    opener: Callable[[Path | None], Any] | None = None,
) -> CheckResult:
    """chunk_date is populated in content_vectors (required for TMP-7B temporal boost).

    Args:
        db_path: Override for the SQLite DB path. Defaults to
                 ``kairix.core.db.get_db_path()``.
        opener:  Public DI seam — when ``None`` the production
                 ``kairix.core.db.open_db`` is used; tests pass a raising
                 fake to drive the FileNotFoundError / generic-exception
                 branches without monkey-patching ``open_db``.
    """
    try:
        _opener: Callable[[Path | None], Any]
        if opener is None:
            from kairix.core.db import open_db

            _opener = open_db
        else:
            _opener = opener

        if db_path is None:
            from kairix.core.db import get_db_path

            db_path = Path(get_db_path())

        db = _opener(db_path)
        try:
            counts_or_error = _query_chunk_date_counts(db)
        finally:
            db.close()

        if isinstance(counts_or_error, CheckResult):
            return counts_or_error
        total, dated = counts_or_error
        return _grade_chunk_date_coverage(total, dated)

    except FileNotFoundError:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail="Index not found — vault not embedded yet",
            fix="Run: kairix embed",
        )
    except Exception as exc:
        return CheckResult(
            name=_CHECK_CHUNK_DATE_POPULATED,
            ok=False,
            detail=f"chunk_date check failed: {exc}",
            fix="Check kairix index at ~/.cache/kairix/index.sqlite",
        )


# ---------------------------------------------------------------------------
# MCP consumer harness checks
# ---------------------------------------------------------------------------
# kairix MCP server is a general service. Different consumers connect via
# different transports:
#
#   stdio (per-session subprocess) — OpenClaw, Claude Desktop, any orchestrator
#   SSE / HTTP (persistent process) — curl, generic HTTP MCP clients
#
# The check below probes whichever harnesses are detectable on this host.
# It passes if at least one harness is configured and functional.
# ---------------------------------------------------------------------------

_MCP_KAIRIX_SERVER_NAME = "mcp-kairix"

# ── OpenClaw ──────────────────────────────────────────────────────────────────
_OPENCLAW_JSON_PATHS = (
    str(Path.home() / ".openclaw" / "openclaw.json"),
    Path.home() / ".openclaw" / "openclaw.json",
)

# ── Claude Desktop ────────────────────────────────────────────────────────────
_CLAUDE_DESKTOP_CONFIG_PATHS = (
    # macOS
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    # Linux (XDG)
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "Claude" / "claude_desktop_config.json",
)

# ── SSE / HTTP ────────────────────────────────────────────────────────────────
# Env read lives in kairix.paths.mcp_port (F4 — env reads stay in paths/secrets).
_MCP_SSE_PORT = _mcp_port()


def _check_openclaw_config_file(p: Path) -> tuple[bool, str] | None:
    """Inspect one OpenClaw config file for a kairix MCP registration.

    Returns a (ok, detail) verdict when the file registers kairix,
    otherwise ``None`` so the caller continues to the next candidate.
    Extracted from ``probe_openclaw_harness`` so the per-candidate
    parse + dispatch chain doesn't have to live inside the outer
    for-loop's try/except — that nesting pushed the parent over F16.
    """
    import json as _json

    try:
        data = _json.loads(p.read_text())
    except (OSError, _json.JSONDecodeError):
        return None
    # OpenClaw stores MCP servers at mcp.servers (set via `openclaw mcp set`)
    mcp_servers = data.get("mcp", {}).get("servers", {})
    if _MCP_KAIRIX_SERVER_NAME not in mcp_servers:
        return None
    entry = mcp_servers[_MCP_KAIRIX_SERVER_NAME]
    cmd = entry.get("command", "")
    cmd_ok = bool(cmd) and Path(cmd).exists() and os.access(cmd, os.X_OK)
    if cmd_ok:
        return True, f"OpenClaw: registered in {p.name}, start command executable"
    return False, f"OpenClaw: registered but start command missing/not executable: {cmd}"


def probe_openclaw_harness(*, config_paths: tuple[Path | str, ...] | None = None) -> tuple[bool, str]:
    """Return (ok, detail) for the OpenClaw stdio harness.

    ``config_paths`` is the public seam — production callers leave it
    ``None`` and the function uses module-level ``_OPENCLAW_JSON_PATHS``;
    tests pass a tmp-path tuple to drive the registered / missing /
    bad-command branches without monkey-patching the constant.
    """
    paths = config_paths if config_paths is not None else _OPENCLAW_JSON_PATHS
    for candidate in paths:
        p = Path(str(candidate))
        if not p.exists():
            continue
        verdict = _check_openclaw_config_file(p)
        if verdict is not None:
            return verdict

    # Fallback: try openclaw CLI
    try:
        # safe: subprocess with trusted system binary (openclaw)
        result = subprocess.run(
            [
                "openclaw",
                "mcp",
                "list",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if _MCP_KAIRIX_SERVER_NAME in result.stdout:
            return True, "OpenClaw: registered (via 'openclaw mcp list')"
    except Exception:  # noqa: S110 — expected when openclaw not installed
        pass

    return False, "OpenClaw: not detected"


def probe_claude_desktop_harness(*, config_paths: tuple[Path, ...] | None = None) -> tuple[bool, str]:
    """Return (ok, detail) for the Claude Desktop stdio harness.

    ``config_paths`` is the public seam — production callers leave it
    ``None`` and the function uses module-level ``_CLAUDE_DESKTOP_CONFIG_PATHS``.
    """
    import json as _json

    paths = config_paths if config_paths is not None else _CLAUDE_DESKTOP_CONFIG_PATHS
    for candidate in paths:
        try:
            p = Path(str(candidate))
            if not p.exists():
                continue
            data = _json.loads(p.read_text())
            mcp_servers = data.get("mcpServers", {})
            if "kairix" in mcp_servers:
                entry = mcp_servers["kairix"]
                cmd = entry.get("command", "")
                return True, f"Claude Desktop: registered (command: {cmd})"
        except (OSError, _json.JSONDecodeError):
            continue

    return False, "Claude Desktop: not detected"


def probe_sse_harness() -> tuple[bool, str]:
    """Return (ok, detail) for the SSE/HTTP persistent service harness."""
    import socket

    # TCP probe on MCP SSE port
    try:
        with socket.create_connection(("127.0.0.1", _MCP_SSE_PORT), timeout=2):
            return True, f"SSE/HTTP: listening on port {_MCP_SSE_PORT}"
    except OSError:
        pass

    # Fallback: check systemd unit exists and is active
    try:
        # safe: subprocess with trusted system binary (systemctl)
        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                "kairix-mcp.service",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        state = result.stdout.strip()
        if state == "active":
            return (
                True,
                f"SSE/HTTP: kairix-mcp.service active (port {_MCP_SSE_PORT} not yet listening — may still be starting)",
            )
        elif state not in ("", "inactive", "failed", "unknown"):
            return False, f"SSE/HTTP: kairix-mcp.service state={state}"
    except Exception:  # noqa: S110 — expected when systemctl not available
        pass

    return False, f"SSE/HTTP: not listening on port {_MCP_SSE_PORT}"


def check_mcp_service(
    *,
    openclaw_probe: Callable[..., tuple[bool, str]] | None = None,
    claude_desktop_probe: Callable[..., tuple[bool, str]] | None = None,
    sse_probe: Callable[..., tuple[bool, str]] | None = None,
) -> CheckResult:
    """
    kairix MCP server is reachable by at least one configured consumer.

    Probes each transport harness that is detectable on this host:
      - OpenClaw (stdio): mcp-kairix registered in openclaw.json
      - Claude Desktop (stdio): kairix registered in claude_desktop_config.json
      - SSE/HTTP (persistent): port 7443 listening or kairix-mcp.service active

    Passes if at least one harness is configured and functional.
    If no harness is detected, reports which harnesses are available to configure.

    The three ``*_probe`` kwargs are the public DI seams — production
    callers leave them ``None`` and the function uses the module-level
    ``_probe_*_harness`` defaults; tests inject stubs to drive each
    harness's outcome without monkey-patching the module attributes.
    """
    openclaw_ok, openclaw_detail = (openclaw_probe or probe_openclaw_harness)()
    claude_ok, claude_detail = (claude_desktop_probe or probe_claude_desktop_harness)()
    sse_ok, sse_detail = (sse_probe or probe_sse_harness)()

    active = [
        d
        for ok, d in [
            (openclaw_ok, openclaw_detail),
            (claude_ok, claude_detail),
            (sse_ok, sse_detail),
        ]
        if ok
    ]
    inactive = [
        d
        for ok, d in [
            (openclaw_ok, openclaw_detail),
            (claude_ok, claude_detail),
            (sse_ok, sse_detail),
        ]
        if not ok
    ]

    if active:
        return CheckResult(
            name=_CHECK_MCP_SERVICE,
            ok=True,
            detail="kairix MCP server accessible — " + "; ".join(active),
        )

    return CheckResult(
        name=_CHECK_MCP_SERVICE,
        ok=False,
        detail="kairix MCP server not configured for any consumer — " + "; ".join(inactive),
        fix=(
            "Configure at least one MCP consumer harness:\n\n"
            "  OpenClaw (stdio):\n"
            "    openclaw mcp set mcp-kairix "
            '\'{"type":"stdio","command":"/path/to/kairix-start.sh"}\'\n\n'
            "  Claude Desktop (stdio): add to ~/Library/Application Support/Claude/claude_desktop_config.json:\n"
            '    {"mcpServers": {"kairix": {"command": "kairix", "args": ["mcp", "serve"]}}}\n\n'
            "  SSE/HTTP (persistent service):\n"
            "    sudo systemctl enable --now kairix-mcp.service\n"
            "    # or: kairix mcp serve --transport sse --port 7443 &\n"
        ),
    )


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------


def check_query_cache_stats(query_cache: Any | None = None) -> CheckResult:
    """Diagnostic: report query-result cache stats (#281).

    Always passes — the existence of this check is so operators can
    see the cache hit-rate in ``kairix onboard check --json``. A
    process that has run zero queries reports size=0, hit_rate=0.0
    and still passes.

    Args:
        query_cache: Override for the process-shared
            :class:`QueryResultCache`. Tests pass an explicit instance
            rather than relying on the lazy module-level singleton.
    """
    try:
        if query_cache is None:
            from kairix.core.factory import get_query_cache

            query_cache = get_query_cache()
        stats = query_cache.stats()
        detail = (
            f"query cache: size={stats.size}, hits={stats.hits}, "
            f"misses={stats.misses}, hit_rate={stats.hit_rate:.2f}, "
            f"oldest_age_s={stats.oldest_entry_age_s:.1f}, "
            f"evictions={stats.evictions}"
        )
        return CheckResult(name=_CHECK_QUERY_CACHE_STATS, ok=True, detail=detail)
    except Exception as exc:
        # Diagnostic check must never block onboarding; a missing cache
        # is reported as a passing check with a degraded detail string
        # rather than a failure (operators see the warning, not a red).
        return CheckResult(
            name=_CHECK_QUERY_CACHE_STATS,
            ok=True,
            detail=f"query cache: unavailable ({exc})",
        )


def check_embed_cache_stats(embed_cache: Any | None = None) -> CheckResult:
    """Diagnostic: report embed-cache stats.

    The embed cache (``kairix.transport.cache``) sits in front
    of the Azure embed roundtrip — same text → same vector regardless
    of which agent / scope asked. This check exists so operators can
    see hit-rate / size in ``kairix onboard check --json`` alongside
    the result-cache (#281) stats. A process that has run zero embeds
    reports size=0, hit_rate=0.0 and still passes.

    Args:
        embed_cache: Override for the process-shared
            :class:`EmbedCache`. Tests pass an explicit instance
            rather than relying on the lazy module-level singleton.
    """
    try:
        if embed_cache is None:
            from kairix.transport.cache import get_embed_cache

            embed_cache = get_embed_cache()
        stats = embed_cache.stats()
        detail = (
            f"embed cache: size={stats.size}, hits={stats.hits}, "
            f"misses={stats.misses}, hit_rate={stats.hit_rate:.2f}, "
            f"oldest_age_s={stats.oldest_entry_age_s:.1f}, "
            f"evictions={stats.evictions}"
        )
        return CheckResult(name=_CHECK_EMBED_CACHE_STATS, ok=True, detail=detail)
    except Exception as exc:
        # Diagnostic check must never block onboarding; a missing cache
        # is reported as a passing check with a degraded detail string
        # rather than a failure (operators see the warning, not a red).
        return CheckResult(
            name=_CHECK_EMBED_CACHE_STATS,
            ok=True,
            detail=f"embed cache: unavailable ({exc})",
        )


# ---------------------------------------------------------------------------
# v2026.5.24a1 — topology + SharePoint credential checks
# ---------------------------------------------------------------------------
# Each check below is gated by a feature flag — when the flag is OFF the
# check returns ok=True with a "skipped (flag off)" detail so a fresh
# deployment sees the check exists but is inert. Once the operator flips
# the corresponding flag, the check exercises the live config / DB /
# secrets path.
#
# Default flag values are read through the standard resolver
# (``kairix.core.features.flag``) so the same env-var / config-overlay /
# registry-default chain applies as everywhere else in kairix.


_CONNECTOR_SHAREPOINT_FLAG = "connector_sharepoint"
_MAINTENANCE_LOOP_FLAG = "maintenance_loop"
# F17 — the "flag off, check is inert" detail line shows up on every
# sharepoint check when the flag is off. Pull to a single constant so
# the wording stays uniform across the checks.
_SKIPPED_FLAG_OFF_DETAIL_TEMPLATE = "skipped — {flag} flag is OFF (default-safe)"

# F17 — the three M365 secret names are referenced from both the
# credential-resolution loop and the failure-detail message.
_SHAREPOINT_SECRET_NAMES: tuple[str, ...] = (
    "connector-m365-tenant-id",  # pragma: allowlist secret — secret slot name, not a credential
    "connector-m365-client-id",  # pragma: allowlist secret — secret slot name, not a credential
    "connector-m365-client-secret",  # pragma: allowlist secret — secret slot name, not a credential
)


def _default_flag_reader(name: str) -> bool:
    """Production seam — defers to ``kairix.core.features.flag``.

    Lazy import so the onboard-check module stays importable on a fresh
    install where the registry module has heavy transitive deps.
    """
    from kairix.core.features import flag as _flag

    return _flag(name)


@dataclass
class TopologyCheckDeps:
    """Injectable dependencies for the topology + SharePoint checks.

    Bundles the three DI seams the three v2026.5.24a1 checks need
    (feature-flag reader, config loader, DB cc_pair namer, secret
    reader) into a single Deps class so the check signatures stay free
    of test-only ``*_loader=None`` kwargs (F6).

    Production callers leave the dataclass at its default values; tests
    construct ``TopologyCheckDeps(flag_reader=..., config_loader=...,
    db_cc_pair_namer=..., secret_reader=...)`` to drive each check's
    branches without monkey-patching the live registry / config /
    secrets paths. Mirrors the existing :class:`OnboardChecksDeps`
    pattern in this module.

    Each field defaults via ``field(default_factory=...)`` so a
    consumer can override individual seams without re-specifying the
    others — the production defaults all delegate to the standard
    feature-flag resolver / config loader / SQLite reader / secret
    resolver.
    """

    flag_reader: Callable[[str], bool] = field(default_factory=lambda: _default_flag_reader)
    config_loader: Callable[[], dict[str, Any] | None] = field(default_factory=lambda: _default_overlay_path_loader)
    db_cc_pair_namer: Callable[[], frozenset[str]] = field(default_factory=lambda: _default_db_cc_pair_names)
    db_scope_actor_id_reader: Callable[[], tuple[str, ...]] = field(default_factory=lambda: _default_db_scope_actor_ids)
    secret_reader: Callable[[str], str | None] = field(default_factory=lambda: _default_secret_reader)


def _default_overlay_path_loader() -> dict[str, Any] | None:
    """Production seam — loads the topology section from kairix.config.yaml.

    Returns the parsed YAML dict (or None when no config file is found
    or the parse fails). The actual topology sub-section is read by
    the caller; this loader returns the full document so the caller
    sees the same shape ``kairix.config.topology.parse_topology``
    expects.

    Env-var read for the config path goes through
    ``kairix.paths.config_path_override`` so the F4 boundary holds
    (env reads live in paths.py / secrets.py only).

    Lazy yaml import so the module stays light when topology is off.
    """
    import yaml as _yaml

    from kairix.paths import config_path_override

    config_path_str = config_path_override() or "kairix.config.yaml"
    p = Path(config_path_str).expanduser()
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = _yaml.safe_load(f) or {}
    except (OSError, _yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def check_topology_config_valid(deps: TopologyCheckDeps | None = None) -> CheckResult:
    """topology: block in kairix.config.yaml parses + passes cross-reference validation.

    Parses the ``topology:`` block out of the active
    ``kairix.config.yaml`` (resolved via the standard ``KAIRIX_CONFIG_PATH``
    env-var or the ``kairix.config.yaml`` default), runs the 5
    cross-reference rules from
    ``kairix.config.topology_validators.validate_topology_references``,
    and reports either ok=True (zero failures) or ok=False with the
    failure messages compacted into the ``detail`` string.

    ``deps`` is the public DI seam (default :class:`TopologyCheckDeps`
    binds the production flag/config readers). Tests construct a Deps
    with substitute callables to drive the parse-error / validation-
    failure / clean branches without touching the live config file.

    ``topology_config`` retired post-cutover (task #132); this check
    no longer short-circuits on the flag.
    """
    d = deps if deps is not None else TopologyCheckDeps()

    try:
        data = d.config_loader()
    except Exception as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CONFIG_VALID,
            ok=False,
            detail=f"config loader raised: {exc}",
            fix="fix: ensure kairix.config.yaml exists and is readable. next: run `kairix config validate`.",
        )

    if data is None:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CONFIG_VALID,
            ok=False,
            detail="kairix.config.yaml not found — no config to validate",
            fix=(
                "fix: create kairix.config.yaml at the repo root (or set KAIRIX_CONFIG_PATH) "
                "with a topology: block per kairix.config.example.yaml. "
                "next: run `kairix config validate`."
            ),
        )

    from kairix.config.topology import TopologyParseError, parse_topology
    from kairix.config.topology_validators import validate_topology_references

    try:
        parsed = parse_topology(data)
    except TopologyParseError as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CONFIG_VALID,
            ok=False,
            detail=f"topology parse failed: {exc}",
            fix="fix: correct the YAML shape in kairix.config.yaml. next: run `kairix config validate`.",
        )

    failures = validate_topology_references(parsed)
    if failures:
        summary = "; ".join(f.message for f in failures[:3])
        suffix = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
        return CheckResult(
            name=_CHECK_TOPOLOGY_CONFIG_VALID,
            ok=False,
            detail=f"{len(failures)} topology cross-reference failure(s): {summary}{suffix}",
            fix=(
                "fix: declare the missing entries or remove the dangling references. "
                "next: run `kairix config validate`."
            ),
        )

    counts = (
        f"connectors={len(parsed.connectors)}, credentials={len(parsed.credentials)}, "
        f"cc_pairs={len(parsed.cc_pairs)}, collections={len(parsed.collections)}, "
        f"scope_profiles={len(parsed.scope_profiles)}, skills={len(parsed.skills)}"
    )
    return CheckResult(
        name=_CHECK_TOPOLOGY_CONFIG_VALID,
        ok=True,
        detail=f"topology config valid ({counts})",
    )


def check_topology_cc_pairs_registered(deps: TopologyCheckDeps | None = None) -> CheckResult:
    """Every declared cc_pair in kairix.config.yaml has a row in topology_cc_pairs.

    Parses the declared cc_pair names from ``kairix.config.yaml`` and
    cross-checks against the live ``topology_cc_pairs`` table (read via
    the production ``list_cc_pairs`` helper). A declared cc_pair without
    a matching DB row means the apply-bridge hasn't run for it yet —
    typically the worker hasn't been restarted since the YAML edit (the
    apply-bridge runs at boot).

    ``topology_config`` retired post-cutover (task #132); this check
    no longer short-circuits on the flag.

    ``deps`` is the public DI seam (default :class:`TopologyCheckDeps`
    binds the production flag / config / DB readers).
    """
    d = deps if deps is not None else TopologyCheckDeps()

    try:
        data = d.config_loader()
    except Exception as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=False,
            detail=f"config loader raised: {exc}",
            fix=(
                "fix: ensure kairix.config.yaml exists and is readable. "
                "next: restart the worker so the apply-bridge re-runs."
            ),
        )
    if data is None:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=True,
            detail="no kairix.config.yaml — nothing to register",
        )

    from kairix.config.topology import TopologyParseError, parse_topology

    try:
        parsed = parse_topology(data)
    except TopologyParseError as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=False,
            detail=f"topology parse failed: {exc}",
            fix="fix: correct the YAML shape in kairix.config.yaml. next: run `kairix config validate`.",
        )

    declared_names = frozenset(p.name for p in parsed.cc_pairs)
    if not declared_names:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=True,
            detail="no cc_pairs declared in topology — nothing to register",
        )

    try:
        registered = d.db_cc_pair_namer()
    except Exception as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=False,
            detail=f"topology_cc_pairs lookup failed: {exc}",
            fix=("fix: ensure the SQLite database is reachable. next: restart the worker so the apply-bridge re-runs."),
        )

    missing = sorted(declared_names - registered)
    if missing:
        return CheckResult(
            name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
            ok=False,
            detail=(f"{len(missing)} declared cc_pair(s) not registered in topology_cc_pairs: {', '.join(missing)}"),
            fix=(
                "fix: restart the worker — the apply-bridge runs at boot "
                "and materialises declared cc_pairs idempotently."
            ),
        )

    return CheckResult(
        name=_CHECK_TOPOLOGY_CC_PAIRS_REGISTERED,
        ok=True,
        detail=f"{len(declared_names)} declared cc_pair(s) registered",
    )


def _default_db_cc_pair_names() -> frozenset[str]:
    """Production seam — return the set of cc_pair names from topology_cc_pairs."""
    from kairix.core.connectors.cc_pair import list_cc_pairs
    from kairix.core.db import get_db_path, open_db

    db = open_db(Path(get_db_path()))
    try:
        rows = list_cc_pairs(db)
    finally:
        db.close()
    return frozenset(row.name for row in rows)


def _default_scope_entries_columns() -> frozenset[str]:
    """Production seam — return the set of column names on topology_scope_entries.

    Opens the kairix DB and runs ``PRAGMA table_info(topology_scope_entries)``;
    returns a frozenset of column names so the check can detect presence /
    absence of the GH #373 ``default_in_scope`` column added by the schema
    migration. Returns an empty frozenset when the table doesn't exist (a
    fresh DB before ``create_schema`` runs).
    """
    from kairix.core.db import get_db_path, open_db

    db = open_db(Path(get_db_path()))
    try:
        # F63-bounded: PRAGMA table_info returns one row per column (≤O(20) for the topology_scope_entries shape).
        rows = db.execute("PRAGMA table_info(topology_scope_entries)").fetchall()
    finally:
        db.close()
    return frozenset(row[1] for row in rows)


@dataclass
class DefaultInScopeCheckDeps:
    """Injectable dependencies for the GH #373 default_in_scope migration check.

    Mirrors :class:`TopologyCheckDeps` but scoped to the schema-only check
    — the migration is independent of the operator-facing
    ``topology_config`` block, so the check uses its own Deps class with
    a single ``columns_reader`` seam. Tests pass a substitute callable
    returning ``frozenset({"default_in_scope", ...})`` or ``frozenset()``
    to drive the present / absent branches without touching the live DB.
    """

    columns_reader: Callable[[], frozenset[str]] = field(default_factory=lambda: _default_scope_entries_columns)


def check_topology_default_in_scope_field_present(
    deps: DefaultInScopeCheckDeps | None = None,
) -> CheckResult:
    """GH #373 schema migration — ``topology_scope_entries.default_in_scope`` exists.

    The schema migration in :mod:`kairix.core.db.schema` adds the
    ``default_in_scope INTEGER NOT NULL DEFAULT 1`` column to
    ``topology_scope_entries`` at boot. This check confirms the migration
    actually applied — operators upgrading to v2026.6+ without restarting
    the worker (e.g. a hot-reload that bypassed ``create_schema``) would
    see the resolver fall back to its pre-migration code path; this check
    surfaces that drift before the next default search runs.

    Reports ok=True when the column is present; ok=False otherwise.
    Tolerates a fresh DB (no ``topology_scope_entries`` table yet) by
    treating "table missing" as a separate failure mode with the same
    remediation (restart triggers ``create_schema`` which both creates
    the table and runs the migration).
    """
    d = deps if deps is not None else DefaultInScopeCheckDeps()
    try:
        cols = d.columns_reader()
    except Exception as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT,
            ok=False,
            detail=f"columns reader raised: {exc}",
            fix=(
                "fix: confirm the kairix DB is reachable + readable. "
                "next: run `kairix onboard check document_root_configured`. "
                "run: ls -la $(kairix paths db)."
            ),
        )

    if not cols:
        return CheckResult(
            name=_CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT,
            ok=False,
            detail=(
                "topology_scope_entries table not present — schema migration has not run. "
                "Restart the worker to trigger create_schema()."
            ),
            fix=(
                "fix: restart the kairix worker / API process to trigger "
                "kairix.core.db.schema.create_schema. "
                "next: re-run `kairix onboard check topology_default_in_scope_field_present`. "
                "run: docker compose restart kairix-worker kairix-1."
            ),
        )

    if "default_in_scope" not in cols:
        return CheckResult(
            name=_CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT,
            ok=False,
            detail=(
                "topology_scope_entries missing default_in_scope column — GH #373 "
                "schema migration did not apply. The resolver falls back to its "
                "pre-migration code path (treats every row as in-default)."
            ),
            fix=(
                "fix: restart the kairix worker / API process to trigger the "
                "ALTER TABLE migration in kairix.core.db.schema.migrate. "
                "next: re-run `kairix onboard check topology_default_in_scope_field_present`. "
                "run: docker compose restart kairix-worker kairix-1."
            ),
        )

    return CheckResult(
        name=_CHECK_TOPOLOGY_DEFAULT_IN_SCOPE_FIELD_PRESENT,
        ok=True,
        detail="topology_scope_entries.default_in_scope column present (GH #373 schema migration applied)",
    )


def _default_db_scope_actor_ids() -> tuple[str, ...]:
    """Production seam — return distinct actor_id values from topology_scope_entries.

    Joins ``topology_scope_entries`` against
    ``topology_scope_profiles`` so the check sees the same field the
    runtime resolver reads. Empty DB / missing table → empty tuple
    (the caller treats that as a pass).
    """
    import sqlite3

    from kairix.core.db import get_db_path, open_db

    db = open_db(Path(get_db_path()))
    try:
        try:
            rows = db.execute(
                "SELECT DISTINCT sp.actor_id "
                "FROM topology_scope_profiles sp "
                "JOIN topology_scope_entries se ON se.scope_profile_id = sp.id "
                "ORDER BY sp.actor_id "
                "LIMIT 10000"  # F63-bounded: scope_profiles is operator-config sized (≤O(100))
            ).fetchall()
        except sqlite3.OperationalError:
            # Schema not migrated (no topology_scope_* tables yet) — treat as pass.
            return ()
    finally:
        db.close()
    return tuple(row[0] for row in rows)


def check_topology_wildcard_expansion_resolved(deps: TopologyCheckDeps | None = None) -> CheckResult:
    """GH #373 — every wildcard ``applies_to: ["*"]`` is expanded in the DB.

    The config loader materialises every wildcard into concrete per-actor
    scope_profile rows so the resolver never sees a literal ``"*"``
    actor_id. A ``"*"`` in ``topology_scope_profiles.actor_id`` means
    the loader did not run (or the operator edited the DB directly) — a
    misconfiguration that silently breaks default-scope resolution for
    every agent the wildcard was meant to cover.

    ``topology_config`` retired post-cutover (task #132); this check
    no longer short-circuits on the flag.

    ``deps`` is the public DI seam. Production callers leave ``deps=None``;
    tests substitute the ``db_cc_pair_namer`` slot via a custom callable
    that mirrors the actor_id-reading shape used here (so the test
    doesn't need to seed the v2 SQL tables to drive the check's branches).
    """
    d = deps if deps is not None else TopologyCheckDeps()

    try:
        actor_ids = d.db_scope_actor_id_reader()
    except Exception as exc:
        return CheckResult(
            name=_CHECK_TOPOLOGY_WILDCARD_EXPANSION_RESOLVED,
            ok=False,
            detail=f"topology_scope_profiles lookup failed: {exc}",
            fix=(
                "fix: ensure the SQLite database is reachable and the topology "
                "schema migration has run. next: restart the worker."
            ),
        )

    unresolved = sorted(a for a in actor_ids if a == "*" or "*" in a)
    if unresolved:
        names = ", ".join(unresolved)
        return CheckResult(
            name=_CHECK_TOPOLOGY_WILDCARD_EXPANSION_RESOLVED,
            ok=False,
            detail=(
                f"{len(unresolved)} unexpanded wildcard actor_id(s) in topology_scope_profiles: {names}. "
                f"The Wave B config loader should have materialised these into per-agent rows."
            ),
            fix=(
                "fix: restart the worker so the config loader re-runs and expands "
                '`applies_to: ["*"]` against the registered agents block. '
                "next: re-run `kairix onboard check`."
            ),
        )

    return CheckResult(
        name=_CHECK_TOPOLOGY_WILDCARD_EXPANSION_RESOLVED,
        ok=True,
        detail=f"{len(actor_ids)} distinct scope actor_id(s) — no unexpanded wildcards",
    )


def check_sharepoint_credentials_loaded(deps: TopologyCheckDeps | None = None) -> CheckResult:
    """SharePoint connector secrets resolve via kairix.secrets.get_secret.

    When the ``connector_sharepoint`` flag is OFF, returns ok=True with
    a "skipped" detail.

    When ON, attempts to resolve the three M365 client-credentials
    secrets the SharePoint connector requires:

      * ``connector-m365-tenant-id``
      * ``connector-m365-client-id``
      * ``connector-m365-client-secret``

    Reports ok=True iff all three resolve to non-empty strings; ok=False
    with the missing-name list otherwise. The secret resolution chain
    is the standard ``kairix.secrets.get_secret`` cascade (env var →
    per-file secret → bundle file → Azure Key Vault), so the same fix
    applies whether the operator is on Docker / pip / VM.

    ``deps`` is the public DI seam (default :class:`TopologyCheckDeps`
    binds the production flag + secret readers). Tests construct a
    Deps with substitute callables to drive the present / partial /
    missing branches without touching the live environment or Key
    Vault.
    """
    d = deps if deps is not None else TopologyCheckDeps()
    if not d.flag_reader(_CONNECTOR_SHAREPOINT_FLAG):
        return CheckResult(
            name=_CHECK_SHAREPOINT_CREDENTIALS_LOADED,
            ok=True,
            detail=_SKIPPED_FLAG_OFF_DETAIL_TEMPLATE.format(flag=_CONNECTOR_SHAREPOINT_FLAG),
        )

    missing: list[str] = []
    for name in _SHAREPOINT_SECRET_NAMES:
        try:
            value = d.secret_reader(name)
        except Exception:
            value = None
        if not value:
            missing.append(name)

    if missing:
        return CheckResult(
            name=_CHECK_SHAREPOINT_CREDENTIALS_LOADED,
            ok=False,
            detail=f"{len(missing)} SharePoint secret(s) unresolved: {', '.join(missing)}",
            fix=(
                "fix: set the three M365 client-credentials secrets that the SharePoint "
                "connector resolves via kairix.secrets.get_secret — "
                "`connector-m365-tenant-id`, `connector-m365-client-id`, and "
                "`connector-m365-client-secret`. next: re-run "
                "`kairix onboard check sharepoint_credentials_loaded`."
            ),
        )

    return CheckResult(
        name=_CHECK_SHAREPOINT_CREDENTIALS_LOADED,
        ok=True,
        detail=f"{len(_SHAREPOINT_SECRET_NAMES)} SharePoint secret(s) resolved",
    )


def _default_secret_reader(name: str) -> str | None:
    """Production seam — defers to ``kairix.secrets.get_secret``.

    Uses ``required=False`` so a missing secret returns ``None`` rather
    than raising — the calling check translates "any None" into a
    failure with the missing-name list.
    """
    from kairix.secrets import get_secret

    return get_secret(name, required=False)


@dataclass
class MaintenanceLoopCheckDeps:
    """Injectable dependencies for :func:`check_maintenance_loop_ticking`.

    F6-clean: every field has a ``default_factory`` so production callers
    omit the Deps and get the real boundary calls; tests pass a Deps with
    substitute callables to drive the flag-OFF / never-ticked / ticking /
    stalled branches without touching the live worker-state JSON or the
    feature-flag registry.

    Fields:
      * ``flag_reader`` — returns the effective value of the named flag.
        Default :func:`_default_flag_reader` (delegates to
        :func:`kairix.core.features.flag`).
      * ``state_reader`` — returns a :class:`WorkerState`-shaped object
        (or ``None`` when no state file exists). Default
        :func:`_default_worker_state_reader` which delegates to
        :func:`kairix.worker_state.read_state` against
        :func:`kairix.paths.worker_state_path`.
      * ``interval_reader`` — returns the maintenance interval in
        seconds. Default :func:`maintenance_interval_seconds`.
      * ``clock`` — ``time.time()``-like callable; tests pin to make
        the jitter-window comparison deterministic.
    """

    flag_reader: Callable[[str], bool] = field(default_factory=lambda: _default_flag_reader)
    state_reader: Callable[[], Any] = field(default_factory=lambda: _default_worker_state_reader)
    interval_reader: Callable[[], int] = field(default_factory=lambda: _default_maintenance_interval_reader)
    clock: Callable[[], float] = field(default_factory=lambda: _default_clock)


def _default_worker_state_reader() -> Any:
    """Production seam — return the persisted ``WorkerState`` or ``None``."""
    from kairix.paths import worker_state_path
    from kairix.worker_state import read_state

    return read_state(worker_state_path())


def _default_maintenance_interval_reader() -> int:
    """Production seam — delegates to :func:`kairix.paths.maintenance_interval_seconds`."""
    from kairix.paths import maintenance_interval_seconds

    return maintenance_interval_seconds()


def _default_clock() -> float:
    """Production seam — wall-clock ``time.time()``."""
    import time as _time

    return _time.time()


def check_maintenance_loop_ticking(deps: MaintenanceLoopCheckDeps | None = None) -> CheckResult:
    """KFEAT-021 Phase 1 — the maintenance loop is firing within its jitter window.

    When the ``maintenance_loop`` flag is OFF, returns ok=True with a
    "skipped (flag off, default-safe)" detail.

    When ON:
      * If the worker has never ticked (``last_maintenance_tick_at == 0``),
        report ok=False with the "no tick yet" remediation pointing at
        the on-demand ``kairix worker maintenance`` verb.
      * If the last tick is older than ``interval * 1.5`` (50% jitter
        window per the brief), report ok=False with the stalled-loop
        remediation.
      * Otherwise report ok=True with the cadence delta in the detail.

    ``deps`` is the public DI seam.
    """
    d = deps if deps is not None else MaintenanceLoopCheckDeps()
    if not d.flag_reader(_MAINTENANCE_LOOP_FLAG):
        return CheckResult(
            name=_CHECK_MAINTENANCE_LOOP_TICKING,
            ok=True,
            detail=_SKIPPED_FLAG_OFF_DETAIL_TEMPLATE.format(flag=_MAINTENANCE_LOOP_FLAG),
        )

    state = d.state_reader()
    last_tick = float(getattr(state, "last_maintenance_tick_at", 0.0)) if state is not None else 0.0
    interval = max(1, int(d.interval_reader()))
    now = float(d.clock())

    if last_tick <= 0.0:
        return CheckResult(
            name=_CHECK_MAINTENANCE_LOOP_TICKING,
            ok=False,
            detail=(
                "maintenance_loop flag is ON but no tick has been recorded yet "
                "(WorkerState.last_maintenance_tick_at == 0)"
            ),
            fix=(
                "fix: wait one maintenance interval for the first tick to fire, "
                "OR run `kairix worker maintenance` for an on-demand tick. "
                "next: re-run `kairix onboard check maintenance_loop_ticking` "
                "after the worker logs `event=maintenance_tick_completed`."
            ),
        )

    delta = now - last_tick
    jitter_cap = interval * 1.5
    if delta > jitter_cap:
        return CheckResult(
            name=_CHECK_MAINTENANCE_LOOP_TICKING,
            ok=False,
            detail=(
                f"maintenance loop stalled — last tick {int(delta)}s ago (interval={interval}s, "
                f"jitter cap={int(jitter_cap)}s)"
            ),
            fix=(
                "fix: confirm the kairix worker process is running and not paused. "
                "next: tail worker logs for `event=maintenance_tick_failed` lines; "
                "an exception in any stage skips that tick but does not crash the loop. "
                "run: kairix worker status"
            ),
        )

    return CheckResult(
        name=_CHECK_MAINTENANCE_LOOP_TICKING,
        ok=True,
        detail=f"last tick {int(delta)}s ago (interval={interval}s, within jitter window)",
    )


# Map every declared kairix extractor plugin to the libraries its
# converters need at runtime. If markitdown is registered but its
# per-format converter libraries (python-docx, openpyxl, etc.) aren't
# installed, extraction silently fails at extract() time with a
# MissingDependencyException — the v2026.5.26a1 dogfood incident
# (#322) where 8,785 SharePoint items dead-lettered because the
# production Docker image was missing every binary-doc converter.
#
# The check_extractor_libraries_importable check below imports each
# of these at onboard time so the failure surfaces BEFORE the worker
# starts processing items.
_EXTRACTOR_LIBRARY_DEPS: dict[str, tuple[str, ...]] = {
    "markitdown": ("markitdown", "markitdown.converters", "docx", "openpyxl", "pptx", "olefile"),
    "passthrough": (),  # passthrough has no library deps
    "pdf_fallback": ("pdfplumber",),
    "docx": ("docx",),
    "pptx": ("pptx",),
    "xlsx": ("openpyxl",),
    "ocr": ("pytesseract", "PIL"),
}


def check_extractor_libraries_importable() -> CheckResult:
    """Every declared extractor plugin's runtime libraries import cleanly.

    Kairix's extractor framework uses pip entry points — declaring an
    extractor in pyproject.toml's ``[project.entry-points."kairix.extractors"]``
    table makes it discoverable by name, but the library it depends on
    (e.g. python-docx for the markitdown DOCX converter) is loaded
    lazily at extract() time. If the runtime image doesn't install the
    matching extra (e.g. ``markitdown[docx]``), the entry-point
    registration succeeds while every extract() call raises
    ``MissingDependencyException``. The failure mode landed on the
    v2026.5.26a1 production VM (#322) — 8,785 SharePoint items
    dead-lettered because the Docker image was missing all five
    binary-doc converter extras.

    This check walks ``kairix.extractors.<name>.CAPABILITIES`` / entry
    points and tries to import each declared library at startup so the
    missing-extras failure surfaces in ``kairix onboard check`` BEFORE
    the worker starts processing.
    """
    import importlib

    missing: dict[str, list[str]] = {}
    checked_count = 0
    for extractor_name, libraries in _EXTRACTOR_LIBRARY_DEPS.items():
        for lib in libraries:
            checked_count += 1
            try:
                importlib.import_module(lib)
            except ImportError:
                missing.setdefault(extractor_name, []).append(lib)
    if missing:
        per_extractor = "; ".join(f"{name}={','.join(libs)}" for name, libs in missing.items())
        return CheckResult(
            name=_CHECK_EXTRACTOR_LIBRARIES_IMPORTABLE,
            ok=False,
            detail=f"missing converter libraries: {per_extractor}",
            fix=(
                "Install the missing extras into the runtime image: "
                "`pip install 'Kairix-agentic-knowledge-mgt[markitdown,pdf_fallback,docx,pptx,xlsx]'`. "
                "On Docker, edit the Dockerfile install line to include the extras + rebuild."
            ),
        )
    return CheckResult(
        name=_CHECK_EXTRACTOR_LIBRARIES_IMPORTABLE,
        ok=True,
        detail=f"all {checked_count} declared extractor library imports succeeded",
    )


# ---------------------------------------------------------------------------
# PLA-298 — honest per-agent memory-writability probe
# ---------------------------------------------------------------------------
# The old onboard check passed 18/18 on a box where the 04-Agent-Knowledge
# overlay was read-only for the agent's uid, because it never probed
# writability — memory_write / ingest_chat then died with EACCES at runtime.
#
# This leg resolves each configured agent's ACTUAL write destination through the
# SAME resolver the runtime write path (remember / ingest_chat) uses —
# resolve_writable_memory_dir — which PREFERS the ADR-017 04-Agent-Knowledge
# overlay but FALLS BACK to a writable path under the kairix data dir when the
# overlay is read-only for our uid (PLA-296). The verdict is whether that
# resolved destination is writable, so the probe reflects the real write
# capability rather than the overlay alone.
#
# On a hardened, read-only-root enterprise box the overlay is legitimately :ro
# and the data-dir fallback IS the correct write location (F94 — persist through
# kairix.paths, never a system path); agents reach memory through
# commands/connectors, so where it is stored does not matter as long as it is
# always writable and searchable. So a read-only overlay whose data-dir fallback
# IS writable PASSES (surfaced as ok(fallback)) — it must NOT block the deploy
# gate (#689). A hard FAIL is reserved for an agent that can persist NOWHERE: a
# non-fallback error (e.g. ENOSPC disk-full) or an unwritable data dir.


def _default_agent_memory_config() -> dict[str, Any] | None:
    """Production seam — the parsed top-level kairix.config.yaml (agents block)."""
    from kairix.paths import load_top_level_config

    return load_top_level_config()


def _default_agent_memory_document_root() -> Path:
    """Production seam — the resolved document root the overlay lives under."""
    from kairix.paths import document_root

    return document_root()


def _default_agent_memory_fallback_root() -> Path:
    """Production seam — the writable data-dir base agent memory falls back to (PLA-296).

    The same :func:`kairix.paths.agent_memory_fallback_root` the runtime write
    path (``remember`` / ``ingest_chat``) uses when the preferred overlay is
    read-only, so the probe resolves the destination identically.
    """
    from kairix.paths import agent_memory_fallback_root

    return agent_memory_fallback_root()


def _nearest_existing_dir(path: Path) -> Path:
    """Walk up from ``path`` to the first existing directory (its own or an ancestor)."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _default_agent_memory_probe(path: Path) -> Any:
    """Production seam — probe the nearest existing ancestor's writability.

    The live write (``remember`` / ``ingest_chat``) does ``mkdir(parents=True) +
    write_text`` under the agent's memory root, so "can I create + write here?"
    is faithfully answered by probing the deepest EXISTING ancestor. Because
    that ancestor already exists, ``probe_write_access``'s create-on-demand is a
    no-op there — the check leaves NO lasting directory behind, a read-only
    overlay still surfaces EACCES/EROFS from the existing mount, and a fresh
    per-agent dir isn't false-failed as ENOENT.
    """
    from kairix.paths import probe_write_access

    return probe_write_access(_nearest_existing_dir(path))


@dataclass
class AgentMemoryWritableCheckDeps:
    """Injectable dependencies for :func:`check_agent_memory_writable` (PLA-298).

    F6-clean: each field has a ``default_factory`` wiring the production
    boundary. Tests construct
    ``AgentMemoryWritableCheckDeps(document_root_fn=lambda: tmp, config_loader=...)``
    to point the probe at a ``tmp_path`` memory root — so the check runs a REAL
    filesystem write attempt (F71 count-equals-ground-truth) without a fake
    probe and without touching the live document tree.
    """

    config_loader: Callable[[], dict[str, Any] | None] = field(default_factory=lambda: _default_agent_memory_config)
    document_root_fn: Callable[[], Path] = field(default_factory=lambda: _default_agent_memory_document_root)
    memory_fallback_root_fn: Callable[[], Path] = field(default_factory=lambda: _default_agent_memory_fallback_root)
    probe_fn: Callable[[Path], Any] = field(default_factory=lambda: _default_agent_memory_probe)


def _fallback_key(label: str) -> str:
    """Filesystem-safe per-agent subdir under the data-dir fallback root.

    Mirrors the runtime write path's ``fallback_root / <agent>`` namespacing
    (``remember`` / ``ingest_chat``) so the probe lands where a real fallback
    write would. The synthetic ``(default agent surface)`` label collapses to
    ``default``.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in label).strip("-")
    return safe or "default"


def _resolve_agent_memory_roots(config: dict[str, Any] | None, document_root: Path) -> list[tuple[str, Path]]:
    """Return ``(agent_label, preferred_overlay_root)`` for every configured agent.

    Reuses :func:`kairix.core.agents.scope.load_agent_scopes` so the PREFERRED
    overlay matches the runtime write path's intent. The resolution to a writable
    destination (overlay-or-data-dir-fallback) happens in
    :func:`_probe_agent_memory_roots`. When no agents are explicitly configured,
    returns the shared ``04-Agent-Knowledge`` writable submount (the surface every
    built-in agent writes under) so a fresh install still gets a truthful signal.
    """
    from kairix.core.agents.scope import load_agent_scopes

    try:
        scopes = load_agent_scopes(config)
    except (ValueError, TypeError):
        scopes = {}
    roots: list[tuple[str, Path]] = []
    for name, scope in scopes.items():
        try:
            candidate = Path(scope.writable_path())
        except ValueError:
            continue
        if not candidate.is_absolute():
            candidate = document_root / candidate
        roots.append((name, candidate))
    if not roots:
        roots.append(("(default agent surface)", document_root / "04-Agent-Knowledge"))
    return roots


def _probe_agent_memory_roots(
    roots: list[tuple[str, Path]],
    fallback_root: Path,
    probe_fn: Callable[[Path], Any],
) -> tuple[list[str], list[tuple[str, Path, Any]]]:
    """Resolve + probe each agent's ACTUAL memory write destination.

    For every configured agent, resolve the write destination through the SAME
    :func:`kairix.paths.resolve_writable_memory_dir` the runtime write path
    (``remember`` / ``ingest_chat``) uses: prefer the 04-Agent-Knowledge overlay,
    fall back to ``fallback_root / <agent>`` under the writable data dir on a
    read-only / permission overlay. The verdict is whether that RESOLVED
    destination is writable, so a read-only overlay whose data-dir fallback IS
    writable reads ``<label>@<dir>=ok(fallback)`` (the correct, expected state on
    a hardened / read-only-root deploy) rather than failing the deploy gate. A
    hard FAIL is reserved for an agent that can persist NOWHERE — a non-fallback
    error (e.g. ENOSPC) or an unwritable data dir.

    Extracted from :func:`check_agent_memory_writable` to keep it under the F16
    cognitive-complexity ceiling. Returns per-agent verdict strings + the
    genuinely-unwritable failures (for ``onboard check --json``).
    """
    from kairix.paths import resolve_writable_memory_dir

    def _probe(target: str | Path) -> Any:
        return probe_fn(Path(target))

    verdicts: list[str] = []
    failures: list[tuple[str, Path, Any]] = []
    for label, preferred_root in roots:
        resolved = resolve_writable_memory_dir(
            preferred_root,
            fallback_root / _fallback_key(label),
            label=f"agent {label!r}",
            fallback_scan_root=fallback_root,
            probe_fn=_probe,
        )
        # resolve_writable_memory_dir only probed the PREFERRED dir; when it fell
        # back, re-probe the resolved fallback dir so the verdict reflects whether
        # the box can persist THERE — not merely that the overlay was read-only.
        probe = probe_fn(resolved.write_dir) if resolved.used_fallback else resolved.probe
        writable = bool(probe.writable)
        if writable:
            state = "ok(fallback)" if resolved.used_fallback else "ok"
        else:
            state = f"FAIL[{str(probe.errno_name) or 'error'}]"
        verdicts.append(f"{label}@{resolved.write_dir}={state}")
        if not writable:
            failures.append((label, resolved.write_dir, probe))
    return verdicts, failures


def _agent_memory_fail_result(
    failures: list[tuple[str, Path, Any]],
    total_roots: int,
    per_agent: str,
) -> CheckResult:
    """Build the hard-FAIL CheckResult reusing ``write_access_fix_hint`` (PLA-298 / #689).

    A failure here means the agent can persist to NEITHER the 04-Agent-Knowledge
    overlay NOR the writable data-dir fallback — so memory writes are lost. This
    is the only condition that blocks the deploy gate; a read-only overlay with a
    working fallback is a PASS (the resolved write path lands in the data dir).
    """
    from kairix.paths import write_access_fix_hint

    first_errno = str(getattr(failures[0][2], "errno_name", "") or "")
    return CheckResult(
        name=_CHECK_AGENT_MEMORY_WRITABLE,
        ok=False,
        detail=(
            f"{len(failures)}/{total_roots} agent(s) have NO writable memory destination "
            f"(neither the overlay nor the data-dir fallback is writable) — {per_agent}"
        ),
        fix=(
            f"{write_access_fix_hint(first_errno)}. "
            "next: re-run `kairix onboard check agent_memory_writable` once a writable "
            "destination exists — free disk space (ENOSPC), fix data-dir ownership, or "
            "mount 04-Agent-Knowledge read-write. "
            "run: df -h and check the kairix data dir is owned by the kairix user."
        ),
    )


def check_agent_memory_writable(deps: AgentMemoryWritableCheckDeps | None = None) -> CheckResult:
    """Each configured agent has a writable memory destination (PLA-298 / #689).

    Resolves every configured agent's memory write destination through the SAME
    :func:`kairix.paths.resolve_writable_memory_dir` the runtime write path
    (``remember`` / ``ingest_chat``) uses: it PREFERS the ADR-017
    ``04-Agent-Knowledge`` overlay but, on a read-only / permission overlay,
    FALLS BACK to a writable path under the kairix data dir. The verdict is
    whether that RESOLVED destination is writable — not whether the preferred
    overlay is.

    On a hardened, read-only-root enterprise box the overlay is legitimately
    ``:ro`` and the data-dir fallback IS the correct write location (F94 —
    persist through ``kairix.paths``, never a system path); agents reach memory
    through commands/connectors, so where it is stored does not matter as long as
    it is always writable and searchable. So a read-only overlay whose data-dir
    fallback IS writable PASSES (surfaced as ``<agent>@<dir>=ok(fallback)``) — it
    does NOT block the deploy gate. A hard FAIL is reserved for an agent that can
    persist NOWHERE: a non-fallback error (e.g. ENOSPC disk-full) or an
    unwritable data dir. This is the leg that both closes the old false-green
    (never probed writability) and stops the false-red that rolled back the
    v2026.7.2 alpha on the read-only-root production box.

    ``deps`` is the public DI seam (config loader / document root / fallback root
    / probe).
    """
    d = deps if deps is not None else AgentMemoryWritableCheckDeps()
    try:
        config = d.config_loader()
        document_root = Path(d.document_root_fn())
        fallback_root = Path(d.memory_fallback_root_fn())
    except Exception as exc:
        return CheckResult(
            name=_CHECK_AGENT_MEMORY_WRITABLE,
            ok=False,
            detail=f"could not resolve agent memory roots: {exc}",
            fix=(
                "fix: ensure KAIRIX_DOCUMENT_ROOT resolves and kairix.config.yaml is readable. "
                "next: run `kairix onboard check document_root_configured`. "
                "run: kairix onboard check."
            ),
        )

    roots = _resolve_agent_memory_roots(config, document_root)
    verdicts, failures = _probe_agent_memory_roots(roots, fallback_root, d.probe_fn)
    per_agent = "; ".join(verdicts)
    if failures:
        return _agent_memory_fail_result(failures, len(roots), per_agent)
    return CheckResult(
        name=_CHECK_AGENT_MEMORY_WRITABLE,
        ok=True,
        detail=f"{len(roots)} agent memory destination(s) writable — {per_agent}",
    )


ALL_CHECKS: list[Callable[..., CheckResult]] = [
    check_kairix_on_path,
    check_wrapper_installed,
    check_secrets_loaded,
    check_document_root_configured,
    check_vector_search_working,
    check_neo4j_reachable,
    check_agent_knowledge_populated,
    check_chunk_date_populated,
    check_mcp_service,
    check_query_cache_stats,
    check_embed_cache_stats,
    check_topology_config_valid,
    check_topology_cc_pairs_registered,
    check_topology_default_in_scope_field_present,
    check_topology_wildcard_expansion_resolved,
    check_sharepoint_credentials_loaded,
    check_maintenance_loop_ticking,
    check_extractor_libraries_importable,
    check_agent_memory_writable,
]


def run_all_checks(*, checks: list[Callable[..., CheckResult]] | None = None) -> list[CheckResult]:
    """Run all deployment checks in order. Returns results for all checks.

    Checks are ordered by dependency: PATH → secrets → vault → search → graph.
    A failure in an early check usually explains failures in later checks.

    ``checks`` is the public DI seam — tests pass a fake check list to
    drive the runner's collation logic without monkey-patching the
    module-level ``ALL_CHECKS`` registry. Production callers leave it
    ``None`` and the runner uses the canonical registry.
    """
    effective = checks if checks is not None else ALL_CHECKS
    results: list[CheckResult] = []
    for check_fn in effective:
        try:
            results.append(check_fn())
        except Exception as exc:
            results.append(
                CheckResult(
                    name=check_fn.__name__.removeprefix("check_"),
                    ok=False,
                    detail=f"Check raised unexpected exception: {exc}",
                    fix="This is a bug in kairix.platform.onboard.check — please report it.",
                )
            )
    return results


def run_onboard_check(*, checks: list[Callable[..., CheckResult]] | None = None) -> OnboardResult:
    """Run all deployment checks and return a structured OnboardResult.

    Canonical surface for:
      - ``kairix onboard check --json`` CLI output
      - docker-compose healthcheck (exit code is derived from .fully_passed)
      - any caller that needs to act on individual failures programmatically

    Each failed check produces a CheckFailure with a populated, non-empty
    ``remediation`` string sourced from CANONICAL_REMEDIATIONS. The set of
    checks (and their order) is identical to run_all_checks() — this
    function only restructures the output. ``checks`` forwards through to
    ``run_all_checks`` as the public DI seam.
    """
    results = run_all_checks(checks=checks)
    failures = [
        CheckFailure(
            check=r.name,
            detail=r.detail,
            remediation=_remediation_for(r.name, r.fix),
        )
        for r in results
        if not r.ok
    ]
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    return OnboardResult(
        passed=passed,
        total=total,
        failures=failures,
        fully_passed=(passed == total),
    )
