"""Outcome tests for the flag-gated web setup wizard (#474).

Every test composes the real Starlette app through
``kairix.agents.mcp.transport.build_mcp_app`` with the canonical fakes
(``FakeSetupService`` / ``FakeSecretsLoader`` / ``FakeMcpTransportServer``
from ``tests/fakes.py``) injected through the public seams — no
monkey-patching (F1/F2-clean by construction). Assertions are on the
rendered HTML / headers (F30 spirit), not on status codes alone.
"""

from __future__ import annotations

import logging
import re
from http.cookies import SimpleCookie

import pytest

# starlette ships via the optional [agents] extra (transitive dep of mcp);
# CI's base-deps stages must skip rather than fail on the missing import.
starlette = pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from kairix.agents.mcp.transport import build_mcp_app  # noqa: E402
from kairix.platform.setup.service import IndexStatus, SetupService  # noqa: E402

# Public guard surface (in routes.__all__). Importing the module's public
# names also keeps this file in the mutation-parity impacted set for
# kairix.platform.setup.web.routes — the guard's branches are pinned here,
# so a mutant on them must be caught by an assertion in this file.
from kairix.platform.setup.web.routes import (  # noqa: E402
    OPERATOR_TOKEN_HEADER,
    SETUP_PATH_PREFIX,
)
from tests.fakes import (  # noqa: E402
    FakeMcpTransportServer,
    FakeSecretsLoader,
    FakeSetupService,
)

pytestmark = pytest.mark.unit

_LOOPBACK = ("127.0.0.1", 9999)
# Fixture credential for the fake service, not a real key.
_SAVE_KEY_PAYLOAD = {
    "provider": "anthropic",
    "api_key": "fake-key-for-tests",  # pragma: allowlist secret
    "endpoint": "",
    "model": "model-alpha",
}
_OPERATOR_TOKEN_IDENTITY = ("infra", "operator", None, "token")
_TOKEN_HEADER = OPERATOR_TOKEN_HEADER
_SETUP_PREFIX = SETUP_PATH_PREFIX


def _build_client(
    *,
    service: SetupService | None = None,
    secrets: FakeSecretsLoader | None = None,
    client_addr: tuple[str, int] = _LOOPBACK,
    use_default_factory: bool = False,
    readiness_check: object = None,
    base_url: str = "http://testserver",
) -> TestClient:
    """Compose the app through the production composer with fakes."""
    resolved_service = service if service is not None else FakeSetupService()
    kwargs: dict[str, object] = {}
    if not use_default_factory:
        # Omitting the kwarg entirely exercises the production default
        # (the lazy build_setup_service stub).
        kwargs["setup_service_factory"] = lambda: resolved_service
    app = build_mcp_app(
        FakeMcpTransportServer(),
        setup_secrets=secrets if secrets is not None else FakeSecretsLoader(),
        readiness_check=readiness_check,  # type: ignore[arg-type]  # F3 rationale: None or callable; build_mcp_app accepts both.
        **kwargs,  # type: ignore[arg-type]  # F3 rationale: heterogeneous kwargs dict for an optional seam; build_mcp_app validates the shape.
    )
    return TestClient(app, client=client_addr, base_url=base_url)


# ---------------------------------------------------------------------------
# Mount (wizard is always served — the setup_wizard_web cutover flag retired,
# PLA-287)
# ---------------------------------------------------------------------------


def test_wizard_mounts_out_of_box() -> None:
    """A fresh install with all defaults mounts ``/setup`` out of the box.

    Builds the app with no explicit service factory so the production
    default (the lazy ``build_setup_service`` stub) is exercised; the
    welcome screen must return 200 while the MCP transport is unchanged.
    """
    app = build_mcp_app(FakeMcpTransportServer())
    client = TestClient(app, client=_LOOPBACK)
    assert client.get("/setup", follow_redirects=True).status_code == 200
    assert client.get("/mcp").status_code == 200


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def test_welcome_screen_invites_the_operator() -> None:
    client = _build_client()
    response = client.get("/setup", follow_redirects=True)
    assert response.status_code == 200
    assert "Welcome to kairix" in response.text
    assert "Get started" in response.text
    assert "/setup/provider" in response.text


def test_welcome_screen_references_only_local_assets() -> None:
    """No CDN URLs — the container may be offline."""
    client = _build_client()
    response = client.get("/setup", follow_redirects=True)
    assert "/setup/static/pico.classless.min.css" in response.text
    assert "/setup/static/htmx.min.js" in response.text
    assert "unpkg.com" not in response.text


def test_provider_screen_lists_installed_provider_plugins() -> None:
    client = _build_client()
    response = client.get("/setup/provider")
    assert response.status_code == 200
    # Names come from the real plugin registry (entry points), not canned data.
    assert "anthropic" in response.text
    assert "openai" in response.text
    assert "Bring your own key" in response.text


def test_key_screen_requires_provider_selection() -> None:
    client = _build_client()
    response = client.get("/setup/key", follow_redirects=True)
    assert "Choose an AI provider" in response.text


def test_key_screen_names_the_chosen_provider() -> None:
    client = _build_client()
    response = client.get("/setup/key", params={"provider": "anthropic"})
    assert response.status_code == 200
    assert "anthropic" in response.text
    assert "Validate key" in response.text


def test_key_screen_offers_deployment_field_for_azure_providers() -> None:
    """#484 — Azure routes requests by deployment name, so azure-shaped
    providers get an optional deployment input with grade-8 help copy."""
    client = _build_client()
    for provider in ("azure_foundry", "azure_legacy"):
        response = client.get("/setup/key", params={"provider": provider})
        assert response.status_code == 200
        assert "Deployment name" in response.text
        assert 'name="deployment"' in response.text
        assert "Azure gives each model you deploy its own name" in response.text


def test_key_screen_hides_deployment_field_for_non_azure_providers() -> None:
    client = _build_client()
    for provider in ("anthropic", "openai", "ollama"):
        response = client.get("/setup/key", params={"provider": provider})
        assert response.status_code == 200
        assert "Deployment name" not in response.text
        assert 'name="deployment"' not in response.text


def test_static_assets_are_served_from_the_package() -> None:
    client = _build_client()
    css = client.get("/setup/static/kairix.css")
    assert css.status_code == 200
    assert "kx-setup-card" in css.text
    assert client.get("/setup/static/htmx.min.js").status_code == 200
    assert client.get("/setup/static/pico.classless.min.css").status_code == 200


