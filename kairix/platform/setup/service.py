"""SetupService — the contract between the web setup wizard and its backend.

The flag-gated web wizard (``kairix.platform.setup.web``) renders
screens against this Protocol only; it never talks to providers,
connectors, or the index directly. The production implementation
lives in :mod:`kairix.platform.setup.backends` and is constructed
through :func:`build_setup_service`.

Frozen dataclasses per F42 — every Protocol method returns a value
object, never ``dict[str, Any]``.

Tests drive the wizard with ``FakeSetupService`` from ``tests/fakes.py``
via the ``setup_service_factory`` seam on
:func:`kairix.agents.mcp.transport.build_mcp_app`. Tests of the real
backend construct through :func:`build_setup_service` with fakes
injected at the seams below the service (``SetupServiceDeps``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kairix.paths import KairixPaths
    from kairix.platform.setup.backends import SetupServiceDeps

# ---------------------------------------------------------------------------
# Contract constants (F17 — one definition site). The backend reports
# these values; the wizard's routes and templates react to them. They
# live here, not in the backend module, so the wizard UI can import
# them without importing the backend's side-effect machinery.
# ---------------------------------------------------------------------------

#: Source sign-in phases — the ``SourceAuthStatus.phase`` vocabulary (#489).
PHASE_IDLE = "idle"
PHASE_STARTING = "starting"
PHASE_CONSENT = "consent"
PHASE_EXCHANGING = "exchanging"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

#: Azure-shaped provider plugin names (#484). The key screen shows the
#: optional deployment-name field for these; the backend requires an
#: endpoint and probes by deployment name for the same set. The two members
#: are also exported singly for the probe-model map, the endpoint-shape
#: remap and the wizard's default pick (F85 — one definition site).
PLUGIN_AZURE_FOUNDRY = "azure_foundry"
PLUGIN_AZURE_LEGACY = "azure_legacy"
AZURE_PROVIDER_NAMES = frozenset({PLUGIN_AZURE_FOUNDRY, PLUGIN_AZURE_LEGACY})


class SecretsWriteError(OSError):
    """A credential could not be written to the operator secrets bundle.

    Raised by :meth:`SetupService.save_provider` when persisting the API
    key fails — e.g. the container's ``/run/secrets`` mount is read-only
    (vault-agent sidecar layout). Typed separately from a config-write
    ``OSError`` so the wizard renders the ``KAIRIX_SECRETS_FILE`` rescue
    instead of the config-overlay one, which cannot fix a secrets mount.

    ``bundle_path`` names the file the write targeted — a path, never a
    secret value (F15). Empty when the failing write carried no filename.
    """

    def __init__(self, bundle_path: str) -> None:
        target = bundle_path or "its configured location"
        super().__init__(f"could not write the secrets bundle at {target}")
        self.bundle_path = bundle_path


@dataclass(frozen=True)
class SetupStatus:
    """Which wizard steps have been completed so far."""

    provider_done: bool
    source_done: bool
    index_done: bool


@dataclass(frozen=True)
class ProviderValidation:
    """Outcome of validating an operator-supplied provider credential.

    ``deployment_missing`` distinguishes Azure's "the key authenticated
    but no deployment carries the probed name" failure (#484) from a
    genuinely bad key — the wizard renders deployment guidance instead
    of key-blame for that case.
    """

    ok: bool
    models: tuple[str, ...]
    error: str | None
    deployment_missing: bool = False


@dataclass(frozen=True)
class FolderScan:
    """Outcome of scanning a candidate source folder before indexing.

    ``words_estimate`` is sample-based: the scan reads at most 200 files
    and extrapolates the per-file average across the full count, and file
    counting stops at 50,000 so a mistyped path can't hang the wizard.
    ``cost_estimate_usd`` prices the extrapolated tokens at the embed
    model's list price — an estimate for the operator, not a bill.
    """

    ok: bool
    files: int
    words_estimate: int
    cost_estimate_usd: float
    error: str | None


@dataclass(frozen=True)
class SourceHint:
    """Container-aware pre-fill for the wizard's folder step (#486).

    ``in_container`` is True when kairix runs inside a container, where
    only mounted folders are visible; ``suggested_path`` carries the
    configured document root (stock compose: ``/data/documents``) so the
    operator starts from the folder they actually mounted. On bare-metal
    installs both read falsy and the folder field starts blank.
    """

    in_container: bool
    suggested_path: str


@dataclass(frozen=True)
class IndexStatus:
    """Progress snapshot for the first-index run."""

    running: bool
    done: bool
    chunks_done: int
    chunks_total: int
    error: str | None


@dataclass(frozen=True)
class SearchPreviewHit:
    """One result row in the first-search preview."""

    title: str
    snippet: str
    source: str
    score: float


@dataclass(frozen=True)
class SearchPreview:
    """Results of the wizard's first-search step."""

    results: tuple[SearchPreviewHit, ...]


@dataclass(frozen=True)
class ConnectSnippet:
    """A copy-paste config block for one agent client."""

    client: str
    config_text: str


@dataclass(frozen=True)
class AgentConnectInfo:
    """Everything an operator needs to point an agent at this kairix."""

    mcp_url: str
    snippets: tuple[ConnectSnippet, ...]


