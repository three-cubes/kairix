"""End-to-end composed production path for the web setup wizard — F48.

The ``setup_wizard_web`` capability shipped across #474 (provider /
folder / index / search / agent handshake), #489 (OAuth source
connect) and #490 (capability tour). This is its composed-path
sibling: one operator journey through the REAL ASGI app
(``build_mcp_app`` → real wizard routes → real ``build_setup_service``
backend), with fakes only at the true process boundaries:

- provider HTTP (``FakeProvider`` behind the ``provider_factory`` seam
  and a deterministic embedder behind ``EmbedDependencies.embed_batch``),
- the OAuth provider (``FakeOAuth2Flow`` behind ``oauth_flow_factory``
  plus a scripted unit-discovery callable — Slack's Web API),
- the LLM behind the tour's prep / brief / timeline cards,
- environment reads (every path pinned to ``tmp_path`` through the
  same constructor seams production wires — F2/F4-clean, no
  ``KAIRIX_*`` env vars in-process).

Everything else is composed production code: the Starlette routes and
templates, the OperatorTokenGuard (with its OAuth-callback exemption),
``KairixSetupService``, ``persist_llm_credentials`` → ``set_secret``
into a real on-disk bundle, ``update_config_file`` onto a real overlay
YAML, ``run_first_index`` → ``run_incremental_embed_pipeline`` with the
real DocumentScanner / FTS rebuild / usearch ``VectorIndex`` / embed
flock, the real ``count_index_chunks`` + ``embed_lock_held`` progress
probes, ``build_search_pipeline`` (the F47 factory), the real
``remember`` use case for the tour's write-then-find proof, the real
``WizardCallbackListener`` nonce dance, ``source_secret_leaves``,
``topology_updates_for_source``, and ``parse_topology`` reading the
emitted overlay back.

The wizard is always mounted (the ``setup_wizard_web`` cutover flag
retired post-validation, PLA-287); this path exercises the composed
``/setup`` journey end to end.

Sabotage-proof (executed): making ``complete_source_callback`` accept a
forged ``state`` (deleting the mismatch rejection in
``kairix/platform/setup/backends.py``) fails this test at the
forged-callback 409 assertion; restoring the check turns it green.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

# starlette ships via the optional [agents] extra (transitive dep of mcp);
# CI's base-deps stages must skip rather than fail on the missing import.
starlette = pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from kairix.agents.mcp.transport import build_mcp_app  # noqa: E402
from kairix.config import parse_topology  # noqa: E402
from kairix.core.db import EMBED_VECTOR_DIMS, open_db  # noqa: E402
from kairix.core.db.schema import create_schema  # noqa: E402
from kairix.core.embed.cli import acquire_lock, release_lock  # noqa: E402
from kairix.core.embed.deps import EmbedDependencies  # noqa: E402
from kairix.core.embed.embed import open_usearch_index_for_paths, run_embed  # noqa: E402
from kairix.core.embed.embedding_cache import EmbeddingCache  # noqa: E402
from kairix.core.embed.schema import save_run_log  # noqa: E402
from kairix.core.embed.use_cases import (  # noqa: E402
    PipelineDeps,
    UseCaseDeps,
    default_index_file,
    default_scan_documents,
    run_incremental_embed_pipeline,
)
from kairix.core.factory import build_search_pipeline  # noqa: E402
from kairix.core.search.config import RetrievalConfig  # noqa: E402
from kairix.platform.setup.backends import (  # noqa: E402
    SetupServiceDeps,
    read_config_mapping,
    run_first_index,
    update_config_file,
)
from kairix.platform.setup.service import SourceUnit, build_setup_service  # noqa: E402
from kairix.platform.setup.wizard import persist_llm_credentials  # noqa: E402
from kairix.secrets import canonical_env_var, set_secret  # noqa: E402
from kairix.use_cases.remember import RememberDeps, remember  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeMcpTransportServer,
    FakeOAuth2Flow,
    FakePaths,
    FakeProvider,
    FakeProviderRegistry,
    FakeSecretsLoader,
)

pytestmark = pytest.mark.e2e

_LOOPBACK = ("127.0.0.1", 9999)
# The docker bridge gateway a stock-Docker browser peer presents — never
# loopback (#500). Composed non-loopback journeys ride this client addr.
_BRIDGE = ("172.18.0.1", 4242)
_OPERATOR_TOKEN_IDENTITY = ("infra", "operator", None, "token")
# Fixture operator token for the grant journey, not a real credential.
_GRANT_TOKEN = "fake-operator-token"  # pragma: allowlist secret — fake fixture
# Fixture credential for the fake provider seam, not a real key.
_FAKE_KEY = "fake-key-for-tests"  # pragma: allowlist secret
_HX_REDIRECT = "HX-Redirect"
_SLACK = "slack"
_WORKSPACE = "alpha"
_AUTH_STATUS_URL = "/setup/source/auth-status"
_PROGRESS_URL = "/setup/indexing/progress"
_POLL_DEADLINE_S = 60.0
# Distinctive memory text for the tour's write-then-find proof — words
# deliberately absent from the seeded corpus so the search leg's hit is
# unambiguously the just-written memory file.
_MEMORY_TEXT = "Tour checkpoint: agent-alpha verified the remember roundtrip lands in seconds."


def _deterministic_embed_batch(
    texts: list[str],
    _api_key: str,
    _endpoint: str,
    _deployment: str,
    dims: int,
    **_kwargs: Any,
) -> list[list[float]]:
    """Embedder at the provider-HTTP boundary — deterministic, never zero."""
    return [[(float(hash(t) % 997) + 1.0) / 998.0] * dims for t in texts]


@dataclass(frozen=True)
class _WizardWorld:
    """Tmp-path pinned locations the composed journey runs against."""

    docs: Path
    db_path: Path
    overlay: Path
    bundle: Path
    cache_path: Path
    run_log: Path

    @property
    def lockfile(self) -> Path:
        """The embed flock — same `embed.lock` site the service probes."""
        return self.db_path.parent / "embed.lock"


def _build_world(tmp_path: Path) -> _WizardWorld:
    """Seed a tiny real corpus + state directory under ``tmp_path``."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "kickoff.md").write_text(
        "# Project kickoff notes\n\nagent-alpha agreed the rollout starts next sprint. " * 10,
        encoding="utf-8",
    )
    (docs / "retro.md").write_text(
        "# Retro notes\n\nThe retro decided to keep the weekly demo cadence. " * 10,
        encoding="utf-8",
    )
    (docs / "roadmap.md").write_text(
        "# Roadmap\n\nThe roadmap names search quality as the next milestone. " * 10,
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    return _WizardWorld(
        docs=docs,
        db_path=state / "index.sqlite",
        overlay=tmp_path / "kairix.config.local.yaml",
        bundle=tmp_path / "secrets" / "kairix.env",
        cache_path=state / "embedding_cache.sqlite",
        run_log=tmp_path / "logs" / "embed-runs.json",
    )


def _scan_deps(world: _WizardWorld) -> UseCaseDeps:
    """The real document scan, pinned to the tmp corpus.

    ``resolve_config_path_fn`` / ``load_collections_fn`` return None so
    the scan walks the default "." collection of the tmp document root
    instead of whatever config the test process' cwd happens to carry;
    reflib mode "skip" keeps the 5,800-file bundled reference library
    out of a test that budgets <60s. Scanner, chunking, and FTS rebuild
    are the production implementations.
    """
    return UseCaseDeps(
        document_root_fn=lambda: world.docs,
        resolve_config_path_fn=lambda: None,
        load_collections_fn=lambda: None,
        reflib_index_mode_fn=lambda: "skip",
    )


def _composed_first_index(world: _WizardWorld) -> None:
    """The REAL first-index run: ``run_first_index`` over the REAL
    incremental embed pipeline, faked only at the provider-HTTP boundary
    and pinned to tmp paths through the production seams."""
    embed_deps = EmbedDependencies(
        get_azure_config=lambda: (_FAKE_KEY, "https://provider.example/v1", "fake-embed-model"),
        preflight_check=lambda *_a, **_kw: EMBED_VECTOR_DIMS,
        embed_batch=_deterministic_embed_batch,
        open_usearch_index=lambda: open_usearch_index_for_paths(
            index_path=world.db_path.parent / "vectors.usearch",
            meta_path=world.db_path.parent / "vectors.meta.json",
            db_path=world.db_path,
        ),
        get_document_root=lambda: str(world.docs),
        open_embedding_cache=lambda: EmbeddingCache(world.cache_path),
        get_reflib_index_mode=lambda: "skip",
    )
    pipeline_deps = PipelineDeps(
        db_path_fn=lambda: str(world.db_path),
        # The REAL embed stage, bound directly: the default wrapper is a
        # lazy-import indirection whose ``deps`` kwarg means UseCaseDeps,
        # so EmbedDependencies must flow to run_embed itself (the same
        # binding tests/embed/test_use_cases.py pins).
        run_embed_fn=run_embed,
        acquire_lock_fn=lambda: acquire_lock(lockfile=world.lockfile, wait_secs=5.0),
        release_lock_fn=lambda fh: release_lock(fh, lockfile=world.lockfile),
        save_run_log_fn=lambda entry: save_run_log(entry, log_path=world.run_log),
        scan_documents_fn=lambda db, diagnostics: default_scan_documents(db, diagnostics, deps=_scan_deps(world)),
    )
    run_first_index(
        pipeline_fn=lambda **kwargs: run_incremental_embed_pipeline(
            deps=embed_deps, pipeline_deps=pipeline_deps, **kwargs
        )
    )


def _index_memory_via_scan(world: _WizardWorld, db_path: Path, target: Path, content_hash: str) -> bool:
    """The remember use case's immediate-index leg, tmp-pinned.

    Same composition as the production default (``open_db`` →
    ``create_schema`` → ``default_index_file`` for the one written file →
    active-hash check), with the scan deps pinned to the tmp corpus for the
    same reason as :func:`_scan_deps`.
    """
    db = open_db(db_path)
    try:
        create_schema(db)
        diagnostics: list[str] = []
        default_index_file(db, diagnostics, target, deps=_scan_deps(world))
        row = db.execute(
            "SELECT 1 FROM documents WHERE hash = ? AND active = 1 LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row is not None
    finally:
        db.close()


def _wizard_service_deps(world: _WizardWorld) -> SetupServiceDeps:
    """Production ``SetupServiceDeps`` with fakes only at true boundaries.

    NOT overridden (real defaults carry): ``index_counts_fn``
    (``count_index_chunks`` against the tmp SQLite), ``embed_lock_probe_fn``
    (``embed_lock_held`` against the real flock the index run takes),
    ``source_options_fn`` (the shipped source cards).
    """
    search_registry = FakeProviderRegistry(
        {"fake": FakeProvider(name="fake", vector=[0.1] * EMBED_VECTOR_DIMS, dim=EMBED_VECTOR_DIMS)}
    )
    remember_deps = RememberDeps(
        config_fn=lambda: None,
        document_root_fn=lambda: world.docs,
        db_path_fn=lambda: world.db_path,
        index_fn=lambda db_path, _root, target, content_hash: _index_memory_via_scan(
            world, db_path, target, content_hash
        ),
    )
    return SetupServiceDeps(
        # Provider HTTP boundary — one fake plugin behind the factory seam.
        provider_factory=lambda name, _creds: FakeProvider(
            name=name, vector=[0.1] * EMBED_VECTOR_DIMS, dim=EMBED_VECTOR_DIMS
        ),
        # REAL persistence chain: persist_llm_credentials → set_secret →
        # tmp bundle file. Hydration into os.environ is the one skipped
        # side effect (F2 — tests must not mutate the process env).
        persist_credentials_fn=lambda key, endpoint, model: persist_llm_credentials(
            key, endpoint, model, bundle_path=world.bundle, hydrate_fn=lambda _p: 0
        ),
        credentials_probe=lambda: world.bundle.exists(),
        configured_document_root_fn=lambda: world.docs,
        # REAL config writes onto the tmp overlay file.
        write_config_fn=lambda updates: update_config_file(world.overlay, updates),
        read_config_fn=lambda: read_config_mapping(overlay_path=str(world.overlay), config_path=None),
        # REAL first index — composed embed pipeline (see helper).
        index_runner_fn=lambda: _composed_first_index(world),
        # REAL factory-built search pipeline (F47), provider faked.
        search_pipeline_factory=lambda p: build_search_pipeline(
            config=RetrievalConfig(provider="fake"), registry=search_registry, paths=p
        ),
        environ={},
        # Tour (#490): remember is the REAL use case; prep / brief /
        # timeline need an LLM or live retrieval state, so they ride
        # scripted seams (their real compositions are pinned at the
        # unit tier in tests/platform/setup/test_setup_service.py).
        remember_fn=lambda agent, content: remember(agent, content, deps=remember_deps),
        top_level_config_fn=lambda: None,
        prep_fn=lambda _query: SimpleNamespace(
            summary="Your documents cover the rollout kickoff and the retro cadence.",
            sources=("kickoff.md",),
            error="",
        ),
        brief_fn=lambda agent: SimpleNamespace(
            agent=agent,
            preview="agent-alpha shipped the rollout notes yesterday.",
            health=SimpleNamespace(next_action=""),
            error="",
        ),
        timeline_fn=lambda _query: SimpleNamespace(
            results=[
                SimpleNamespace(
                    title="Retro notes",
                    snippet="The retro decided to keep the weekly demo cadence.",
                    path="retro.md",
                    date="2026-06-10",
                )
            ],
            error="",
        ),
        # OAuth provider boundary (#489): the flow fake mirrors the real
        # builders — the authorize URL carries the service's single-use
        # state nonce, and authorize() blocks on the REAL listener.
        oauth_flow_factory=lambda request: FakeOAuth2Flow(
            browser=request.browser,
            authorize_url=f"https://provider.example/consent?state={request.nonce}",
        ),
        # REAL secret persistence for the captured tokens — canonical
        # names from the REAL source_secret_leaves walk, written by the
        # REAL set_secret into the tmp bundle.
        persist_secret_fn=lambda name, value: set_secret(name, value, bundle_path=world.bundle),
        # Slack Web API boundary — scripted picker rows.
        discover_units_fn=lambda _provider, _client, _tokens: (
            SourceUnit(unit_id="C1", name="#rollout", detail="public channel"),
            SourceUnit(unit_id="C2", name="#retro", detail="private channel"),
        ),
    )


def _compose_client(world: _WizardWorld) -> TestClient:
    """The REAL transport composer + REAL backend — wizard mounted unconditionally."""
    paths = FakePaths(
        document_root=world.docs,
        db_path=world.db_path,
        log_dir=world.run_log.parent,
        workspace_root=world.db_path.parent / "workspaces",
    )
    service = build_setup_service(paths=paths, deps=_wizard_service_deps(world))
    app = build_mcp_app(
        FakeMcpTransportServer(),
        setup_service_factory=lambda: service,
        setup_secrets=FakeSecretsLoader(),
    )
    return TestClient(app, client=_LOOPBACK)


def _poll(client: TestClient, url: str, done: Any, what: str) -> Any:
    """GET ``url`` until ``done(response)`` is truthy, within the deadline."""
    deadline = time.monotonic() + _POLL_DEADLINE_S
    last: Any = None
    while time.monotonic() < deadline:
        last = client.get(url)
        if done(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"{what} never happened within {_POLL_DEADLINE_S:.0f}s; last response: {last and last.text}")


# ---------------------------------------------------------------------------
# Journey legs (split per F16 — each leg is one linear helper)
# ---------------------------------------------------------------------------


def _drive_provider_step(client: TestClient, world: _WizardWorld) -> None:
    welcome = client.get("/setup", follow_redirects=True)
    assert welcome.status_code == 200
    assert "Welcome to kairix" in welcome.text

    validated = client.post(
        "/setup/key/validate",
        data={"provider": "openai", "api_key": _FAKE_KEY, "endpoint": ""},
    )
    assert validated.status_code == 200
    assert "Key validated successfully" in validated.text
    assert _FAKE_KEY not in validated.text  # F15 — the key is never echoed

    saved = client.post(
        "/setup/key",
        data={"provider": "openai", "api_key": _FAKE_KEY, "endpoint": "", "model": "fake-embed-model"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    # The credential landed through the REAL set_secret chain: the tmp
    # bundle carries the canonical env-var names (names only — F15).
    bundle_text = world.bundle.read_text(encoding="utf-8")
    assert canonical_env_var("provider", "llm", None, "api-key") + "=" in bundle_text
    assert canonical_env_var("provider", "llm", None, "endpoint") + "=" in bundle_text
    # The plugin pick landed in the overlay through the REAL merge-write.
    assert yaml.safe_load(world.overlay.read_text(encoding="utf-8"))["provider"] == "openai"


def _drive_folder_and_index(client: TestClient, world: _WizardWorld) -> None:
    scan = client.post("/setup/folder/scan", data={"folder_path": str(world.docs)})
    assert scan.status_code == 200
    assert "Scan complete" in scan.text
    assert "<dd>3</dd>" in scan.text  # the 3 seeded markdown files

    folder_saved = client.post("/setup/folder", data={"folder_path": str(world.docs)}, follow_redirects=False)
    assert folder_saved.status_code == 303
    config = yaml.safe_load(world.overlay.read_text(encoding="utf-8"))
    assert config["provider"] == "openai"  # provider pick survived the merge
    assert config["paths"]["document_root"] == str(world.docs)

    # The REAL embed pipeline runs on the wizard's background thread; the
    # progress poll reads the REAL content_vectors / pending counters and
    # redirects to the tour once pending == 0 and embedded > 0.
    done = _poll(
        client,
        _PROGRESS_URL,
        lambda r: r.headers.get(_HX_REDIRECT) == "/setup/tour",
        "first index reaching done",
    )
    assert "Indexing complete" in done.text
    assert (world.db_path.parent / "vectors.usearch").exists(), (
        "the real embed run must persist a usearch index next to the SQLite db"
    )


def _drive_first_search(client: TestClient) -> None:
    results = client.post("/setup/search", data={"query": "when does the rollout start"})
    assert results.status_code == 200
    assert "kickoff" in results.text, (
        f"the composed factory pipeline must surface the ingested corpus; got: {results.text[:500]}"
    )
    assert "% match" in results.text


def _drive_oauth_source_leg(client: TestClient, world: _WizardWorld) -> None:
    source = client.get("/setup/source")
    assert source.status_code == 200
    assert "Slack" in source.text

    connect_form = client.get(f"/setup/source/connect?provider={_SLACK}")
    assert connect_form.status_code == 200
    assert "http://testserver/setup/oauth/callback" in connect_form.text  # origin-derived redirect URI

    started = client.post(
        "/setup/source/connect",
        data={
            "provider": _SLACK,
            "workspace": _WORKSPACE,
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",  # pragma: allowlist secret — fixture value
        },
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"] == f"/setup/source/wait?provider={_SLACK}"

    # Consent phase: the status poll hands the operator's browser the
    # provider consent URL — which carries the single-use state nonce.
    consent = _poll(
        client,
        _AUTH_STATUS_URL,
        lambda r: r.headers.get(_HX_REDIRECT, "").startswith("https://provider.example/consent"),
        "source auth reaching the consent phase",
    )
    authorize_url = consent.headers[_HX_REDIRECT]
    nonce = parse_qs(urlparse(authorize_url).query)["state"][0]

    # A forged redirect must bounce off the REAL nonce verification —
    # the compensating control for the callback's token-guard exemption.
    forged = client.get("/setup/oauth/callback?code=evil&state=forged-nonce", follow_redirects=False)
    assert forged.status_code == 409
    assert "does not match" in forged.text

    # The genuine provider redirect rides the REAL route + REAL
    # complete_source_callback and unblocks the waiting flow.
    callback = client.get(f"/setup/oauth/callback?code=auth-1&state={nonce}", follow_redirects=False)
    assert callback.status_code == 303
    _poll(
        client,
        _AUTH_STATUS_URL,
        lambda r: r.headers.get(_HX_REDIRECT, "").startswith("/setup/source/picker"),
        "source auth reaching done",
    )

    # Captured tokens persisted under canonical names via the REAL
    # source_secret_leaves → set_secret chain (names only — F15).
    bundle_text = world.bundle.read_text(encoding="utf-8")
    for leaf in ("client-id", "client-secret", "bot-token"):
        assert canonical_env_var("connector", _SLACK, _WORKSPACE, leaf) + "=" in bundle_text

    picker = client.get(f"/setup/source/picker?provider={_SLACK}")
    assert picker.status_code == 200
    assert "#rollout" in picker.text

    saved = client.post(
        "/setup/source/save",
        data={"provider": _SLACK, "instance": _WORKSPACE, "unit": ["C1", "C2"]},
    )
    assert saved.status_code == 200
    assert "2 channels selected" in saved.text

    # The topology config landed on the overlay — read the file back and
    # parse it with the REAL parser the worker uses.
    parsed = parse_topology(yaml.safe_load(world.overlay.read_text(encoding="utf-8")))
    assert [c.id for c in parsed.connectors] == [f"{_SLACK}-{_WORKSPACE}-conn"]
    assert [p.connector for p in parsed.cc_pairs] == [f"{_SLACK}-{_WORKSPACE}-conn"]
    assert [c.name for c in parsed.collections] == [f"{_SLACK}-{_WORKSPACE}"]
    filters = sorted(s.path_filter for s in parsed.collections[0].sources)
    assert filters == ["slack://channel/C1/*", "slack://channel/C2/*"]


def _drive_tour(client: TestClient, world: _WizardWorld) -> None:
    tour = client.get("/setup/tour")
    assert tour.status_code == 200
    for tool in ("search", "prep", "memory_write", "brief", "timeline"):
        assert f'<code class="kx-tool-tag">{tool}</code>' in tour.text

    # The write-then-find card is the REAL composed leg: real remember
    # use case (write + scan-index) followed by the real factory search.
    roundtrip = client.post("/setup/tour/remember", data={"content": _MEMORY_TEXT})
    assert roundtrip.status_code == 200
    assert "Saved, then found by search" in roundtrip.text, (
        f"the remembered memory must come back through the real search pipeline; got: {roundtrip.text[:800]}"
    )
    memory_files = list((world.docs / "04-Agent-Knowledge").rglob("*.md"))
    assert len(memory_files) == 1, f"remember must write exactly one memory file; found {memory_files}"

    # The scripted-seam cards still render through the real routes.
    prep = client.post("/setup/tour/prep", data={"query": "current projects"})
    assert "rollout kickoff and the retro cadence" in prep.text
    brief = client.post("/setup/tour/brief", data={})
    assert "agent-alpha shipped the rollout notes" in brief.text
    timeline = client.post("/setup/tour/timeline", data={"query": "last week"})
    assert "weekly demo cadence" in timeline.text
    assert "2026-06-10" in timeline.text


def _drive_done_screen(client: TestClient) -> None:
    done = client.get("/setup/done")
    assert done.status_code == 200
    assert "Your knowledge is ready" in done.text
    assert "chunks indexed" in done.text
    for tool in ("search", "prep", "memory_write", "brief", "timeline"):
        assert f"<code>{tool}</code>" in done.text


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_composed_setup_wizard_journey(tmp_path: Path) -> None:
    """Flag ON: the full operator journey against composed production code.

    welcome → provider validate + save (real secrets bundle, real
    overlay) → folder scan + save → REAL first index (real scanner,
    FTS, usearch, flock; fake embedder) → progress poll to done → first
    search through the real factory pipeline → OAuth source connect
    (real callback route, real nonce verification, real secret + topology
    emission) → capability tour (real remember round trip) → done screen
    naming the five MCP tools.
    """
    world = _build_world(tmp_path)
    client = _compose_client(world)

    _drive_provider_step(client, world)
    _drive_folder_and_index(client, world)
    _drive_first_search(client)
    _drive_oauth_source_leg(client, world)
    _drive_tour(client, world)
    _drive_done_screen(client)


def test_composed_remote_browser_grant_journey(tmp_path: Path) -> None:
    """#500 — a non-loopback browser (the docker-bridge stand-in) reaches
    the wizard through the composed production guard via the tokened-URL →
    signed-cookie grant, then drives a real wizard leg (provider validate +
    folder scan) on that cookie alone — no header, no loopback.

    Everything is composed production code: the real transport composer,
    the real OperatorTokenGuard resolving the operator token through the
    secrets resolver, the real wizard routes + ``build_setup_service``
    backend. Only the provider HTTP / env reads are faked at their seams,
    exactly as the loopback journey above.
    """
    world = _build_world(tmp_path)
    paths = FakePaths(
        document_root=world.docs,
        db_path=world.db_path,
        log_dir=world.run_log.parent,
        workspace_root=world.db_path.parent / "workspaces",
    )
    service = build_setup_service(paths=paths, deps=_wizard_service_deps(world))
    app = build_mcp_app(
        FakeMcpTransportServer(),
        setup_service_factory=lambda: service,
        setup_secrets=FakeSecretsLoader(values={_OPERATOR_TOKEN_IDENTITY: _GRANT_TOKEN}),
    )
    bridge = TestClient(app, client=_BRIDGE)

    # Without the grant a bridge browser is locked out.
    assert bridge.get("/setup/provider").status_code == 403

    # The tokened URL grants a signed cookie and bounces to the start; the
    # token never lingers in the redirect Location (F15).
    grant = bridge.get(f"/setup/?operator_token={_GRANT_TOKEN}", follow_redirects=False)
    assert grant.status_code == 303
    assert grant.headers["location"] == "/setup/"
    assert _GRANT_TOKEN not in grant.headers["set-cookie"]

    # On the cookie alone (the TestClient jar retains it), the bridge
    # browser now drives real wizard legs.
    provider = bridge.get("/setup/provider")
    assert provider.status_code == 200
    assert "Choose an AI provider" in provider.text

    validated = bridge.post(
        "/setup/key/validate",
        data={"provider": "openai", "api_key": _FAKE_KEY, "endpoint": ""},
    )
    assert validated.status_code == 200
    assert "Key validated successfully" in validated.text
    assert _FAKE_KEY not in validated.text  # F15 — the key is never echoed

    scan = bridge.post("/setup/folder/scan", data={"folder_path": str(world.docs)})
    assert scan.status_code == 200
    assert "<dd>3</dd>" in scan.text  # the 3 seeded markdown files