@pytest.mark.parametrize(
    ("path", "params", "button_id", "success_selector"),
    [
        pytest.param("/setup/key", {"provider": "anthropic"}, "save-key-btn", ".kx-validation-success", id="key"),
        pytest.param("/setup/folder", {}, "start-indexing-btn", ".kx-scan-result", id="folder"),
    ],
)
def test_reveal_buttons_are_wired_to_the_served_reveal_script(
    path: str, params: dict[str, str], button_id: str, success_selector: str
) -> None:
    """The hidden action button opts into the static reveal glue (F91 —
    no inline script) and the script it relies on is actually served."""
    client = _build_client()
    html = client.get(path, params=params).text
    button = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', html)
    assert button is not None
    assert f'data-kx-reveal-on="{success_selector}"' in button.group(0)
    assert '<script src="/setup/static/kx-reveal.js"' in html
    script = client.get("/setup/static/kx-reveal.js")
    assert script.status_code == 200
    assert "data-kx-reveal-on" in script.text
    assert "kx-revealed" in script.text


# ---------------------------------------------------------------------------
# Key validation + save
# ---------------------------------------------------------------------------


def test_key_validation_success_lists_models() -> None:
    service = FakeSetupService(models=("model-alpha", "model-beta"))
    client = _build_client(service=service)
    response = client.post(
        "/setup/key/validate",
        data={"provider": "anthropic", "api_key": "fake-key-for-tests", "endpoint": ""},  # pragma: allowlist secret
    )
    assert response.status_code == 200
    assert "Key validated successfully" in response.text
    assert "model-alpha" in response.text
    assert "model-beta" in response.text
    # The wizard passed the form through to the service unchanged
    # (empty endpoint normalised to None; no deployment field posted).
    assert service.validate_calls == [("anthropic", "fake-key-for-tests", None, None)]


def test_key_validation_failure_renders_guided_error() -> None:
    service = FakeSetupService(validate_ok=False)
    client = _build_client(service=service)
    response = client.post(
        "/setup/key/validate",
        data={"provider": "anthropic", "api_key": "fake-bad-key", "endpoint": ""},  # pragma: allowlist secret
    )
    assert response.status_code == 200
    assert "rejected by the provider" in response.text
    assert "Fix" in response.text
    assert "Next" in response.text
    # Internal rule IDs never appear in user copy.
    assert "F21" not in response.text


def test_api_key_never_echoed_or_logged(caplog: pytest.LogCaptureFixture) -> None:
    """F15 — the pasted key must not appear in any response body or log."""
    secret_key = "fake-key-do-not-echo-1234567890"  # pragma: allowlist secret
    service = FakeSetupService(validate_ok=False)
    client = _build_client(service=service)
    with caplog.at_level(logging.DEBUG):
        failure = client.post(
            "/setup/key/validate",
            data={"provider": "anthropic", "api_key": secret_key, "endpoint": ""},
        )
        success = client.post(
            "/setup/key/validate",
            data={"provider": "anthropic", "api_key": secret_key, "endpoint": ""},
        )
    assert secret_key not in failure.text
    assert secret_key not in success.text
    assert secret_key not in caplog.text


def test_key_validate_passes_the_deployment_name_through() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post(
        "/setup/key/validate",
        data={
            "provider": "azure_foundry",
            "api_key": "fake-key-for-tests",  # pragma: allowlist secret
            "endpoint": "https://res.services.ai.azure.com",
            "deployment": "my-embed-deploy",
        },
    )
    assert response.status_code == 200
    assert service.validate_calls == [
        ("azure_foundry", "fake-key-for-tests", "https://res.services.ai.azure.com", "my-embed-deploy")
    ]


def test_deployment_not_found_renders_key_works_guidance() -> None:
    """#484 — DeploymentNotFound must NOT show the generic key-blame block;
    the key authenticated, only the deployment name is wrong."""
    service = FakeSetupService(validate_deployment_missing=True)
    client = _build_client(service=service)
    response = client.post(
        "/setup/key/validate",
        data={
            "provider": "azure_foundry",
            "api_key": "fake-key-for-tests",  # pragma: allowlist secret
            "endpoint": "https://res.services.ai.azure.com",
            "deployment": "wrong-name",
        },
    )
    assert response.status_code == 200
    assert "Your key works" in response.text
    assert "no deployment named" in response.text
    assert "deployment field" in response.text
    # The generic key-blame guidance stays out of this case.
    assert "copied the key completely" not in response.text