@dataclass(frozen=True)
class HandshakeResult:
    """Outcome of verifying an MCP handshake against the running server."""

    ok: bool
    tools_count: int
    error: str | None


# ---------------------------------------------------------------------------
# Capability tour (#490) — one frozen DTO per sample run. ``message`` is
# the shared failure convention: empty on success; on any failure it
# carries grade-8 guidance (never a stack trace) and the payload fields
# read empty/falsy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TourPrep:
    """Outcome of the tour's context-pack sample run (the ``prep`` tool).

    ``summary`` is the LLM-grounded topic summary (or the use case's own
    honest "no relevant documents" sentence on a thin corpus); ``sources``
    names the documents the summary was built from.
    """

    summary: str
    sources: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class TourRememberRoundtrip:
    """Outcome of the tour's write-then-find sample run (``memory_write``).

    The round trip is the proof: ``saved`` says the memory landed on disk,
    ``found`` says the search leg surfaced that same file, ``elapsed_ms``
    is the wall-clock for the whole loop, and ``hits`` are the search
    results shown so the operator sees the memory come back.
    """

    saved: bool
    agent: str
    path: str
    found: bool
    elapsed_ms: int
    hits: tuple[SearchPreviewHit, ...]
    message: str


@dataclass(frozen=True)
class TourBrief:
    """Outcome of the tour's session-briefing sample run (the ``brief`` tool).

    ``preview`` is empty on a fresh knowledge store — the screen renders
    an honest empty state, never fabricated content. ``next_action``
    passes through the briefing's own suggestion when it has one.
    """

    agent: str
    preview: str
    next_action: str
    message: str


@dataclass(frozen=True)
class TourTimelineHit:
    """One date-aware result row in the tour's timeline sample run."""

    title: str
    snippet: str
    source: str
    date: str


@dataclass(frozen=True)
class TourTimeline:
    """Outcome of the tour's timeline sample run (the ``timeline`` tool)."""

    hits: tuple[TourTimelineHit, ...]
    message: str


@dataclass(frozen=True)
class SourceOption:
    """One card on the wizard's source step (#489).

    ``oauth=False`` rows route to existing screens (the folder step);
    ``oauth=True`` rows start the wizard-origin OAuth connect flow.
    """

    key: str
    label: str
    description: str
    oauth: bool


@dataclass(frozen=True)
class SourceAuthStart:
    """Outcome of asking the backend to start a source sign-in flow."""

    ok: bool
    error: str | None


@dataclass(frozen=True)
class SourceAuthStatus:
    """Snapshot of the in-flight source sign-in (#489).

    ``phase`` walks ``idle → starting → consent → exchanging →
    done | failed``. In the ``consent`` phase ``authorize_url`` carries
    the provider consent-screen URL the operator's browser must visit;
    the wizard's status poll redirects there. ``failed`` carries an
    operator-facing F21 message in ``error``.
    """

    provider: str
    phase: str
    authorize_url: str | None
    error: str | None


@dataclass(frozen=True)
class CallbackOutcome:
    """Outcome of delivering a provider redirect to the pending flow.

    ``ok=False`` covers the two rejection cases: no flow is pending, or
    the redirect's ``state`` does not match the pending flow's nonce.
    """

    ok: bool
    error: str | None


@dataclass(frozen=True)
class SourceUnit:
    """One pickable unit (a Slack channel, a GitHub repo) in the picker."""

    unit_id: str
    name: str
    detail: str = ""


@dataclass(frozen=True)
class SourceUnits:
    """The picker payload for one connected source.

    ``pickable=False`` sources (Gmail / Google Calendar / Google Drive
    — no sub-unit listing surface) render a confirm screen instead of
    a checkbox list; ``note`` carries the confirm copy.
    """

    provider: str
    units: tuple[SourceUnit, ...]
    pickable: bool
    note: str = ""
    error: str | None = None


@dataclass(frozen=True)
class SavedSource:
    """Outcome of persisting the picked units as connector config.

    ``summary`` states what will be fetched ("2 channels selected — …")
    BEFORE any spend happens; the wizard shows it on the saved screen.
    ``config_file`` names the file the save landed in (#492) so the
    operator can see — and verify — where their configuration lives;
    empty on a failed save.
    """

    ok: bool
    summary: str
    error: str | None
    config_file: str = ""


