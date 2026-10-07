"""F43 + F68 contract for the SetupService Protocol's ``config_file_path``.

This is the live reference for F43's behavioural-parity strengthening
(EPIC #499 phase 1): the positive contract is proved by ONE parametrized
body run over BOTH the real ``KairixSetupService`` (through its
``SetupServiceDeps`` seam) and the canonical ``FakeSetupService`` from
``tests/fakes.py``. Co-asserting the same observable through both impls
is what would have caught session-escape 7 — where the fake inverted
production's done-semantics while every separate-body suite stayed green.

The ``raises`` failure shape (F68) is genuinely single-impl: only the
real backend resolves a config target through an injectable resolver
that can fail; the fake returns a constructor knob and has no analogue.
It carries the ``# F43-single-impl:`` rationale the rule requires.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.platform.setup.backends import EMPTY_INDEX_MESSAGE, KairixSetupService, SetupServiceDeps
from kairix.platform.setup.service import (
    PHASE_FAILED,
    PHASE_IDLE,
    SecretsWriteError,
    SetupService,
    SetupStatus,
    SourceAuthStatus,
    SourceHint,
    build_setup_service,
)
from tests.fakes import (
    FakeCallbackListener,
    FakeOAuth2Flow,
    FakePaths,
    FakeProvider,
    FakeSetupService,
)

pytestmark = pytest.mark.contract

# The shared resolvable target both impls are configured against. The
# parity body asserts both surface THIS exact path — the fake's knob and
# the real backend's resolver must agree on the observable, never just
# "some non-empty string each". ``config_file_path()`` returns a str on
# both impls; the real backend's resolver yields a Path (its declared
# type) that the backend stringifies, so the observable is this string.
_RESOLVED_TARGET = "/var/lib/kairix/kairix.config.local.yaml"


def _real_service() -> SetupService:
    """The production backend, its config-target resolver pinned to the
    shared path through the canonical ``SetupServiceDeps`` seam."""
    return KairixSetupService(deps=SetupServiceDeps(config_target_fn=lambda: Path(_RESOLVED_TARGET)))


def _fake_service() -> SetupService:
    """The canonical fake, its config-file knob set to the shared path."""
    return FakeSetupService(config_file=_RESOLVED_TARGET)


# Real + fake behind one parameter — the F43 parity shape. ``name``
# labels the case; ``factory`` builds the impl under test.
_IMPLEMENTATIONS: list[tuple[str, Callable[[], SetupService]]] = [
    ("real", _real_service),
    ("fake", _fake_service),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_config_file_path_surfaces_the_resolved_target(name: str, factory: Callable[[], SetupService]) -> None:
    """Both impls return the resolved config path verbatim — non-empty
    and equal to the target they were configured against.

    The save/done screens render this line so the operator knows which
    file to carry to a new machine; a fake that returned a *different*
    truthy string (or empty) would render misleading copy while every
    separate-body test still passed. Running the SAME assertion through
    real and fake pins them to one observable.

    Sabotage proof (executed): change the fake's ``config_file_path`` to
    ``return self._config_file + "-DRIFT"`` → this body fails for the
    ``fake`` param (``... != /var/lib/...``) while ``real`` still passes,
    catching exactly the real/fake divergence F43 targets. Restored.
    """
    service = factory()
    result = service.config_file_path()
    assert result == _RESOLVED_TARGET, f"{name} impl must surface the resolved target verbatim"
    assert result != "", f"{name} impl must never blank the config path when one resolves"


# F43-single-impl: the fake's config_file_path() returns a constructor
# knob and cannot raise — only the real backend resolves through an
# injectable target resolver that can fail. No fake-side analogue exists,
# so this ``raises`` failure-mode probe is genuinely real-only.
def test_config_file_path_raises_when_target_resolution_fails() -> None:
    """``raises`` shape (F68): a failing target resolver propagates,
    never masks. The service must not swallow a resolver error into a
    fabricated path — the route tier owns rendering decisions (review
    M2's lesson: wrong rescue copy is worse than a visible error).
    """
    service = KairixSetupService(deps=SetupServiceDeps(config_target_fn=_raise_oserror))
    with pytest.raises(OSError, match="config target unavailable"):
        service.config_file_path()


def _raise_oserror() -> Path:
    raise OSError("config target unavailable")


# ---------------------------------------------------------------------------
# F68 failure-mode coverage for every other SetupService method (PLA-472).
#
# Each body below runs ONCE over two implementations (the F43 parity
# shape): the real ``KairixSetupService`` built through
# ``build_setup_service`` with the failing collaborator injected at its
# ``SetupServiceDeps`` seam, and the canonical ``FakeSetupService``
# configured through its failure knob to model the same failure. The
# assertions are the observable the wizard route renders — so a fake that
# drifts from the real failure shape fails the same body the real impl
# passes.
# ---------------------------------------------------------------------------

_API_KEY = "sk-contract-fixture-key"  # pragma: allowlist secret — fixture value, never a real credential
_DEFAULT_MCP_URL = "http://localhost:8080/mcp"
_INDEX_THREAD = "setup-wizard-index"
_SOURCE_AUTH_THREAD = "setup-wizard-source-auth"
_ORIGIN = "http://localhost:8080"
_SLACK_FIELDS = {"workspace": "alpha", "client_id": "id-1", "client_secret": "sec-1"}  # pragma: allowlist secret
_NOT_CONNECTED = "This source is not connected yet. fix: finish the sign-in first."
_MISSING_FOLDER = "no-such-folder"

ServiceFactory = Callable[[Path], SetupService]


def _real(tmp_path: Path, **overrides: Any) -> SetupService:
    """The production backend on a fresh, unconfigured host.

    Every seam the failure-mode bodies don't drive is pinned to the
    "nothing configured yet" answer so no test reads the live process
    env, the operator's config file, or a real index database.
    """
    seams: dict[str, Any] = {
        "environ": {},
        "credentials_probe": lambda: False,
        "configured_document_root_fn": lambda: None,
        "index_counts_fn": lambda _db: (0, 0),
        "embed_lock_probe_fn": lambda _lock: False,
        "top_level_config_fn": lambda: None,
        "clock_fn": lambda: 0.0,
        # Every persistence seam is armed: a failure-mode path that
        # reaches a write (to the operator's real config or secrets)
        # fails the test loudly instead of touching the host.
        "write_config_fn": _raise(AssertionError("failure path must not write config")),
        "persist_credentials_fn": _raise(AssertionError("failure path must not persist credentials")),
        "persist_secret_fn": _raise(AssertionError("failure path must not persist secrets")),
        "read_config_fn": lambda: {},
    }
    seams.update(overrides)
    paths = FakePaths(
        document_root=tmp_path / "documents",
        db_path=tmp_path / "index.sqlite",
        log_dir=tmp_path / "logs",
        workspace_root=tmp_path / "workspaces",
    )
    return build_setup_service(paths=paths, deps=SetupServiceDeps(**seams))


def _join_worker(thread_name: str) -> None:
    """Wait for the real backend's named background worker to finish.

    Deterministic replacement for a sleep-poll: the worker thread is
    looked up by its production name and joined. The fake spawns no
    thread, so this is a no-op on the fake side of every parity body.
    """
    for thread in threading.enumerate():
        if thread.name == thread_name:
            thread.join(timeout=10)


def _raise(exc: BaseException) -> Callable[..., Any]:
    """A seam stand-in that raises ``exc`` whatever it is called with."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _boom


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService()),
    ],
)
def test_status_returns_empty_when_nothing_is_configured(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``returns_empty``: a fresh host reports every step incomplete.

    Sabotage proof (executed): make ``KairixSetupService.status`` return
    ``index_done=embedded >= 0`` → the ``real`` case fails (the wizard
    would skip straight past indexing on an empty host). Restored.
    """
    status = factory(tmp_path).status()
    assert status == SetupStatus(provider_done=False, source_done=False, index_done=False), name


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                provider_factory=lambda _plugin, _creds: FakeProvider(
                    embed_raises=PermissionError(f"401 Unauthorized: key {_API_KEY} was rejected by the provider")
                ),
            ),
        ),
        (
            "fake",
            lambda _tmp: FakeSetupService(
                validate_ok=False,
                validate_error="401 Unauthorized: key [redacted] was rejected by the provider",
            ),
        ),
    ],
)
def test_validate_provider_unauthorized_reports_rejection_without_echoing_key(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unauthorized``: a rejected key renders the provider's 401 —
    never a success, never a model list, and never the typed-in key (F15).

    Sabotage proof (executed): drop the ``_scrub_secret`` call in
    ``validate_provider`` → the ``real`` case fails on the key-echo
    assertion. Restored.
    """
    result = factory(tmp_path).validate_provider("openai", _API_KEY, None)
    assert result.ok is False, name
    assert result.models == ()
    assert result.deployment_missing is False
    assert result.error is not None
    assert "401 Unauthorized" in result.error
    assert _API_KEY not in result.error


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                persist_credentials_fn=_raise(OSError(30, "Read-only file system", "/run/secrets/kairix.env")),
            ),
        ),
        ("fake", lambda _tmp: FakeSetupService(save_provider_raises=SecretsWriteError("/run/secrets/kairix.env"))),
    ],
)
def test_save_provider_raises_secrets_write_error_naming_the_bundle(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``raises``: a read-only secrets mount surfaces as the typed
    ``SecretsWriteError`` naming the bundle path, and the provider step
    stays incomplete.

    Sabotage proof (executed): change ``save_provider``'s ``except
    OSError`` to re-raise the bare ``OSError`` → the ``real`` case fails
    (``pytest.raises(SecretsWriteError)`` sees a plain OSError). Restored.
    """
    service = factory(tmp_path)
    with pytest.raises(SecretsWriteError, match=re.escape("/run/secrets/kairix.env")):
        service.save_provider("openai", _API_KEY, None, "text-embedding-3-small")
    assert service.status().provider_done is False, name


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService(scan_ok=False)),
    ],
)
def test_scan_folder_returns_empty_for_a_missing_folder(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``returns_empty``: scanning a folder that doesn't exist reports
    zero files / words / cost and names the path it looked at.

    Sabotage proof (executed): disable the ``if not folder.is_dir()``
    branch in ``scan_folder`` → the ``real`` case fails (the walk of a
    missing folder reports ``ok=True``). Restored.
    """
    missing = tmp_path / _MISSING_FOLDER
    scan = factory(tmp_path).scan_folder(str(missing))
    assert scan.ok is False, name
    assert (scan.files, scan.words_estimate, scan.cost_estimate_usd) == (0, 0, 0.0)
    assert scan.error is not None
    assert str(missing) in scan.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService()),
    ],
)
def test_source_hint_returns_empty_outside_a_container(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``returns_empty``: off-container there is no mounted folder to
    pre-fill, so the hint is blank rather than a guessed path.

    Sabotage proof (executed): make ``source_hint`` return
    ``in_container=True`` unconditionally → the ``real`` case fails.
    Restored.
    """
    assert factory(tmp_path).source_hint() == SourceHint(in_container=False, suggested_path=""), name


def _missing_folder_error(tmp: Path) -> ValueError:
    return ValueError(f"Folder not found or not readable: {tmp / _MISSING_FOLDER}.")


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda tmp: FakeSetupService(save_source_raises=_missing_folder_error(tmp))),
    ],
)
def test_save_source_raises_value_error_for_a_missing_folder(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``raises``: saving a folder that doesn't exist is a hard reject
    naming the path — never a silent config write — and the source step
    stays incomplete.

    Sabotage proof (executed): disable the ``if not folder.is_dir():
    raise ValueError`` guard in ``save_source`` → the ``real`` case
    fails (the armed config writer is reached instead). Restored.
    """
    service = factory(tmp_path)
    missing = tmp_path / _MISSING_FOLDER
    with pytest.raises(ValueError, match=re.escape(str(missing))):
        service.save_source(str(missing))
    assert service.status().source_done is False, name


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                index_runner_fn=_raise(RuntimeError("embed provider rejected the batch")),
                index_counts_fn=lambda _db: (0, 9),
            ),
        ),
        (
            "fake",
            lambda _tmp: FakeSetupService(
                chunks_total=9,
                index_error="Indexing stopped: embed provider rejected the batch fix: check the provider.",
            ),
        ),
    ],
)
def test_start_index_raises_in_worker_surfaces_error_in_status(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``raises``: an index run that blows up in the background stops
    cleanly and the next status poll carries the failure — not
    ``running`` forever, and never ``done``.

    Sabotage proof (executed): in ``_index_worker`` replace the
    ``except Exception`` body's error recording with ``pass`` → the
    ``real`` case fails
    (``error is None``; the screen would spin with no explanation).
    Restored.
    """
    service = factory(tmp_path)
    service.start_index()
    _join_worker(_INDEX_THREAD)
    status = service.index_status()
    assert status.running is False, name
    assert status.done is False
    assert status.error is not None
    assert "embed provider rejected the batch" in status.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, index_runner_fn=lambda: None)),
        ("fake", lambda _tmp: FakeSetupService(chunks_total=0)),
    ],
)
def test_index_status_returns_empty_corpus_as_done_with_explanation(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``returns_empty``: a run over an empty corpus completes honestly —
    ``done`` with zero chunks and the 0-documents explanation — instead
    of spinning forever (session-escape 7: the fake and real once
    disagreed on exactly this).

    Sabotage proof (executed): change ``done = finished_clean and
    (embedded > 0 or ran_to_completion)`` to ``... and embedded > 0`` →
    the ``real`` case fails (``done`` stays False). Restored.
    """
    service = factory(tmp_path)
    service.start_index()
    _join_worker(_INDEX_THREAD)
    status = service.index_status()
    assert status.done is True, name
    assert status.running is False
    assert (status.chunks_done, status.chunks_total) == (0, 0)
    assert status.error == EMPTY_INDEX_MESSAGE


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, search_pipeline_factory=_raise(ConnectionError("vector index unavailable")))),
        ("fake", lambda _tmp: FakeSetupService(search_hits=())),
    ],
)
def test_first_search_returns_empty_when_the_pipeline_fails(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``returns_empty``: a failing search pipeline yields an empty
    preview the screen's empty state guides from — never a stack trace.

    Sabotage proof (executed): make the ``except`` branch in
    ``first_search`` re-raise → the ``real`` case fails with the
    ConnectionError escaping. Restored.
    """
    preview = factory(tmp_path).first_search("rollout plan")
    assert preview.results == (), name


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService(mcp_url=_DEFAULT_MCP_URL)),
    ],
)
def test_agent_connect_info_unavailable_endpoint_override_falls_back_to_local_default(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unavailable``: with no ``KAIRIX_MCP_ENDPOINT`` configured the
    connect screen still shows a usable URL — the local default — and a
    copy-paste snippet carrying exactly that URL.

    Sabotage proof (executed): make ``agent_connect_info`` build the
    URL as ``mcp_endpoint("", environ=...)`` (no default) → the
    ``real`` case fails (blank URL). Restored.
    """
    info = factory(tmp_path).agent_connect_info()
    assert info.mcp_url == _DEFAULT_MCP_URL, name
    assert any(snippet.config_text == _DEFAULT_MCP_URL for snippet in info.snippets)


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                capability_probe_fn=lambda: {"secrets_loaded": True},
                tools_count_fn=_raise(ConnectionRefusedError("connection refused")),
            ),
        ),
        (
            "fake",
            lambda _tmp: FakeSetupService(
                handshake_ok=False,
                handshake_error="MCP handshake check failed: connection refused.",
            ),
        ),
    ],
)
def test_verify_agent_handshake_unavailable_reports_zero_tools_and_cause(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unavailable``: an unreachable MCP surface fails the handshake
    with zero tools and the underlying cause — not a false "connected".

    Sabotage proof (executed): in ``verify_agent_handshake`` return
    ``HandshakeResult(ok=True, tools_count=0, error=None)`` from the
    ``except`` branch → the ``real`` case fails. Restored.
    """
    result = factory(tmp_path).verify_agent_handshake()
    assert result.ok is False, name
    assert result.tools_count == 0
    assert result.error is not None
    assert "connection refused" in result.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, prep_fn=_raise(RuntimeError("LLM endpoint timed out")))),
        ("fake", lambda _tmp: FakeSetupService(tour_prep_message="The context pack could not be built.")),
    ],
)
def test_tour_prep_raises_returns_guidance_not_a_summary(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``raises``: a failing prep run renders guidance copy with no
    fabricated summary or sources.

    Sabotage proof (executed): make the ``except`` branch in
    ``tour_prep`` re-raise → the ``real`` case fails with the
    RuntimeError escaping. Restored.
    """
    prep = factory(tmp_path).tour_prep("rollout plan")
    assert (prep.summary, prep.sources) == ("", ()), name
    assert prep.message != ""


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, remember_fn=_raise(PermissionError("documents folder is read-only")))),
        ("fake", lambda _tmp: FakeSetupService(tour_remember_message="The memory could not be saved.")),
    ],
)
def test_tour_remember_roundtrip_raises_reports_not_saved_and_not_found(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``raises``: a failed memory write never claims ``saved`` or
    ``found`` — the round-trip proof is only shown when it happened.

    Sabotage proof (executed): change ``_failed_roundtrip`` to pass
    ``found=True`` → the ``real`` case fails. Restored.
    """
    roundtrip = factory(tmp_path).tour_remember_roundtrip("setup finished")
    assert roundtrip.saved is False, name
    assert roundtrip.found is False
    assert (roundtrip.path, roundtrip.hits, roundtrip.elapsed_ms) == ("", (), 0)
    assert roundtrip.message != ""


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, brief_fn=_raise(RuntimeError("LLM endpoint timed out")))),
        ("fake", lambda _tmp: FakeSetupService(tour_brief_message="The briefing could not be generated.")),
    ],
)
def test_tour_brief_raises_returns_guidance_and_no_preview(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``raises``: a failing brief run renders guidance with an empty
    preview, never a half-built briefing.

    Sabotage proof (executed): make the ``except`` branch in
    ``tour_brief`` re-raise → the ``real`` case fails with the
    RuntimeError escaping. Restored.
    """
    brief = factory(tmp_path).tour_brief()
    assert brief.preview == "", name
    assert brief.message != ""


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, timeline_fn=_raise(RuntimeError("index locked")))),
        ("fake", lambda _tmp: FakeSetupService(tour_timeline_message="The timeline lookup did not finish.")),
    ],
)
def test_tour_timeline_raises_returns_guidance_and_no_hits(name: str, factory: ServiceFactory, tmp_path: Path) -> None:
    """``raises``: a failing timeline run renders guidance and zero hits.

    Sabotage proof (executed): make the ``except`` branch in
    ``tour_timeline`` re-raise → the ``real`` case fails. Restored.
    """
    timeline = factory(tmp_path).tour_timeline("last week")
    assert timeline.hits == (), name
    assert timeline.message != ""


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, source_options_fn=lambda: ())),
        ("fake", lambda _tmp: FakeSetupService(source_options=())),
    ],
)
def test_source_options_returns_empty_when_no_source_is_offered(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``returns_empty``: an install offering no source cards yields an
    empty tuple — never the defaults silently re-injected.

    Sabotage proof (executed): make ``source_options`` return
    ``self._deps.source_options_fn() or DEFAULT_SOURCE_OPTIONS`` → the
    ``real`` case fails. Restored.
    """
    assert factory(tmp_path).source_options() == (), name


_FLOW_ERROR = "Slack needs a client secret. fix: paste the app's client secret. next: connect again."


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                listener_factory=lambda _origin, _state: FakeCallbackListener(),
                oauth_flow_factory=_raise(ValueError(_FLOW_ERROR)),
            ),
        ),
        ("fake", lambda _tmp: FakeSetupService(source_auth_start_error=_FLOW_ERROR)),
    ],
)
def test_start_source_auth_raises_in_flow_construction_returns_error_and_stays_idle(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``raises``: a flow that cannot be constructed (missing credential
    material) surfaces its F21 message verbatim and leaves NO pending
    sign-in behind — the status poll stays ``idle``.

    Sabotage proof (executed): in ``start_source_auth`` register a
    pending ``_SourceAuthState`` inside the flow-construction ``except``
    branch → the ``real`` case fails on the ``idle`` phase. Restored.
    """
    service = factory(tmp_path)
    started = service.start_source_auth("slack", _SLACK_FIELDS, _ORIGIN)
    assert started.ok is False, name
    assert started.error == _FLOW_ERROR
    assert service.source_auth_status().phase == PHASE_IDLE


def _consent_flow(request: Any) -> FakeOAuth2Flow:
    """A provider flow that opens the consent screen, then waits on the listener."""
    return FakeOAuth2Flow(browser=request.browser)


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "real",
            lambda tmp: _real(
                tmp,
                listener_factory=lambda _origin, _state: FakeCallbackListener(denied=True),
                oauth_flow_factory=_consent_flow,
                persist_secret_fn=_raise(AssertionError("a denied sign-in must persist nothing")),
            ),
        ),
        (
            "fake",
            lambda _tmp: FakeSetupService(
                source_auth_statuses=(
                    SourceAuthStatus(
                        provider="slack",
                        phase=PHASE_FAILED,
                        authorize_url="https://provider.test/consent",
                        error="fake listener: consent denied. fix: sign in again and approve access.",
                    ),
                )
            ),
        ),
    ],
)
def test_source_auth_status_unauthorized_consent_denied_reports_failed_phase(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unauthorized``: the operator denying consent drives the sign-in
    to ``failed`` with the denial reason — never ``done``.

    Sabotage proof (executed): in ``_source_auth_worker`` replace the
    ``except ConnectError`` body with ``pass`` (the denial is no longer
    recorded) → the ``real`` case fails (phase never reaches
    ``failed``). Restored.
    """
    service = factory(tmp_path)
    assert service.start_source_auth("slack", _SLACK_FIELDS, _ORIGIN).ok is True
    _join_worker(_SOURCE_AUTH_THREAD)
    status = service.source_auth_status()
    assert status.phase == PHASE_FAILED, name
    assert status.provider == "slack"
    assert status.error is not None
    assert "consent denied" in status.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService(callback_ok=False)),
    ],
)
def test_complete_source_callback_unauthorized_without_a_pending_flow(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unauthorized``: a provider redirect arriving with no sign-in
    pending is rejected — the unauthenticated callback route must never
    accept an unsolicited authorization code.

    Sabotage proof (executed): drop the ``pending is None`` clause from
    the rejection guard in ``complete_source_callback`` → the ``real``
    case fails (AttributeError on the missing pending flow). Restored.
    """
    outcome = factory(tmp_path).complete_source_callback("forged-state", {"code": "auth-1"})
    assert outcome.ok is False, name
    assert outcome.error is not None
    assert "No source connection is waiting" in outcome.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp)),
        ("fake", lambda _tmp: FakeSetupService(source_units=(), source_units_error=_NOT_CONNECTED)),
    ],
)
def test_discover_source_units_unauthorized_before_sign_in_lists_nothing(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unauthorized``: asking for a source's channels before it is
    signed in lists nothing and tells the operator to finish sign-in.

    Sabotage proof (executed): in ``discover_source_units`` move the
    not-connected guidance from ``error=`` to ``note=`` (``error=None``)
    → the ``real`` case fails. Restored.
    """
    units = factory(tmp_path).discover_source_units("slack")
    assert units.provider == "slack", name
    assert units.units == ()
    assert units.error is not None
    assert "not connected" in units.error


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", lambda tmp: _real(tmp, write_config_fn=_raise(AssertionError("nothing may be written")))),
        ("fake", lambda _tmp: FakeSetupService(save_oauth_error=_NOT_CONNECTED)),
    ],
)
def test_save_oauth_source_unauthorized_before_sign_in_writes_nothing(
    name: str, factory: ServiceFactory, tmp_path: Path
) -> None:
    """``unauthorized``: saving picks for a source that was never signed
    in fails without a summary and without naming a config file — the
    real backend's config writer is armed to fail the test if touched.

    Sabotage proof (executed): disable the ``if state is None`` guard in
    ``save_oauth_source`` → the ``real`` case fails (AttributeError on
    the missing state, before any write). Restored.
    """
    saved = factory(tmp_path).save_oauth_source("slack", "alpha", ("C001",))
    assert saved.ok is False, name
    assert (saved.summary, saved.config_file) == ("", "")
    assert saved.error is not None
    assert "not connected" in saved.error