def test_key_save_persists_provider_and_advances_to_folder() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post(
        "/setup/key",
        data=_SAVE_KEY_PAYLOAD,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/folder"
    assert service.saved_providers == [("anthropic", "fake-key-for-tests", None, "model-alpha", None)]


def test_key_save_on_read_only_config_renders_overlay_rescue_banner() -> None:
    """#485 — a read-only config mount must NOT surface as a raw 500; the
    banner names the failing path and the overlay rescue, F21-shaped."""
    service = FakeSetupService(
        save_provider_raises=OSError(30, "Read-only file system", "/etc/kairix/kairix.config.yaml"),
    )
    client = _build_client(service=service)
    response = client.post("/setup/key", data=_SAVE_KEY_PAYLOAD, follow_redirects=False)
    assert response.status_code == 200
    assert "Could not write the config file at /etc/kairix/kairix.config.yaml" in response.text
    assert "read-only" in response.text
    assert "KAIRIX_CONFIG_OVERLAY_PATH" in response.text
    assert "/var/lib/kairix/kairix.config.local.yaml" in response.text
    assert "fix:" in response.text
    assert "next:" in response.text
    # The config rescue stays config-shaped — never the secrets one (M2).
    assert "KAIRIX_SECRETS_FILE" not in response.text
    # The operator stays on the key screen — nothing was saved.
    assert service.saved_providers == []


def test_key_save_on_read_only_secrets_renders_the_secrets_rescue_banner() -> None:
    """Review M2 — failing to persist the KEY must prescribe
    KAIRIX_SECRETS_FILE and name the bundle path; the config-overlay
    rescue cannot fix a read-only secrets mount."""
    from kairix.platform.setup.service import SecretsWriteError

    service = FakeSetupService(save_provider_raises=SecretsWriteError("/run/secrets/kairix.env"))
    client = _build_client(service=service)
    response = client.post("/setup/key", data=_SAVE_KEY_PAYLOAD, follow_redirects=False)
    assert response.status_code == 200
    assert "Could not save the API key to the secrets file at /run/secrets/kairix.env" in response.text
    assert "KAIRIX_SECRETS_FILE" in response.text
    assert "KAIRIX_CONFIG_OVERLAY_PATH" not in response.text
    assert "fix:" in response.text
    assert "next:" in response.text
    assert service.saved_providers == []


def test_key_save_rejects_an_empty_api_key_with_guidance() -> None:
    """Review H4 — an empty key must not be persisted silently."""
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post(
        "/setup/key",
        data={"provider": "anthropic", "api_key": "", "endpoint": ""},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "The API key field is empty" in response.text
    assert "fix:" in response.text
    assert service.saved_providers == []


def test_key_save_rejects_a_missing_provider_with_guidance() -> None:
    """Review H4 — a crafted POST without the hidden provider field must
    not write ``{"provider": ""}`` to the config."""
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post(
        "/setup/key",
        data={"api_key": "fake-key-for-tests"},  # pragma: allowlist secret
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "No provider is selected" in response.text
    assert "fix:" in response.text
    assert service.saved_providers == []


# ---------------------------------------------------------------------------
# Folder scan + save
# ---------------------------------------------------------------------------


def test_folder_scan_renders_cost_estimate_card() -> None:
    service = FakeSetupService(scan_files=533, scan_words=3_200_000, scan_cost_usd=0.04)
    client = _build_client(service=service)
    response = client.post("/setup/folder/scan", data={"folder_path": "~/Documents"})
    assert response.status_code == 200
    assert "Scan complete" in response.text
    assert "533" in response.text
    assert "3,200,000" in response.text
    assert "$0.04" in response.text
    assert "~/Documents" in response.text
    # The prototype's bogus cache-hit promise was deliberately dropped.
    assert "cache hit" not in response.text


def test_folder_scan_failure_renders_guided_error() -> None:
    service = FakeSetupService(scan_ok=False)
    client = _build_client(service=service)
    response = client.post("/setup/folder/scan", data={"folder_path": "/nonexistent"})
    assert response.status_code == 200
    assert "not found or not readable" in response.text
    assert "Fix" in response.text
    # The success-only reveal hook must be absent so "Start indexing"
    # stays hidden after a failed scan.
    assert "kx-scan-result" not in response.text


def test_folder_save_records_source_and_starts_indexing() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/folder", data={"folder_path": "~/Documents"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/indexing"
    assert service.saved_sources == ["~/Documents"]
    assert service.start_index_calls == 1


def test_folder_save_on_read_only_config_renders_rescue_and_skips_indexing() -> None:
    """#485 — the folder save mirrors the key save: F21 banner, no raw
    500, and indexing must not start on a failed config write."""
    service = FakeSetupService(
        save_source_raises=OSError(30, "Read-only file system", "/etc/kairix/kairix.config.yaml"),
    )
    client = _build_client(service=service)
    response = client.post("/setup/folder", data={"folder_path": "~/Documents"}, follow_redirects=False)
    assert response.status_code == 200
    assert "Could not write the config file at /etc/kairix/kairix.config.yaml" in response.text
    assert "KAIRIX_CONFIG_OVERLAY_PATH" in response.text
    assert "fix:" in response.text
    assert "next:" in response.text
    # The typed path survives the re-render so the operator can retry.
    assert 'value="~/Documents"' in response.text
    assert service.start_index_calls == 0


def test_folder_save_validation_reject_renders_banner_and_skips_indexing() -> None:
    """Review H4 — ``save_source``'s documented ValueError (relative or
    missing path; reachable because the field is editable after the
    scan) renders the F21 banner instead of a raw 500, and indexing
    must not start."""
    message = (
        "Folder not found or not readable: /data/typo."
        " fix: create the folder (or fix the spelling), then scan it again."
        " next: the scan step confirms the folder before it is saved."
    )
    service = FakeSetupService(save_source_raises=ValueError(message))
    client = _build_client(service=service)
    response = client.post("/setup/folder", data={"folder_path": "/data/typo"}, follow_redirects=False)
    assert response.status_code == 200
    assert "Folder not found or not readable: /data/typo" in response.text
    assert "fix:" in response.text
    assert "next:" in response.text
    # The typed path survives the re-render so the operator can retry.
    assert 'value="/data/typo"' in response.text
    assert service.start_index_calls == 0


def test_folder_screen_prefills_the_mounted_root_in_container_mode() -> None:
    """#486 — inside a container the folder field starts at the mounted
    document root with Docker-aware helper copy."""
    service = FakeSetupService(in_container=True, suggested_folder="/data/documents")
    client = _build_client(service=service)
    response = client.get("/setup/folder")
    assert response.status_code == 200
    assert 'value="/data/documents"' in response.text
    assert "Running in Docker?" in response.text
    assert "This is the folder you mounted" in response.text


def test_folder_screen_stays_blank_outside_a_container() -> None:
    service = FakeSetupService(in_container=False)
    client = _build_client(service=service)
    response = client.get("/setup/folder")
    assert response.status_code == 200
    assert 'value=""' in response.text
    assert "Running in Docker?" not in response.text


def test_folder_scan_rejects_relative_paths_naming_the_resolution_base() -> None:
    """#486 — the REAL backend behind the real routes: a relative path is
    rejected with copy that names the server's working folder instead of
    silently joining it."""
    from pathlib import Path

    from kairix.platform.setup.backends import SetupServiceDeps
    from kairix.platform.setup.service import build_setup_service

    service = build_setup_service(deps=SetupServiceDeps(environ={}))
    client = _build_client(service=service)
    response = client.post("/setup/folder/scan", data={"folder_path": "notes/projects"})
    assert response.status_code == 200
    assert "relative path" in response.text
    assert str(Path.cwd()) in response.text
    assert "full path" in response.text


# ---------------------------------------------------------------------------
# Indexing progress
# ---------------------------------------------------------------------------


def test_indexing_progress_advances_then_redirects() -> None:
    service = FakeSetupService(chunks_total=100, chunks_per_tick=50)
    client = _build_client(service=service)
    client.post("/setup/folder", data={"folder_path": "~/Documents"}, follow_redirects=False)

    first = client.get("/setup/indexing/progress")
    assert "50%" in first.text
    assert "in progress" in first.text
    assert "HX-Redirect" not in first.headers

    second = client.get("/setup/indexing/progress")
    assert "100%" in second.text
    assert "Indexing complete" in second.text
    assert second.headers["HX-Redirect"] == "/setup/tour"


def test_indexing_progress_with_unknown_total_shows_zero_percent() -> None:
    pending = IndexStatus(running=True, done=False, chunks_done=0, chunks_total=0, error=None)
    service = FakeSetupService(index_statuses=(pending,))
    client = _build_client(service=service)
    response = client.get("/setup/indexing/progress")
    assert "0%" in response.text
    assert "HX-Redirect" not in response.headers


def test_indexing_error_renders_guided_error_without_redirect() -> None:
    service = FakeSetupService(index_error="Indexing stopped: provider rejected the embed request.")
    client = _build_client(service=service)
    response = client.get("/setup/indexing/progress")
    assert "Indexing stopped" in response.text
    assert "Fix" in response.text
    assert "HX-Redirect" not in response.headers


def test_indexing_screen_polls_the_progress_endpoint() -> None:
    client = _build_client()
    response = client.get("/setup/indexing")
    assert "/setup/indexing/progress" in response.text
    assert "every 1s" in response.text


def test_indexing_an_empty_folder_explains_instead_of_spinning() -> None:
    """Review M1 — a run over a folder with nothing readable completes
    with the honest 0-documents copy; the screen must not auto-advance
    to the tour, and must not spin forever."""
    service = FakeSetupService(chunks_total=0)
    client = _build_client(service=service)
    client.post("/setup/folder", data={"folder_path": "/data/empty"}, follow_redirects=False)
    response = client.get("/setup/indexing/progress")
    assert response.status_code == 200
    assert "0 documents indexed" in response.text
    assert "fix:" in response.text
    assert "HX-Redirect" not in response.headers
    assert "Indexing in progress" not in response.text


# ---------------------------------------------------------------------------
# Capability tour (#490)
# ---------------------------------------------------------------------------

# The five MCP tool names the tour and the done screen surface, exactly
# as agents call them.
_TOUR_TOOL_NAMES = ("search", "prep", "memory_write", "brief", "timeline")


def test_tour_screen_offers_five_runnable_cards_with_tool_names() -> None:
    client = _build_client()
    response = client.get("/setup/tour")
    assert response.status_code == 200
    assert "See what your agents can do" in response.text
    assert response.text.count("Run it") == 5
    for tool in _TOUR_TOOL_NAMES:
        assert f'<code class="kx-tool-tag">{tool}</code>' in response.text
    # Long-running cards warn honestly about the wait.
    assert response.text.count("takes up to") == 2
    assert "real run against your documents" in response.text


def test_first_search_path_redirects_to_the_tour() -> None:
    client = _build_client()
    response = client.get("/setup/first-search", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/tour"
    followed = client.get("/setup/first-search", follow_redirects=True)
    assert "See what your agents can do" in followed.text


def test_search_returns_result_cards() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/search", data={"query": "project kickoff"})
    assert response.status_code == 200
    assert "Project kickoff notes" in response.text
    assert "92% match" in response.text
    assert "notes/kickoff.md" in response.text
    assert service.search_queries == ["project kickoff"]


def test_search_with_no_hits_renders_an_honest_empty_state() -> None:
    """Review L7 — zero hits get guidance, not a bare "0 results" count."""
    service = FakeSetupService(search_hits=())
    client = _build_client(service=service)
    response = client.post("/setup/search", data={"query": "nothing matches this"})
    assert response.status_code == 200
    assert "No matches for" in response.text
    assert "Try different words" in response.text
    assert "0 results" not in response.text


def test_tour_prep_renders_summary_and_sources() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/tour/prep", data={"query": "current projects"})
    assert response.status_code == 200
    assert "rollout plan is the main thread" in response.text
    assert "notes/kickoff.md" in response.text
    assert "notes/rollout.md" in response.text
    assert service.tour_prep_queries == ["current projects"]


def test_tour_prep_failure_renders_guidance_not_a_trace() -> None:
    service = FakeSetupService(
        tour_prep_message="The context pack could not be built. fix: check the provider key. next: run it again.",
    )
    client = _build_client(service=service)
    response = client.post("/setup/tour/prep", data={"query": "current projects"})
    assert response.status_code == 200
    assert "could not be built" in response.text
    assert "fix:" in response.text
    assert "next:" in response.text
    assert "Traceback" not in response.text


def test_tour_remember_renders_the_write_then_find_round_trip() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/tour/remember", data={"content": "Setup finished today."})
    assert response.status_code == 200
    assert "Saved, then found by search" in response.text
    assert "240" in response.text
    assert "agent-alpha" in response.text
    assert "notes/kickoff.md" in response.text  # the search leg's hit is shown
    assert service.tour_remember_contents == ["Setup finished today."]


def test_tour_remember_not_yet_found_is_reported_honestly() -> None:
    service = FakeSetupService(tour_remember_found=False)
    client = _build_client(service=service)
    response = client.post("/setup/tour/remember", data={"content": "Setup finished today."})
    assert "Saved in" in response.text
    assert "hasn't caught up with it yet" in response.text
    assert "found by search" not in response.text


def test_tour_brief_renders_the_briefing_preview() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/tour/brief")
    assert response.status_code == 200
    assert "the rollout kicked off and two decisions landed" in response.text
    assert service.tour_brief_calls == 1


def test_tour_brief_empty_on_a_fresh_store_explains_honestly() -> None:
    service = FakeSetupService(tour_brief_preview="", tour_brief_next_action="Try tool_search for now.")
    client = _build_client(service=service)
    response = client.post("/setup/tour/brief")
    assert "your brief gets richer as your team works" in response.text
    assert "recent decisions, open work" in response.text
    assert "Try tool_search for now." in response.text


def test_tour_brief_guidance_message_renders_inside_the_honest_empty_state() -> None:
    service = FakeSetupService(
        tour_brief_message="Briefings are written for a named agent. fix: add your agent. next: ask it for a brief.",
    )
    client = _build_client(service=service)
    response = client.post("/setup/tour/brief")
    assert "your brief gets richer as your team works" in response.text
    assert "fix: add your agent" in response.text


def test_tour_timeline_renders_dated_results() -> None:
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post("/setup/tour/timeline", data={"query": "last week"})
    assert response.status_code == 200
    assert "2026-06-08" in response.text
    assert "Sprint planning" in response.text
    assert "notes/kickoff.md" in response.text
    assert service.tour_timeline_queries == ["last week"]


def test_tour_timeline_empty_corpus_explains_the_fill_in() -> None:
    service = FakeSetupService(tour_timeline_hits=())
    client = _build_client(service=service)
    response = client.post("/setup/tour/timeline", data={"query": "last week"})
    assert "the timeline fills in" in response.text


# ---------------------------------------------------------------------------
# Connect agent
# ---------------------------------------------------------------------------


def test_connect_agent_screen_shows_mcp_url_and_snippets() -> None:
    service = FakeSetupService(mcp_url="http://127.0.0.1:8765/mcp")
    client = _build_client(service=service)
    response = client.get("/setup/connect-agent")
    assert response.status_code == 200
    # Review L1 — the displayed URL re-anchors on the live request
    # origin (the wizard and the MCP transport share one server), so
    # the endpoint default's host:port never leaks onto the page.
    assert "http://testserver/mcp" in response.text
    assert "127.0.0.1:8765" not in response.text
    assert "Claude Code" in response.text
    assert "mcpServers" in response.text
    assert "Verify connection" in response.text


def test_connect_agent_screen_shows_the_port_the_browser_reached() -> None:
    """Review L1 — a pip install whose port auto-shifted: the page must
    advertise the live origin's port, not KAIRIX_MCP_ENDPOINT's default."""
    service = FakeSetupService(mcp_url="http://localhost:8080/mcp")
    client = _build_client(service=service, base_url="http://testserver:8123")
    response = client.get("/setup/connect-agent")
    assert response.status_code == 200
    assert "http://testserver:8123/mcp" in response.text
    assert "localhost:8080" not in response.text
    # The copy-paste snippets carry the rebased URL too.
    assert "claude mcp add --transport http kairix http://testserver:8123/mcp" in response.text


def test_connect_agent_screen_falls_back_to_the_service_url_without_a_host() -> None:
    """A service URL with no host part (nothing to swap) renders verbatim."""
    service = FakeSetupService(mcp_url="run kairix mcp serve to get a URL")
    client = _build_client(service=service)
    response = client.get("/setup/connect-agent")
    assert response.status_code == 200
    assert "run kairix mcp serve to get a URL" in response.text


def test_connect_agent_screen_explains_which_url_works_where() -> None:
    """#487 — the transport matrix: local CLI agents take plain http,
    claude.ai / Claude Desktop need https behind a reverse proxy."""
    service = FakeSetupService(mcp_url="http://127.0.0.1:8765/mcp")
    client = _build_client(service=service)
    response = client.get("/setup/connect-agent")
    assert response.status_code == 200
    assert "Which URL works where" in response.text
    assert "SSH tunnel" in response.text
    assert "claude.ai" in response.text
    assert "https://" in response.text
    assert "reverse proxy" in response.text
    assert "docs/operations/OPERATIONS.md#deploying-behind-a-reverse-proxy" in response.text
    assert "localhost http is fine" in response.text


def test_connect_agent_screen_offers_the_claude_mcp_add_one_liner() -> None:
    """#487 — the one-command connect snippet carries the screen's real
    resolved URL (origin-rebased per review L1)."""
    service = FakeSetupService(mcp_url="http://127.0.0.1:8765/mcp")
    client = _build_client(service=service)
    response = client.get("/setup/connect-agent")
    assert "claude mcp add --transport http kairix http://testserver/mcp" in response.text


def test_connect_agent_verify_reports_tool_count() -> None:
    service = FakeSetupService(tools_count=12)
    client = _build_client(service=service)
    response = client.post("/setup/connect-agent/verify")
    assert "Agent connected" in response.text
    assert "12 tools available" in response.text


def test_connect_agent_verify_failure_renders_guided_error() -> None:
    service = FakeSetupService(handshake_ok=False)
    client = _build_client(service=service)
    response = client.post("/setup/connect-agent/verify")
    assert "No agent handshake observed" in response.text
    assert "Fix" in response.text


# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------


def test_done_screen_celebrates_indexed_chunks() -> None:
    service = FakeSetupService(chunks_total=100, chunks_per_tick=100)
    client = _build_client(service=service)
    client.post("/setup/folder", data={"folder_path": "~/Documents"}, follow_redirects=False)
    client.get("/setup/indexing/progress")
    response = client.get("/setup/done")
    assert response.status_code == 200
    assert "Your knowledge is ready" in response.text
    assert "100" in response.text
    assert "chunks indexed" in response.text


def test_done_screen_lists_the_five_mcp_tool_names() -> None:
    """#490 — the finish screen names each capability's MCP tool so the
    operator can repeat everything from their agent."""
    client = _build_client()
    response = client.get("/setup/done")
    assert response.status_code == 200
    for tool in _TOUR_TOOL_NAMES:
        assert f"<code>{tool}</code>" in response.text
    # The recap keeps the agent-connect pointer.
    assert "MCP connection you set up" in response.text


# ---------------------------------------------------------------------------
# Shared layout primitive (#488)
#
# Structural invariant: every full screen renders card → content →
# banner slot → action row, with every async result target INSIDE the
# slot — so no interactive element ever moves when a result arrives.
# ---------------------------------------------------------------------------

_BANNER_SLOT_ID_MARKUP = 'id="kx-banner-slot"'
_ACTION_ROW_CLASS = "kx-setup-actions"
_ACTION_ROW_RE = re.compile('<div class="' + _ACTION_ROW_CLASS + '">(.*?)</div>', re.DOTALL)
_BTN_SECONDARY = "kx-btn-secondary"
_BTN_OUTLINE = "kx-btn-outline"
_BTN_PRIMARY = "kx-btn-primary"

# (screen id, method, path, query params / form data) for every full
# screen in the wizard. All screens are GETs except the saved-source
# confirmation, which the wizard renders as the POST /setup/source/save
# response (its only render path).
_SCREEN_REQUESTS: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    ("welcome", "GET", "/setup", {}),
    ("provider", "GET", "/setup/provider", {}),
    ("key", "GET", "/setup/key", {"provider": "anthropic"}),
    ("folder", "GET", "/setup/folder", {}),
    ("indexing", "GET", "/setup/indexing", {}),
    ("tour", "GET", "/setup/tour", {}),
    ("connect-agent", "GET", "/setup/connect-agent", {}),
    ("done", "GET", "/setup/done", {}),
    # OAuth source-connect screens (#489) — same primitive, same step.
    ("source", "GET", "/setup/source", {}),
    ("source-connect", "GET", "/setup/source/connect", {"provider": "slack"}),
    ("source-wait", "GET", "/setup/source/wait", {"provider": "slack"}),
    ("source-picker", "GET", "/setup/source/picker", {"provider": "slack"}),
    ("source-saved", "POST", "/setup/source/save", {"provider": "slack", "unit": "C001"}),
)

# Canonical action-row composition per screen: Back (secondary) leftmost,
# helpers (outline) in the middle, the primary action rightmost. The
# tour's five per-card "Run it" buttons live in the content area (each
# with its own fixed result slot), so its action row stays a lone
# primary — same shape the first-search screen carried. The source and
# source-wait screens advance through their content (provider cards /
# the auto-advancing status poll), so their rows carry only Back.
_CANONICAL_ROWS: dict[str, tuple[str, ...]] = {
    "welcome": (_BTN_PRIMARY,),
    "provider": (_BTN_SECONDARY, _BTN_PRIMARY),
    "key": (_BTN_SECONDARY, _BTN_OUTLINE, _BTN_PRIMARY),
    "folder": (_BTN_SECONDARY, _BTN_OUTLINE, _BTN_PRIMARY),
    "indexing": (),
    "tour": (_BTN_PRIMARY,),
    "connect-agent": (_BTN_SECONDARY, _BTN_OUTLINE, _BTN_PRIMARY),
    "done": (_BTN_PRIMARY,),
    "source": (_BTN_SECONDARY,),
    "source-connect": (_BTN_SECONDARY, _BTN_PRIMARY),
    "source-wait": (_BTN_SECONDARY,),
    "source-picker": (_BTN_SECONDARY, _BTN_PRIMARY),
    "source-saved": (_BTN_SECONDARY, _BTN_PRIMARY),
}

# Per-screen HTMX swap target for screens that render async results
# into the shared banner slot. The tour renders into per-card slots
# instead — covered by test_tour_cards_own_fixed_result_slots below.
_ASYNC_TARGETS: dict[str, str] = {
    "key": "validation-result",
    "folder": "scan-result",
    "indexing": "indexing-progress",
    "connect-agent": "handshake-result",
    "source-wait": "source-auth-status",
}

# (id, method, path, form data) for every partial-rendering endpoint.
_PARTIAL_REQUESTS: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    ("key-validate", "POST", "/setup/key/validate", _SAVE_KEY_PAYLOAD),
    ("folder-scan", "POST", "/setup/folder/scan", {"folder_path": "~/Documents"}),
    ("indexing-progress", "GET", "/setup/indexing/progress", {}),
    ("search", "POST", "/setup/search", {"query": "project kickoff"}),
    ("connect-verify", "POST", "/setup/connect-agent/verify", {}),
    ("tour-prep", "POST", "/setup/tour/prep", {"query": "current projects"}),
    ("tour-remember", "POST", "/setup/tour/remember", {"content": "Setup finished today."}),
    ("tour-brief", "POST", "/setup/tour/brief", {}),
    ("tour-timeline", "POST", "/setup/tour/timeline", {"query": "last week"}),
    ("source-auth-status", "GET", "/setup/source/auth-status", {}),
)

# The tour's per-card HTMX swap targets — each must be a fixed-size slot
# present from first paint so the cards below never move (#488).
_TOUR_RESULT_TARGETS = (
    "tour-search-result",
    "tour-prep-result",
    "tour-remember-result",
    "tour-brief-result",
    "tour-timeline-result",
)


def _screen_html(method: str, path: str, payload: dict[str, str]) -> str:
    client = _build_client()
    if method == "GET":
        response = client.get(path, params=payload, follow_redirects=True)
    else:
        response = client.post(path, data=payload)
    assert response.status_code == 200
    return response.text


def _action_row(html: str) -> str:
    match = _ACTION_ROW_RE.search(html)
    assert match is not None, "canonical action row missing from the screen"
    return match.group(1)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [pytest.param(method, path, payload, id=name) for name, method, path, payload in _SCREEN_REQUESTS],
)
def test_every_screen_renders_the_banner_slot_above_the_action_row(
    method: str, path: str, payload: dict[str, str]
) -> None:
    html = _screen_html(method, path, payload)
    assert _BANNER_SLOT_ID_MARKUP in html
    assert _ACTION_ROW_CLASS in html
    # Document order: the banner slot always precedes the action row, so
    # results render above the buttons, never among or below them.
    assert html.index(_BANNER_SLOT_ID_MARKUP) < html.index(_ACTION_ROW_CLASS)


@pytest.mark.parametrize(
    ("name", "method", "path", "payload"),
    [pytest.param(name, method, path, payload, id=name) for name, method, path, payload in _SCREEN_REQUESTS],
)
def test_action_rows_share_the_canonical_composition(
    name: str, method: str, path: str, payload: dict[str, str]
) -> None:
    row = _action_row(_screen_html(method, path, payload))
    found = tuple(re.findall(r"kx-btn-(?:secondary|outline|primary)", row))
    assert found == _CANONICAL_ROWS[name]
    # No inline styles in the row: hidden-until-revealed actions hold
    # their slot via the kx-btn-reveal class, never display:none.
    assert "style=" not in row


@pytest.mark.parametrize(
    ("method", "path", "payload", "target_id"),
    [
        pytest.param(method, path, payload, _ASYNC_TARGETS[name], id=name)
        for name, method, path, payload in _SCREEN_REQUESTS
        if name in _ASYNC_TARGETS
    ],
)
def test_async_result_targets_live_inside_the_banner_slot(
    method: str, path: str, payload: dict[str, str], target_id: str
) -> None:
    html = _screen_html(method, path, payload)
    target_markup = f'id="{target_id}"'
    slot_at = html.index(_BANNER_SLOT_ID_MARKUP)
    actions_at = html.index(_ACTION_ROW_CLASS)
    target_at = html.index(target_markup)
    assert slot_at < target_at < actions_at, (
        f"{target_markup} must sit inside the banner slot (after {_BANNER_SLOT_ID_MARKUP}, "
        f"before the {_ACTION_ROW_CLASS} row) so the buttons never move when results render"
    )


def test_tour_cards_own_fixed_result_slots_above_the_action_row() -> None:
    """#490 — every tour card's swap target is a fixed-size slot rendered
    from first paint, before the action row, so nothing shifts when a
    run finishes (the per-card analogue of the banner-slot invariant)."""
    html = _screen_html("GET", "/setup/tour", {})
    actions_at = html.index(_ACTION_ROW_CLASS)
    for target_id in _TOUR_RESULT_TARGETS:
        target_markup = f'id="{target_id}" class="kx-tour-result"'
        assert target_markup in html, f"{target_id} must hold its slot with the kx-tour-result class"
        assert html.index(target_markup) < actions_at


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [pytest.param(method, path, payload, id=name) for name, method, path, payload in _SCREEN_REQUESTS],
)
def test_every_screen_uses_the_uniform_card_width(method: str, path: str, payload: dict[str, str]) -> None:
    html = _screen_html(method, path, payload)
    assert 'class="kx-setup-card' in html
    # The 2026-06-11 demo flagged the provider grid rendering in a wider
    # card than the other steps. One card width across every screen,
    # source screens included (#488): no screen opts into a
    # width-variant class.
    assert "kx-setup-card-wide" not in html


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [pytest.param(method, path, data, id=name) for name, method, path, data in _PARTIAL_REQUESTS],
)
def test_partials_render_into_the_slot_without_layout_markup(method: str, path: str, data: dict[str, str]) -> None:
    client = _build_client()
    response = client.request(method, path, data=data or None)
    assert response.status_code == 200
    # Partials swap INTO the banner slot; they never carry their own
    # slot or action-row scaffolding (which would nest or move buttons).
    assert "kx-banner-slot" not in response.text
    assert _ACTION_ROW_CLASS not in response.text