class SetupService(Protocol):
    """Boundary Protocol the web wizard composes against.

    The wizard owns rendering; this service owns every side effect
    (credential validation, secret persistence, folder scanning,
    index runs, search, handshake verification).
    """

    def status(self) -> SetupStatus:
        """Return which wizard steps are complete."""

    def validate_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        deployment: str | None = None,
    ) -> ProviderValidation:
        """Check a credential against the provider; list available models.

        ``deployment`` (#484) is the operator's Azure deployment name —
        when provided it is probed instead of the per-plugin default
        model, because Azure routes requests by deployment name.
        """

    def save_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        model: str | None,
        deployment: str | None = None,
    ) -> None:
        """Persist the validated provider selection + credential.

        ``deployment`` (#484) backstops ``model``: when no model was
        chosen, the Azure deployment name is persisted as the embed
        model so indexing talks to the deployment that validated.

        Raises:
            SecretsWriteError: when the credential cannot be written to
                the secrets bundle (read-only mount) — the wizard
                renders the ``KAIRIX_SECRETS_FILE`` rescue banner.
            OSError: when the config file cannot be written (read-only
                mount without an overlay, #485) — the wizard renders
                the config-overlay rescue banner.
        """

    def scan_folder(self, path: str) -> FolderScan:
        """Scan a candidate folder and estimate indexing size + cost."""

    def source_hint(self) -> SourceHint:
        """Container-aware pre-fill for the folder step (#486)."""

    def save_source(self, path: str) -> None:
        """Persist the chosen folder as the first source."""

    def config_file_path(self) -> str:
        """The config file wizard saves land in — shown on save/done screens (#492)."""

    def start_index(self) -> None:
        """Kick off the first index run in the background."""

    def index_status(self) -> IndexStatus:
        """Return the current first-index progress snapshot."""

    def first_search(self, query: str) -> SearchPreview:
        """Run a search against the freshly built index."""

    def agent_connect_info(self) -> AgentConnectInfo:
        """Return the MCP URL + per-client connect snippets."""

    def verify_agent_handshake(self) -> HandshakeResult:
        """Probe the running MCP server and count the tools it offers."""

    def tour_prep(self, query: str) -> TourPrep:
        """Build a compact context pack on ``query`` (#490 — the ``prep`` tool)."""

    def tour_remember_roundtrip(self, content: str) -> TourRememberRoundtrip:
        """Save ``content`` as a memory, then find it again with search (#490)."""

    def tour_brief(self) -> TourBrief:
        """Generate a session briefing from recent activity (#490)."""

    def tour_timeline(self, query: str) -> TourTimeline:
        """Run date-aware retrieval for ``query`` (#490 — the ``timeline`` tool)."""

    def source_options(self) -> tuple[SourceOption, ...]:
        """Return the source cards the wizard's source step offers (#489)."""

    def start_source_auth(self, provider: str, fields: Mapping[str, str], origin: str) -> SourceAuthStart:
        """Start the OAuth connect flow for ``provider`` in the background.

        ``fields`` carries the operator-typed connect-form values
        (never logged — F15). ``origin`` is the scheme+host+port the
        operator's browser used to reach the wizard, captured from the
        live request; the flow's redirect URI derives from it.
        """

    def source_auth_status(self) -> SourceAuthStatus:
        """Return the current source sign-in snapshot (#489)."""

    def complete_source_callback(self, state: str | None, params: Mapping[str, str]) -> CallbackOutcome:
        """Deliver a provider redirect's params to the pending flow.

        Rejects (``ok=False``) when no flow is pending or ``state``
        does not match the pending flow's single-use nonce.
        """

    def discover_source_units(self, provider: str) -> SourceUnits:
        """List the pickable units the connected source offers (#489)."""

    def save_oauth_source(self, provider: str, instance: str, picks: tuple[str, ...]) -> SavedSource:
        """Persist the picked units as connector + collection config.

        Raises:
            OSError: when the config file cannot be written (read-only
                mount without an overlay, #485) — the wizard renders
                the rescue banner.
        """


def build_setup_service(
    *,
    paths: KairixPaths | None = None,
    deps: SetupServiceDeps | None = None,
) -> SetupService:
    """Production factory for the wizard backend.

    Args:
        paths: Optional :class:`kairix.paths.KairixPaths` pinning the
            index database (and therefore the embed lockfile and the
            search pipeline) to an explicit location. ``None`` —
            the production default — resolves through the platform
            chain (env vars, config file, platform defaults).
        deps: Optional :class:`kairix.platform.setup.backends.SetupServiceDeps`
            carrying the injectable collaborators. ``None`` wires the
            real implementations; tests pass fakes for the seams a
            scenario drives.
    """
    from kairix.platform.setup.backends import KairixSetupService

    return KairixSetupService(paths=paths, deps=deps)


__all__ = [
    "AZURE_PROVIDER_NAMES",
    "PHASE_CONSENT",
    "PHASE_DONE",
    "PHASE_EXCHANGING",
    "PHASE_FAILED",
    "PHASE_IDLE",
    "PHASE_STARTING",
    "PLUGIN_AZURE_FOUNDRY",
    "PLUGIN_AZURE_LEGACY",
    "AgentConnectInfo",
    "CallbackOutcome",
    "ConnectSnippet",
    "FolderScan",
    "HandshakeResult",
    "IndexStatus",
    "ProviderValidation",
    "SavedSource",
    "SearchPreview",
    "SearchPreviewHit",
    "SecretsWriteError",
    "SetupService",
    "SetupStatus",
    "SourceAuthStart",
    "SourceAuthStatus",
    "SourceHint",
    "SourceOption",
    "SourceUnit",
    "SourceUnits",
    "TourBrief",
    "TourPrep",
    "TourRememberRoundtrip",
    "TourTimeline",
    "TourTimelineHit",
    "build_setup_service",
]
