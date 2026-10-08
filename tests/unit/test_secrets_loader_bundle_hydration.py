"""Regression: bootstrap_secrets + SecretsLoader live-read = ONE bundle hydration site.

The 2026-06-01 production failure: kairix.credentials._resolve_embed
called SecretsLoader().require(...) but the loader snapshotted env
at construction. KAIRIX_LLM_API_KEY was in the bundle but absent from
the snapshot → SecretNotFoundError.

The structural fix:

1. SecretsLoader stores env by REFERENCE — every get() reads live.
2. kairix.secrets.bootstrap.bootstrap_secrets() hydrates the bundle
   ONCE at process boot. Wired into the CLI dispatcher, worker main,
   and MCP cli main.

After both: every loader sees every bundle secret regardless of
construction order. No per-call-site hydration hacks.

F2-clean: tests pass env as an explicit dict and mutate the dict to
simulate hydration. No process-env mutation; bootstrap_secrets
accepts an explicit bundle_path kwarg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_bootstrap_state() -> None:
    """Per-test bootstrap reset — the bootstrap guard is process-global,
    so tests that exercise it would otherwise leak state into each other.
    """
    from kairix.secrets.bootstrap import reset_for_tests

    reset_for_tests()


def test_loader_reads_env_live_so_post_construction_mutation_is_visible() -> None:
    """Loader stores env by reference — mutating the dict mid-flight surfaces in get().

    This is the structural property that closes the 2026-06-01 bug:
    in production, bootstrap_secrets() mutates os.environ; loaders
    constructed before or after that call all see the mutation
    because they hold a reference to the env mapping, not a snapshot.

    Test exercises the property by passing an explicit dict and
    mutating it post-construction (F2-clean — no process env mutation).
    """
    from kairix.secrets.loader import SecretsLoader

    env: dict[str, str] = {}
    loader = SecretsLoader(env=env)

    # Pre-mutation: loader sees an empty env.
    assert loader.get("provider", "llm", None, "api-key") is None

    # Simulate bootstrap_secrets() hydrating the bundle into env.
    env["KAIRIX_PROVIDER_LLM_API_KEY"] = "hydrated-from-bundle"  # pragma: allowlist secret

    # Same loader instance must see the new value via live read.
    value = loader.get("provider", "llm", None, "api-key")
    assert value == "hydrated-from-bundle", (
        f"loader constructed before mutation must still see post-mutation env; got {value!r}"
    )


def test_bootstrap_is_idempotent_one_shot_guard(tmp_path: Path) -> None:
    """Second bootstrap call is a no-op — production CLIs may double-call accidentally."""
    from kairix.secrets.bootstrap import bootstrap_secrets

    bundle = tmp_path / "kairix.env"
    bundle.write_text(
        "KAIRIX_BOOTSTRAP_TEST_VAR=v1\n",  # pragma: allowlist secret
        encoding="utf-8",
    )

    n1 = bootstrap_secrets(bundle_path=bundle)
    assert n1 >= 1, f"first call must hydrate; got {n1}"

    n2 = bootstrap_secrets(bundle_path=bundle)
    assert n2 == 0, f"second call must be a no-op; got {n2}"


def test_bootstrap_with_missing_bundle_returns_zero_does_not_raise(tmp_path: Path) -> None:
    """No bundle file = no-op + zero returned. Production must not crash on missing bundle."""
    from kairix.secrets.bootstrap import bootstrap_secrets

    n = bootstrap_secrets(bundle_path=tmp_path / "no-such-bundle.env")
    assert n == 0, f"missing bundle must return 0; got {n}"


def test_bootstrap_absent_bundle_does_not_latch_so_later_write_recovers(tmp_path: Path) -> None:
    """Boot secret-load race (#733): an initially-ABSENT bundle must not latch.

    On a container/VM restart the search readiness path can resolve secrets
    BEFORE the ``kairix-fetch-secrets`` sidecar has written the tmpfs bundle.
    The old once-guard latched ``_BOOTSTRAPPED`` unconditionally, so the
    process cached the 'unavailable' state for its whole lifetime and served
    BM25-only until a human restarted the container. ``build_search_pipeline``
    re-runs ``bootstrap_secrets`` on every build (before its pipeline-cache
    lookup), so the fix is to latch only once hydration has SETTLED — when the
    bundle is still absent we stay un-latched and the next build recovers.

    F2-clean: injected ``bundle_path``; the env var is a unique, deliberately
    non-``KAIRIX_`` test-only slot (the bundle hydrates any key), popped
    either side so the real ``os.environ`` is left untouched.
    """
    import os

    from kairix.secrets.bootstrap import bootstrap_secrets

    var = "BOOTSTRAP_RACE_RECOVERY_TEST_VAR"
    os.environ.pop(var, None)
    try:
        bundle = tmp_path / "kairix.env"

        # First bootstrap during the race window: bundle not written yet.
        assert bootstrap_secrets(bundle_path=bundle) == 0, "absent bundle must return 0"

        # The fetch-secrets sidecar finishes and writes the tmpfs bundle.
        bundle.write_text(f"{var}=recovered\n", encoding="utf-8")  # pragma: allowlist secret

        # The next per-build bootstrap must re-attempt and hydrate — NOT stay
        # latched on the earlier miss. A 0 here is the silent BM25-only defect.
        n2 = bootstrap_secrets(bundle_path=bundle)
        assert n2 >= 1, (
            f"bootstrap must recover after an initially-absent bundle appears; "
            f"got {n2} — the process would be stuck BM25-only until a restart"
        )
        assert os.environ.get(var) == "recovered", "the appeared bundle must hydrate into env"
    finally:
        os.environ.pop(var, None)


def test_loader_explicit_env_is_live_reference_not_snapshot() -> None:
    """The explicit-env path is symmetric with the os.environ path —
    both are live reads of the mapping (no snapshot)."""
    from kairix.secrets.loader import SecretsLoader

    env: dict[str, str] = {"KAIRIX_PROVIDER_LLM_API_KEY": "first"}
    loader = SecretsLoader(env=env)
    assert loader.get("provider", "llm", None, "api-key") == "first"

    env["KAIRIX_PROVIDER_LLM_API_KEY"] = "second"  # pragma: allowlist secret
    assert loader.get("provider", "llm", None, "api-key") == "second", "loader must read env by reference, not snapshot"


def test_loader_require_raises_with_canonical_name_when_no_source_resolves() -> None:
    """No env entry → require() raises with the canonical name in the message."""
    from kairix.secrets.loader import SecretNotFoundError, SecretsLoader

    loader = SecretsLoader(env={})
    with pytest.raises(SecretNotFoundError, match=r"kairix-provider-llm-api-key"):
        loader.require("provider", "llm", None, "api-key")