# ---------------------------------------------------------------------------
# Operator token guard
# ---------------------------------------------------------------------------


def test_remote_request_without_configured_token_is_refused() -> None:
    client = _build_client(secrets=FakeSecretsLoader(), client_addr=("203.0.113.7", 4242))
    response = client.get("/setup/provider")
    assert response.status_code == 403
    assert "no operator token is configured" in response.text


def test_remote_request_requires_matching_token() -> None:
    secrets = FakeSecretsLoader(values={_OPERATOR_TOKEN_IDENTITY: "fake-operator-token"})
    client = _build_client(secrets=secrets, client_addr=("203.0.113.7", 4242))

    refused = client.get("/setup/provider")
    assert refused.status_code == 403
    assert "requires a valid operator token" in refused.text

    wrong = client.get("/setup/provider", headers={_TOKEN_HEADER: "wrong-token"})
    assert wrong.status_code == 403

    allowed = client.get("/setup/provider", headers={_TOKEN_HEADER: "fake-operator-token"})
    assert allowed.status_code == 200
    assert "Choose an AI provider" in allowed.text


def test_token_refusal_never_echoes_the_provided_token() -> None:
    secrets = FakeSecretsLoader(values={_OPERATOR_TOKEN_IDENTITY: "fake-operator-token"})
    client = _build_client(secrets=secrets, client_addr=("203.0.113.7", 4242))
    response = client.get("/setup/provider", headers={_TOKEN_HEADER: "attacker-guess-value"})
    assert response.status_code == 403
    assert "attacker-guess-value" not in response.text


def test_loopback_request_skips_the_token() -> None:
    client = _build_client(secrets=FakeSecretsLoader(), client_addr=_LOOPBACK)
    response = client.get("/setup/provider")
    assert response.status_code == 200
    assert "Choose an AI provider" in response.text


def test_unknown_client_address_fails_closed() -> None:
    """A request with no peer address is treated as remote, not loopback."""
    service = FakeSetupService()
    app = build_mcp_app(
        FakeMcpTransportServer(),
        setup_service_factory=lambda: service,
        setup_secrets=FakeSecretsLoader(),
    )
    client = TestClient(app, client=None)  # type: ignore[arg-type]  # F3 rationale: TestClient accepts None to strip the peer address; the stub types only the tuple form.
    assert client.get("/setup/provider").status_code == 403


def test_default_secrets_seam_serves_loopback_without_config() -> None:
    """No injected secrets resolver → the production loader is the seam
    default; loopback requests never consult it so the wizard still serves."""
    service = FakeSetupService()
    app = build_mcp_app(
        FakeMcpTransportServer(),
        setup_service_factory=lambda: service,
    )
    client = TestClient(app, client=_LOOPBACK)
    response = client.get("/setup/provider")
    assert response.status_code == 200
    assert "Choose an AI provider" in response.text


# ---------------------------------------------------------------------------
# Tokened-URL → signed-cookie grant (#500)
# ---------------------------------------------------------------------------
#
# On stock Docker every published-port peer is the bridge gateway IP
# (never loopback) and browsers cannot send custom headers, so the
# header path is unusable for the wizard's own UI. The grant path lets a
# browser open /setup/?operator_token=<v> once, receive a signed cookie,
# and reach every later screen on that cookie. These tests drive the
# composed mount from a NON-loopback client address (the unit-level
# stand-in for the Docker bridge IP; the real bridge-source-IP proof
# lives in the Linux-only fresh-install-smoke browser stage).

_REMOTE = ("203.0.113.7", 4242)
_GRANT_COOKIE = "kairix_operator_grant"
# Fixture operator token — not a real credential.
_FAKE_TOKEN = "fake-operator-token"  # pragma: allowlist secret — fake fixture


def _grant_secrets() -> FakeSecretsLoader:
    return FakeSecretsLoader(values={_OPERATOR_TOKEN_IDENTITY: _FAKE_TOKEN})


def test_tokened_url_grants_a_signed_cookie_and_redirects() -> None:
    """GET /setup/?operator_token=<good> from a non-loopback browser
    returns 303 to the bare /setup/ with a Set-Cookie carrying the signed
    grant — and the token never appears in the redirect Location."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    response = client.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{_SETUP_PREFIX}/"
    set_cookie = response.headers["set-cookie"]
    assert _GRANT_COOKIE in set_cookie
    # The raw operator token never rides the cookie (F15).
    assert _FAKE_TOKEN not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert f"Path={_SETUP_PREFIX}" in set_cookie


def test_granted_cookie_reaches_the_wizard() -> None:
    """Following the grant redirect, the cookie the client now holds
    admits a later non-loopback GET to a wizard screen."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    # The TestClient persists Set-Cookie across requests, so following the
    # grant redirect leaves the signed cookie in its jar.
    client.get(f"/setup/?operator_token={_FAKE_TOKEN}", follow_redirects=True)
    allowed = client.get("/setup/provider")
    assert allowed.status_code == 200
    assert "Choose an AI provider" in allowed.text


def test_absent_cookie_from_non_loopback_is_refused() -> None:
    """A non-loopback request with neither header nor cookie still 403s."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    response = client.get("/setup/provider")
    assert response.status_code == 403
    assert "requires a valid operator token" in response.text


def test_tampered_grant_cookie_is_refused() -> None:
    """Flipping one character of the signed cookie's MAC invalidates it —
    the request falls through to the same 403 as no cookie."""
    secrets = _grant_secrets()
    grant_client = _build_client(secrets=secrets, client_addr=_REMOTE)
    redirect = grant_client.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    good_cookie = SimpleCookie(redirect.headers["set-cookie"])[_GRANT_COOKIE].value
    # Tamper the last MAC character (keep length + base64 alphabet valid).
    tampered = good_cookie[:-1] + ("A" if good_cookie[-1] != "A" else "B")
    assert tampered != good_cookie

    fresh = _build_client(secrets=secrets, client_addr=_REMOTE)
    fresh.cookies.set(_GRANT_COOKIE, tampered)
    refused = fresh.get("/setup/provider")
    assert refused.status_code == 403


def test_garbage_cookie_value_is_refused() -> None:
    """A non-base64 / structurally-broken cookie value never raises — it
    is refused like an absent cookie."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    client.cookies.set(_GRANT_COOKIE, "not-a-valid-grant")
    refused = client.get("/setup/provider")
    assert refused.status_code == 403


def test_wrong_grant_token_sets_no_cookie_and_is_refused() -> None:
    """A wrong ?operator_token value never sets a cookie and is refused
    (it must not silently grant access)."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    response = client.get(
        "/setup/?operator_token=attacker-guess",
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_grant_only_fires_on_the_mount_root_not_inner_paths() -> None:
    """The tokened-URL grant is accepted ONLY on the mount root. A token on
    an inner path (e.g. /setup/provider?operator_token=...) must NOT mint a
    cookie — it falls through to the normal check and is refused."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    response = client.get(
        f"/setup/provider?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_loopback_grant_is_unaffected_and_stays_cookie_free() -> None:
    """Loopback still reaches the wizard with neither header nor cookie —
    the grant path is purely additive for non-loopback browsers."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_LOOPBACK)
    response = client.get("/setup/provider")
    assert response.status_code == 200
    assert "Choose an AI provider" in response.text


def test_grant_redirect_carries_security_headers() -> None:
    """F91 Limb A on the 303 grant redirect: a RedirectResponse bypasses
    the _render chokepoint, so the guard must apply the nosniff /
    frame-denial / CSP triple to it explicitly."""
    client = _build_client(secrets=_grant_secrets(), client_addr=_REMOTE)
    response = client.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_grant_over_https_marks_the_cookie_secure() -> None:
    """When the request itself arrived over https the cookie is Secure;
    plain-http (the default tunnel case) leaves Secure off so the cookie
    survives http://."""
    secrets = _grant_secrets()
    https_client = _build_client(
        secrets=secrets,
        client_addr=_REMOTE,
        base_url="https://testserver",
    )
    secure = https_client.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    assert "Secure" in secure.headers["set-cookie"]

    http_client = _build_client(secrets=secrets, client_addr=_REMOTE)
    plain = http_client.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        follow_redirects=False,
    )
    assert "Secure" not in plain.headers["set-cookie"]


def test_grant_behind_tls_terminating_proxy_marks_the_cookie_secure() -> None:
    """A TLS-terminating reverse proxy forwards plain http to the app but
    signals the real scheme via X-Forwarded-Proto — the cookie must be
    Secure when that header says https, and plain otherwise."""
    secrets = _grant_secrets()
    forwarded_https = _build_client(secrets=secrets, client_addr=_REMOTE)
    secure = forwarded_https.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "Secure" in secure.headers["set-cookie"]

    forwarded_http = _build_client(secrets=secrets, client_addr=_REMOTE)
    plain = forwarded_http.get(
        f"/setup/?operator_token={_FAKE_TOKEN}",
        headers={"X-Forwarded-Proto": "http"},
        follow_redirects=False,
    )
    assert "Secure" not in plain.headers["set-cookie"]


# ---------------------------------------------------------------------------
# Production default factory + cold-start bypass
# ---------------------------------------------------------------------------


def test_flag_on_with_production_default_factory_serves_the_wizard() -> None:
    """The production ``build_setup_service`` backend now exists — the
    default factory must serve the welcome screen, not the pre-backend
    503 stub. Construction is side-effect free (seams resolve lazily on
    first use), so rendering the welcome screen touches nothing real."""
    client = _build_client(use_default_factory=True)
    response = client.get("/setup", follow_redirects=True)
    assert response.status_code == 200
    assert "Welcome to kairix" in response.text


def test_wizard_bypasses_the_cold_start_gate() -> None:
    client = _build_client(readiness_check=lambda: False)
    # The wizard answers during warm-up — it exists for first-boot operators.
    welcome = client.get("/setup", follow_redirects=True)
    assert welcome.status_code == 200
    assert "Welcome to kairix" in welcome.text
    # Everything else still gets the structured cold-start 503.
    assert client.get("/mcp").status_code == 503


# ---------------------------------------------------------------------------
# F91 Limb A — security headers on the wizard's HTML responses
# ---------------------------------------------------------------------------
#
# Contract: every HTML the wizard serves carries the nosniff /
# frame-denial / Content-Security-Policy triple. The static check
# (scripts/checks/check_f91_browser_surface.py) proves the render path
# SETS them; these tests prove they actually arrive on a live response,
# on both a full-screen render and an HTMX partial (both flow through
# the _render chokepoint). Sabotage proof: delete any of the three
# names from _SECURITY_HEADERS in routes.py and the matching assertion
# below fails.


def test_full_page_response_carries_security_headers() -> None:
    client = _build_client()
    response = client.get("/setup/provider")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    # Frame-denial via either header is acceptable; the wizard ships
    # X-Frame-Options: DENY plus frame-ancestors 'none' in the CSP.
    assert response.headers["X-Frame-Options"] == "DENY"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_htmx_partial_response_carries_security_headers() -> None:
    """The async partials (validation / scan / progress / search) are HTML
    the browser swaps into the live DOM — they must carry the same
    headers as the full screens, not just the initial page load."""
    service = FakeSetupService()
    client = _build_client(service=service)
    response = client.post(
        "/setup/key/validate",
        data={"provider": "anthropic", "api_key": "fake-key-for-tests"},  # pragma: allowlist secret
    )
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_security_csp_restricts_default_and_object_and_frame_sources() -> None:
    """The CSP closes the three highest-value browser vectors for a
    config-rendering surface: off-origin fetch (default-src), plugin
    execution (object-src), and clickjacking (frame-ancestors)."""
    client = _build_client()
    csp = client.get("/setup", follow_redirects=True).headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
