"""Tests for kairix.paths — centralised path resolution."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kairix.paths import (
    KairixPaths,
    Mode,
    PathTraversalError,
    agent_cli_roots,
    briefing_dir,
    bundled_suites_root,
    clear_cache,
    confine_to,
    confine_to_roots,
    default_cache_dir,
    default_data_dir,
    default_document_root,
    default_workspace_root,
    document_root,
    is_docker_runtime_check,
    is_service_install,
    load_paths_from_config,
    log_dir,
    maintenance_skip_noop_threshold,
    rechunk_sweep_per_tick_cap,
    reference_library_root,
)


@pytest.fixture(autouse=True)
def _clear_path_cache():
    """Clear the path cache before and after each test."""
    clear_cache()
    yield
    clear_cache()


@pytest.mark.unit
class TestKairixPaths:
    @pytest.mark.unit
    def test_document_root_from_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "/custom/vault"})
        assert paths.document_root == Path("/custom/vault")

    @pytest.mark.unit
    def test_db_path_from_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_DB_PATH": "/custom/db/index.sqlite"})
        assert paths.db_path == Path("/custom/db/index.sqlite")

    @pytest.mark.unit
    def test_log_dir_from_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_LOG_DIR": "/custom/logs"})
        assert paths.log_dir == Path("/custom/logs")

    @pytest.mark.unit
    def test_workspace_root_from_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_WORKSPACE_ROOT": "/custom/workspaces"})
        assert paths.workspace_root == Path("/custom/workspaces")

    @pytest.mark.unit
    def test_defaults_not_data_paths(self) -> None:
        """Default paths should not contain /data/ (TC-specific)."""
        paths = KairixPaths.resolve(env={})
        assert "/data/" not in str(paths.document_root)
        assert "/data/" not in str(paths.db_path)

    @pytest.mark.unit
    def test_docker_detection_via_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_DOCKER": "1"})
        assert str(paths.document_root) == "/data/documents"

    @pytest.mark.unit
    def test_explicit_env_resolution_is_not_cached(self) -> None:
        """An explicit ``env`` mapping is a one-off resolution — two different
        mappings resolve to two different roots (never a stale cached answer).

        Sabotage: route ``resolve(env=...)`` through the lru_cache'd
        ``_resolve_cached`` and the second call returns ``/first``.
        """
        paths1 = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "/first"})
        paths2 = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "/second"})
        assert paths1.document_root == Path("/first")
        assert paths2.document_root == Path("/second")

    @pytest.mark.unit
    def test_tilde_expansion(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "~/my-vault"})
        assert "~" not in str(paths.document_root)
        assert str(paths.document_root).endswith("/my-vault")


@pytest.mark.unit
class TestDocumentRootEnvVar:
    @pytest.mark.unit
    def test_document_root_from_env(self, tmp_path):
        result = document_root(env={"KAIRIX_DOCUMENT_ROOT": str(tmp_path)})
        assert result == tmp_path

    @pytest.mark.unit
    def test_document_root_default_when_unset(self):
        result = document_root(env={})
        assert result == Path.home() / "Documents"


# ---------------------------------------------------------------------------
# is_docker_runtime_check() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDocker:
    @pytest.mark.unit
    def test_dockerenv_file_present(self) -> None:
        """/.dockerenv existing should trigger docker detection."""
        with patch("os.path.exists", return_value=True):
            assert is_docker_runtime_check(env={}) is True

    @pytest.mark.unit
    def test_kairix_docker_env_var(self) -> None:
        """KAIRIX_DOCKER=1 should trigger docker detection."""
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check(env={"KAIRIX_DOCKER": "1"}) is True

    @pytest.mark.unit
    def test_container_env_var(self) -> None:
        """Non-empty 'container' env var should trigger docker detection."""
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check(env={"container": "podman"}) is True

    @pytest.mark.unit
    def test_not_docker_when_nothing_set(self) -> None:
        """Should return False when no docker indicators present."""
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check(env={}) is False


# ---------------------------------------------------------------------------
# is_service_install() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsServiceInstall:
    @pytest.mark.unit
    def test_service_install_when_venv_exists(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            assert is_service_install() is True

    @pytest.mark.unit
    def test_not_service_install_when_no_venv(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            assert is_service_install() is False


# ---------------------------------------------------------------------------
# default_data_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultDataDir:
    @pytest.mark.unit
    def test_docker_returns_fhs_var_lib(self) -> None:
        """Docker runtime resolves to the FHS ``/var/lib/kairix`` data dir,
        matching the image-baked ``KAIRIX_DATA_DIR`` and the per-mode
        ``data_dir(Mode.container)`` resolver (#447 / PLA-276 retired the old
        ``/data/kairix`` default that landed the index off the mounted volume).

        F2-clean: the docker signal is injected through the ``env`` seam, not
        ``monkeypatch.setenv``.

        Sabotage: revert the docker branch to ``Path("/data/kairix")`` and
        this assertion fails.
        """
        result = default_data_dir(env={"KAIRIX_DOCKER": "1"})
        assert result == Path("/var/lib/kairix")

    @pytest.mark.unit
    def test_xdg_data_home(self) -> None:
        with patch("os.path.exists", return_value=False):
            with patch.object(Path, "exists", return_value=False):
                result = default_data_dir(env={"XDG_DATA_HOME": "/custom/data"})
        assert result == Path("/custom/data/kairix")

    @pytest.mark.unit
    def test_windows_localappdata(self) -> None:
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_data_dir(platform="win32", env={"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"})
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix"

    @pytest.mark.unit
    def test_default_fallback(self) -> None:
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_data_dir(env={})
        assert result == Path.home() / ".local" / "share" / "kairix"


# ---------------------------------------------------------------------------
# default_cache_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultCacheDir:
    @pytest.mark.unit
    def test_docker_returns_fhs_var_cache(self) -> None:
        """Docker runtime resolves to the FHS ``/var/cache/kairix`` cache dir,
        matching the image-baked ``KAIRIX_CACHE_DIR`` and the per-mode
        ``cache_dir(Mode.container)`` resolver (#447 / PLA-276 retired the old
        ``/data/kairix`` default).

        F2-clean: the docker signal is injected through the ``env`` seam.

        Sabotage: revert the docker branch to ``Path("/data/kairix")`` → fails.
        """
        result = default_cache_dir(env={"KAIRIX_DOCKER": "1"})
        assert result == Path("/var/cache/kairix")

    @pytest.mark.unit
    def test_xdg_cache_home(self) -> None:
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir(env={"XDG_CACHE_HOME": "/custom/cache"})
        assert result == Path("/custom/cache/kairix")

    @pytest.mark.unit
    def test_windows_localappdata_cache(self) -> None:
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir(platform="win32", env={"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"})
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix" / "cache"

    @pytest.mark.unit
    def test_default_fallback(self) -> None:
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir(env={})
        assert result == Path.home() / ".cache" / "kairix"


# ---------------------------------------------------------------------------
# load_paths_from_config() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadPathsFromConfig:
    @pytest.mark.unit
    def test_returns_empty_when_no_config(self, tmp_path) -> None:
        result = load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(tmp_path / "nonexistent.yaml")})
        assert result == {}

    @pytest.mark.integration
    def test_loads_paths_from_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("paths:\n  document_root: /from/config\n  db_path: /from/config/db.sqlite\n")
        result = load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        assert result.get("document_root") == "/from/config"
        assert result.get("db_path") == "/from/config/db.sqlite"

    @pytest.mark.integration
    def test_graceful_fallback_on_malformed_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        result = load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        # Should return {} rather than raising
        assert isinstance(result, dict)

    @pytest.mark.integration
    def test_returns_empty_when_no_paths_section(self, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("logging:\n  level: DEBUG\n")
        result = load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        assert result == {}


# ---------------------------------------------------------------------------
# clear_cache() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClearCache:
    @pytest.mark.unit
    def test_clear_cache_invalidates(self) -> None:
        """``clear_cache()`` drops the cached process-wide resolution.

        Observed through object identity on the public ``resolve()`` (the
        process cache is the unit under test) rather than by mutating the
        process env between resolves.

        Sabotage: make ``clear_cache`` a no-op and the post-clear resolve
        returns the very same cached instance.
        """
        p1 = KairixPaths.resolve()
        assert KairixPaths.resolve() is p1, "resolve() must be cached per process"
        clear_cache()
        assert KairixPaths.resolve() is not p1


# ---------------------------------------------------------------------------
# Service-install branches — default_document_root / default_data_dir /
# default_cache_dir / default_workspace_root all check
# Path("/opt/kairix/.venv").exists() (via is_service_install) and return
# admin-configured paths when True. These four branches are uncovered
# because the local dev environment never has /opt/kairix/.venv.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServiceInstallDefaults:
    @pytest.mark.unit
    def test_service_install_document_root(self) -> None:
        """When /opt/kairix/.venv exists and Docker is not, doc root is /var/lib/kairix/documents."""
        with (
            patch("os.path.exists", return_value=False),  # not Docker
            patch.object(Path, "exists", return_value=True),  # /opt/kairix/.venv present
        ):
            assert default_document_root(env={}) == Path("/var/lib/kairix/documents")

    @pytest.mark.unit
    def test_service_install_data_dir(self) -> None:
        """Service install → data dir is /var/lib/kairix."""
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_data_dir(env={}) == Path("/var/lib/kairix")

    @pytest.mark.unit
    def test_service_install_cache_dir(self) -> None:
        """Service install → cache dir is /var/cache/kairix (NOT /var/lib/kairix)."""
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_cache_dir(env={}) == Path("/var/cache/kairix")

    @pytest.mark.unit
    def test_service_install_workspace_root(self) -> None:
        """Service install → workspaces under /var/lib/kairix/workspaces (FHS).

        #447 / PLA-276 reconciled this off the old /data/workspaces default
        so agent-memory logs sit on the same persistent data tree as the
        SQLite index. Docker shares the same FHS workspace root.
        """
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_workspace_root(env={}) == Path("/var/lib/kairix/workspaces")

    @pytest.mark.unit
    def test_workspace_root_user_default(self) -> None:
        """Neither Docker nor service install → workspaces under ~/.kairix/workspaces."""
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            assert default_workspace_root(env={}) == Path.home() / ".kairix" / "workspaces"


# ---------------------------------------------------------------------------
# #447 / PLA-276 — the primary SQLite index defaults to the PERSISTENT data
# dir (source of truth: FTS5 + content_vectors), never the regenerable cache,
# and the docker/container defaults match the FHS image layout
# (/var/lib/kairix, /var/cache/kairix, /data/documents). These pin the
# divergence the issue closed; F2-clean throughout (env seam + per-mode
# resolvers, no monkeypatch.setenv driving the docker detection).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrimaryIndexUnderDataDir:
    @pytest.mark.unit
    def test_db_path_defaults_under_data_dir_not_cache(self) -> None:
        """``KairixPaths.resolve().db_path`` falls back to
        ``default_data_dir()/index.sqlite`` (the persistent data dir), NOT
        ``default_cache_dir()/index.sqlite`` — matching ``index_path()``.

        An empty ``env`` mapping strips any ambient override so the
        documented FALLBACK is exercised (F2-clean); the hermetic conftest
        already empties the config search path.

        Sabotage: revert the ``_resolve_cached`` db_path fallback to
        ``cache_dir / "index.sqlite"`` and this fails on any host where the
        data dir differs from the cache dir.
        """
        resolved = KairixPaths.resolve(env={})
        assert resolved.db_path == default_data_dir(env={}) / "index.sqlite"
        assert resolved.db_path != default_cache_dir(env={}) / "index.sqlite"

    @pytest.mark.unit
    def test_index_path_container_is_on_fhs_data_tree(self) -> None:
        """``index_path(Mode.container)`` sits on the persistent FHS data dir
        (the same ``/var/lib/kairix`` tree the runtime db_path fallback now
        uses), closing the cache-vs-data divergence (#447).

        Sabotage: point ``data_dir``/``index_path`` at the cache root → fails.
        """
        from kairix.paths import Mode, data_dir, index_path

        assert index_path(Mode.container) == data_dir(Mode.container) / "index.sqlite"
        assert index_path(Mode.container) == Path("/var/lib/kairix/index.sqlite")


@pytest.mark.unit
class TestDataDirOverride:
    @pytest.mark.unit
    def test_kairix_data_dir_override_expands_user(self) -> None:
        """Regression: ``data_dir()`` ``~``-expands ``KAIRIX_DATA_DIR`` like
        ``default_data_dir`` does — pre-fix it returned ``Path("~/kairix-data")``
        so every cache under ``data_dir()`` landed in a literal ``~`` dir under
        the CWD. Sabotage: drop ``.expanduser()`` → fails.
        """
        from kairix.paths import data_dir

        assert data_dir(env={"KAIRIX_DATA_DIR": "~/kairix-data"}) == Path.home() / "kairix-data"


@pytest.mark.unit
class TestDockerFhsLayout:
    @pytest.mark.unit
    def test_docker_mode_defaults_match_fhs_layout(self) -> None:
        """The docker/container defaults match the FHS image layout:
        data=/var/lib/kairix, cache=/var/cache/kairix, documents=/data/documents.

        F2-clean: ``default_*`` driven via the ``env`` seam; the document
        root via the explicit-``mode`` per-mode resolver. No ``setenv``.

        Sabotage: revert any docker branch to ``/data/kairix`` → fails.
        """
        from kairix.paths import Mode, document_root

        assert default_data_dir(env={"KAIRIX_DOCKER": "1"}) == Path("/var/lib/kairix")
        assert default_cache_dir(env={"KAIRIX_DOCKER": "1"}) == Path("/var/cache/kairix")
        assert document_root(Mode.container) == Path("/data/documents")

    @pytest.mark.unit
    def test_docker_workspace_root_on_fhs_data_tree(self) -> None:
        """Docker workspace root lands under ``/var/lib/kairix/workspaces`` —
        on the mounted data volume, not the retired ``/data/workspaces``.

        F2-clean via the ``env`` seam.

        Sabotage: revert the docker branch to ``/data/workspaces`` → fails.
        """
        assert default_workspace_root(env={"KAIRIX_DOCKER": "1"}) == Path("/var/lib/kairix/workspaces")


# ---------------------------------------------------------------------------
# reference_library_root / bundled_suites_root — shipping-asset resolution
# (#450 moved suites in-wheel; the corpus stays a fetched-on-demand asset).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShippedAssetPaths:
    @pytest.mark.unit
    def test_reference_library_root_env_override(self) -> None:
        """KAIRIX_REFLIB_ROOT overrides every candidate, even when missing.

        The override is returned as-is so a misconfigured operator path
        surfaces as an explicit downstream error rather than silently
        falling back to a resolved candidate (#450).
        """
        assert reference_library_root(env={"KAIRIX_REFLIB_ROOT": "/custom/reflib"}) == Path("/custom/reflib")

    @pytest.mark.unit
    def test_reference_library_root_fallback_when_no_candidate(self) -> None:
        """With the env unset and no candidate dir on disk, falls back to the
        CWD-relative ``reference-library`` (legacy behaviour).

        Driven by pointing every real candidate at a non-existent tmp tree
        via the cache-dir override so the repo-root corpus on the dev host
        doesn't shadow the assertion — F2-clean: the only env touched is the
        documented cache override, not a kairix-internal seam.
        """
        # default_cache_dir honours KAIRIX_CACHE_DIR; point it at an empty
        # tmp tree so the cache candidate doesn't exist, and stub the
        # repo-root candidate out by asserting only when /opt + repo-root
        # also miss — which holds in CI's clean checkout. The robust, host-
        # independent assertion is on the resolver's documented contract
        # via resolve_first_existing_dir below.
        from kairix.paths import resolve_first_existing_dir

        result = resolve_first_existing_dir(
            override=None,
            candidates=[Path("/no/cache/reference-library"), Path("/no/opt/reference-library")],
            fallback=Path("reference-library"),
        )
        assert result == Path("reference-library")

    @pytest.mark.unit
    def test_reference_corpus_install_dir_is_cache_candidate(self, tmp_path) -> None:
        """``reference_corpus_install_dir`` equals the cache-dir candidate the
        resolver looks in — so a fetch lands where the next run reads from.
        """
        from kairix.paths import reference_corpus_install_dir

        env = {"KAIRIX_CACHE_DIR": str(tmp_path)}
        assert reference_corpus_install_dir(env=env) == tmp_path / "reference-library"

    @pytest.mark.unit
    def test_bundled_suites_root_resolves_in_package_copy(self) -> None:
        """With no override, ``bundled_suites_root`` returns the in-wheel
        package-data copy under ``kairix/data/suites`` and that copy holds
        the bundled suites (the #450 packaging guarantee at the resolver
        level — proves the in-package candidate is FIRST in the chain)."""
        root = bundled_suites_root()
        assert root.name == "suites"
        assert root.parent.name == "data"
        assert (root / "reflib-gold-v3.yaml").is_file()
        assert (root / "contract-suite.yaml").is_file()

    @pytest.mark.unit
    def test_bundled_suites_root_env_override(self) -> None:
        """KAIRIX_SUITES_ROOT overrides every other lookup (step 1 of #268 resolution)."""
        assert bundled_suites_root(env={"KAIRIX_SUITES_ROOT": "/custom/suites"}) == Path("/custom/suites")


# ---------------------------------------------------------------------------
# log_dir() convenience wrapper — uncovered because tests above call
# KairixPaths.resolve() directly. Exercise the function itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogDirWrapper:
    @pytest.mark.unit
    def test_log_dir_from_env(self) -> None:
        assert log_dir(env={"KAIRIX_LOG_DIR": "/custom/logs"}) == Path("/custom/logs")


# ---------------------------------------------------------------------------
# maintenance_skip_noop_threshold — three branches: unset (default 10),
# valid int, invalid string (logs warning + falls back to 10).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMaintenanceSkipNoopThreshold:
    @pytest.mark.unit
    def test_unset_returns_default_10(self) -> None:
        """Without the env var, threshold falls back to 10."""
        assert maintenance_skip_noop_threshold(env={}) == 10

    @pytest.mark.unit
    def test_valid_int_override(self) -> None:
        """A valid integer string is parsed and returned."""
        assert maintenance_skip_noop_threshold(env={"KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD": "42"}) == 42

    @pytest.mark.unit
    def test_invalid_falls_back_to_10_and_warns(self, caplog) -> None:
        """An unparseable value logs a warning and falls back to 10."""
        import logging

        env = {"KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD": "not-an-int"}
        with caplog.at_level(logging.WARNING, logger="kairix.paths"):
            assert maintenance_skip_noop_threshold(env=env) == 10
        assert any("not an int" in rec.message for rec in caplog.records), (
            "expected a warning about the invalid int value"
        )


# ---------------------------------------------------------------------------
# entity_overrides_path — #166: vault-driven entity-overrides file location.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEntityOverridesPath:
    @pytest.mark.unit
    def test_explicit_env_override_wins(self, tmp_path) -> None:
        """``KAIRIX_ENTITY_OVERRIDES_PATH`` takes precedence over the default."""
        from kairix.paths import entity_overrides_path

        custom = tmp_path / "custom-overrides.md"
        assert entity_overrides_path(env={"KAIRIX_ENTITY_OVERRIDES_PATH": str(custom)}) == custom

    @pytest.mark.unit
    def test_default_lives_under_document_root(self, tmp_path) -> None:
        """Without the override env var, the path sits under
        ``{document_root}/04-Agent-Knowledge/_entity-overrides.md``."""
        from kairix.paths import entity_overrides_path

        env = {"KAIRIX_DOCUMENT_ROOT": str(tmp_path)}
        assert entity_overrides_path(env=env) == tmp_path / "04-Agent-Knowledge" / "_entity-overrides.md"

    @pytest.mark.unit
    def test_explicit_env_expands_user(self) -> None:
        """A ``~``-prefixed override path is expanded to the home directory."""
        from kairix.paths import entity_overrides_path

        result = entity_overrides_path(env={"KAIRIX_ENTITY_OVERRIDES_PATH": "~/overrides.md"})
        assert "~" not in str(result)
        assert str(result).endswith("overrides.md")


# ---------------------------------------------------------------------------
# Typed env helpers — read_int_env / read_float_env / embed_pool_* /
# embed_vector_dims invalid-input branches.
#
# These tests sit at the bottom of the test pyramid for kairix.paths. The
# operator-visible behaviour of "I tune pool config via env vars" is
# pinned by BDD (tests/bdd/features/embed_pool_config.feature) +
# integration (tests/integration/test_embed_pool_config_e2e.py); both
# exercise the happy path through real make_openai_client + httpx flow.
# The defensive try/except branches below (invalid integer/float values
# from misconfigured Key Vault secrets) don't surface a user-visible
# change beyond a logged warning, so they need targeted unit coverage to
# pin the operator-misconfig guard behaviour the layer above relies on.
# ---------------------------------------------------------------------------


class TestReadIntEnv:
    @pytest.mark.unit
    def test_returns_int_when_set_valid(self) -> None:
        """Set env → parsed int. Sabotage: replace return int(raw) with default → fails."""
        from kairix.paths import read_int_env

        assert read_int_env("KAIRIX_TEST_INT", default=10, env={"KAIRIX_TEST_INT": "42"}) == 42

    @pytest.mark.unit
    def test_returns_default_when_unset(self) -> None:
        """Unset env → default. Sabotage: drop the None early-return → int(None) raises."""
        from kairix.paths import read_int_env

        assert read_int_env("KAIRIX_TEST_INT", default=7, env={}) == 7

    @pytest.mark.unit
    def test_returns_default_when_invalid(self) -> None:
        """Garbage → default + warning. Sabotage: remove try/except → int('abc') crashes."""
        from kairix.paths import read_int_env

        assert read_int_env("KAIRIX_TEST_INT", default=99, env={"KAIRIX_TEST_INT": "not-an-int"}) == 99


class TestReadFloatEnv:
    @pytest.mark.unit
    def test_returns_float_when_set_valid(self) -> None:
        """Set env → parsed float. Sabotage: change return to default."""
        from kairix.paths import read_float_env

        assert read_float_env("KAIRIX_TEST_FLOAT", default=1.0, env={"KAIRIX_TEST_FLOAT": "3.5"}) == 3.5

    @pytest.mark.unit
    def test_returns_default_when_unset(self) -> None:
        """Unset → default. Sabotage: drop None early-return → float(None) raises."""
        from kairix.paths import read_float_env

        assert read_float_env("KAIRIX_TEST_FLOAT", default=2.5, env={}) == 2.5

    @pytest.mark.unit
    def test_returns_default_when_invalid(self) -> None:
        """Garbage → fallback. Sabotage: remove try/except → ValueError on float('xyz')."""
        from kairix.paths import read_float_env

        assert read_float_env("KAIRIX_TEST_FLOAT", default=0.5, env={"KAIRIX_TEST_FLOAT": "xyz"}) == 0.5


class TestEmbedPoolKeepalive:
    @pytest.mark.unit
    def test_valid_env_returns_set_value(self) -> None:
        """KAIRIX_EMBED_POOL_KEEPALIVE=25 → 25. Sabotage: ignore env → default."""
        from kairix.paths import embed_pool_keepalive

        assert embed_pool_keepalive(10, env={"KAIRIX_EMBED_POOL_KEEPALIVE": "25"}) == 25

    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self) -> None:
        """Garbage → default + warning. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_pool_keepalive

        assert embed_pool_keepalive(10, env={"KAIRIX_EMBED_POOL_KEEPALIVE": "bad"}) == 10


class TestEmbedPoolExpiry:
    @pytest.mark.unit
    def test_valid_env_returns_set_value(self) -> None:
        """KAIRIX_EMBED_POOL_EXPIRY_S=45.5 → 45.5. Sabotage: ignore env → default."""
        from kairix.paths import embed_pool_expiry_s

        assert embed_pool_expiry_s(30.0, env={"KAIRIX_EMBED_POOL_EXPIRY_S": "45.5"}) == 45.5

    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self) -> None:
        """Garbage → default. Sabotage: remove try/except → float() raises."""
        from kairix.paths import embed_pool_expiry_s

        assert embed_pool_expiry_s(30.0, env={"KAIRIX_EMBED_POOL_EXPIRY_S": "nope"}) == 30.0


class TestEmbedVectorDimsFallback:
    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self) -> None:
        """KAIRIX_EMBED_DIMS=abc → default + warning. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_vector_dims

        assert embed_vector_dims(default=1536, env={"KAIRIX_EMBED_DIMS": "abc"}) == 1536


class TestEmbedCoalesceWindowMs:
    """Round-trip tests for ``embed_coalesce_window_ms`` (#288).

    F2-clean here because this file is the baselined home for
    kairix.paths env-var round-trip tests — env IS the public boundary
    we're verifying.
    """

    @pytest.mark.unit
    def test_default_when_unset(self) -> None:
        """Unset env → documented default 50.

        Sabotage: change the default in ``embed_coalesce_window_ms``
        from 50 to e.g. 5 and the documented behaviour drifts away
        from the actual fall-back.
        """
        from kairix.paths import embed_coalesce_window_ms

        assert embed_coalesce_window_ms(env={}) == 50

    @pytest.mark.unit
    def test_valid_int_passes_through(self) -> None:
        """An in-range value comes back unchanged.

        Sabotage: hard-code the return value and the operator's
        configured 100ms window is silently ignored.
        """
        from kairix.paths import embed_coalesce_window_ms

        assert embed_coalesce_window_ms(env={"KAIRIX_EMBED_COALESCE_WINDOW_MS": "100"}) == 100

    @pytest.mark.unit
    def test_oob_high_clamps_to_500(self) -> None:
        """Out-of-range high value clamps to the documented upper bound.

        Sabotage: drop the ``min(500, value)`` clamp and an operator
        typo (e.g. 99999) silently sets the window to 99 seconds —
        every embed call appears to hang.
        """
        from kairix.paths import embed_coalesce_window_ms

        assert embed_coalesce_window_ms(env={"KAIRIX_EMBED_COALESCE_WINDOW_MS": "99999"}) == 500

    @pytest.mark.unit
    def test_oob_low_clamps_to_zero(self) -> None:
        """Negative value clamps to 0 (which is the documented "disable" mode).

        Sabotage: drop the ``max(0, ...)`` clamp and a negative window
        causes Condition.wait to fire instantly — defeats coalescing.
        """
        from kairix.paths import embed_coalesce_window_ms

        assert embed_coalesce_window_ms(env={"KAIRIX_EMBED_COALESCE_WINDOW_MS": "-100"}) == 0

    @pytest.mark.unit
    def test_invalid_falls_back_to_default(self) -> None:
        """Garbage → default. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_coalesce_window_ms

        assert embed_coalesce_window_ms(env={"KAIRIX_EMBED_COALESCE_WINDOW_MS": "nope"}) == 50


class TestEmbedCoalesceMaxBatch:
    """Round-trip tests for ``embed_coalesce_max_batch`` (#288)."""

    @pytest.mark.unit
    def test_default_when_unset(self) -> None:
        """Unset env → documented default 16."""
        from kairix.paths import embed_coalesce_max_batch

        assert embed_coalesce_max_batch(env={}) == 16

    @pytest.mark.unit
    def test_valid_int_passes_through(self) -> None:
        """In-range value comes back unchanged."""
        from kairix.paths import embed_coalesce_max_batch

        assert embed_coalesce_max_batch(env={"KAIRIX_EMBED_COALESCE_MAX_BATCH": "32"}) == 32

    @pytest.mark.unit
    def test_oob_high_clamps_to_64(self) -> None:
        """Out-of-range high value clamps to 64.

        Sabotage: drop the ``min(64, value)`` clamp and an operator
        typo enables a 1000-text batch — large response payloads slow
        the dispatch loop and starve callers.
        """
        from kairix.paths import embed_coalesce_max_batch

        assert embed_coalesce_max_batch(env={"KAIRIX_EMBED_COALESCE_MAX_BATCH": "999"}) == 64

    @pytest.mark.unit
    def test_oob_low_clamps_to_one(self) -> None:
        """A zero or negative max-batch clamps to 1 (minimum useful batch).

        Sabotage: drop the ``max(1, ...)`` clamp and a 0 batch size
        means the dispatcher never wakes via the batch-full path.
        """
        from kairix.paths import embed_coalesce_max_batch

        assert embed_coalesce_max_batch(env={"KAIRIX_EMBED_COALESCE_MAX_BATCH": "0"}) == 1

    @pytest.mark.unit
    def test_invalid_falls_back_to_default(self) -> None:
        """Garbage → default. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_coalesce_max_batch

        assert embed_coalesce_max_batch(env={"KAIRIX_EMBED_COALESCE_MAX_BATCH": "nope"}) == 16


class TestTraceEnabled:
    """Round-trip tests for ``trace_enabled`` (Plan B-parity D4)."""

    @pytest.mark.unit
    def test_default_off(self) -> None:
        """Unset env → trace stays off."""
        from kairix.paths import trace_enabled

        assert trace_enabled(env={}) is False

    @pytest.mark.unit
    def test_one_turns_on(self) -> None:
        """``KAIRIX_TRACE=1`` opts in."""
        from kairix.paths import trace_enabled

        assert trace_enabled(env={"KAIRIX_TRACE": "1"}) is True

    @pytest.mark.unit
    def test_other_values_stay_off(self) -> None:
        """Only the literal ``1`` opts in — ``true``/``yes`` etc. stay off.

        Sabotage: relax to ``bool(value)`` and ``KAIRIX_TRACE=0`` would
        turn tracing ON. Pinning the literal-1 contract here.
        """
        from kairix.paths import trace_enabled

        assert trace_enabled(env={"KAIRIX_TRACE": "true"}) is False
        assert trace_enabled(env={"KAIRIX_TRACE": "0"}) is False


class TestWorkerWritesVecIndex:
    """Round-trip tests for ``worker_writes_vec_index`` (#335 OOM gate)."""

    @pytest.mark.unit
    def test_default_off(self) -> None:
        """Unset env → worker skips usearch writes (default safe)."""
        from kairix.paths import worker_writes_vec_index

        assert worker_writes_vec_index(env={}) is False

    @pytest.mark.unit
    def test_one_opts_in(self) -> None:
        """``KAIRIX_WORKER_WRITES_VEC_INDEX=1`` opts in to the legacy write path."""
        from kairix.paths import worker_writes_vec_index

        assert worker_writes_vec_index(env={"KAIRIX_WORKER_WRITES_VEC_INDEX": "1"}) is True

    @pytest.mark.unit
    def test_true_and_yes_also_opt_in(self) -> None:
        """Accept the conventional truthy spellings, case-insensitive."""
        from kairix.paths import worker_writes_vec_index

        for value in ("true", "True", "TRUE", "yes", "Yes"):
            env = {"KAIRIX_WORKER_WRITES_VEC_INDEX": value}
            assert worker_writes_vec_index(env=env) is True, f"expected True for {value!r}"

    @pytest.mark.unit
    def test_falsey_strings_stay_off(self) -> None:
        """``0`` / ``false`` / ``no`` / empty stay OFF.

        Sabotage: relax the truthy set to ``bool(value)`` and
        ``KAIRIX_WORKER_WRITES_VEC_INDEX=0`` would re-enable the OOM
        path. Pinning the explicit-truthy contract here.
        """
        from kairix.paths import worker_writes_vec_index

        for value in ("0", "false", "no", "", "off"):
            env = {"KAIRIX_WORKER_WRITES_VEC_INDEX": value}
            assert worker_writes_vec_index(env=env) is False, f"expected False for {value!r}"


class TestFeatureFlagOverride:
    """Round-trip tests for ``feature_flag_override`` (PR-2 feature-flag scaffold).

    Per ``docs/architecture/feature-flag-architecture.md`` §3.4 the
    env-var override is highest priority; ``feature_flag_override``
    returns True / False / None so the resolver can distinguish "unset"
    from "explicitly false".
    """

    @pytest.mark.unit
    def test_unset_returns_none(self) -> None:
        """No env var → ``None`` so the resolver falls through layers."""
        from kairix.paths import feature_flag_override

        assert feature_flag_override("canary", env={}) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on", "ON"])
    def test_truthy_values_return_true(self, truthy: str) -> None:
        """Documented truthy values resolve to ``True`` (case-insensitive)."""
        from kairix.paths import feature_flag_override

        assert feature_flag_override("canary", env={"KAIRIX_FEATURE_CANARY": truthy}) is True

    @pytest.mark.unit
    @pytest.mark.parametrize("falsy", ["0", "false", "False", "no", "off"])
    def test_falsy_values_return_false(self, falsy: str) -> None:
        """Documented falsy values resolve to ``False``."""
        from kairix.paths import feature_flag_override

        assert feature_flag_override("canary", env={"KAIRIX_FEATURE_CANARY": falsy}) is False

    @pytest.mark.unit
    def test_garbage_value_returns_none_with_warning(self, caplog) -> None:
        """A non-boolean string logs a warning and returns ``None``.

        Sabotage: remove the warning log → the operator's typo silently
        disables the override; this assertion fails.
        """
        from kairix.paths import feature_flag_override

        with caplog.at_level("WARNING"):
            result = feature_flag_override("canary", env={"KAIRIX_FEATURE_CANARY": "maybe"})
        assert result is None
        assert any("not a recognised boolean" in r.getMessage() for r in caplog.records)

    @pytest.mark.unit
    def test_uppercases_the_flag_name(self) -> None:
        """The env var name is ``KAIRIX_FEATURE_<UPPERCASE>`` regardless
        of the case the caller passes. Locks the spec §3.4 contract.
        """
        from kairix.paths import feature_flag_override

        env = {"KAIRIX_FEATURE_MY_FLAG": "1"}
        assert feature_flag_override("my_flag", env=env) is True
        assert feature_flag_override("My_Flag", env=env) is True


class TestFeatureFlagConfigOverlay:
    """Round-trip tests for ``feature_flag_config_overlay``.

    The middle layer of §3.4 — reads the ``features:`` section from
    ``kairix.config.yaml``. Returns an empty dict when the file or
    section is missing so the resolver falls back to the registry
    default.
    """

    @pytest.mark.unit
    def test_returns_empty_dict_when_config_missing(self, tmp_path) -> None:
        """No config file → empty dict."""
        from kairix.paths import feature_flag_config_overlay

        assert feature_flag_config_overlay(environ={"KAIRIX_CONFIG_PATH": str(tmp_path / "missing.yaml")}) == {}

    @pytest.mark.unit
    def test_reads_features_section(self, tmp_path) -> None:
        """``features: {flag_a: true, flag_b: false}`` parses round-trip."""
        from kairix.paths import feature_flag_config_overlay

        cfg = tmp_path / "kairix.config.yaml"
        cfg.write_text("features:\n  flag_a: true\n  flag_b: false\n", encoding="utf-8")
        overlay = feature_flag_config_overlay(environ={"KAIRIX_CONFIG_PATH": str(cfg)})
        assert overlay == {"flag_a": True, "flag_b": False}

    @pytest.mark.unit
    def test_returns_empty_dict_when_features_section_absent(self, tmp_path) -> None:
        """Config file exists but ``features:`` key missing → empty dict."""
        from kairix.paths import feature_flag_config_overlay

        cfg = tmp_path / "kairix.config.yaml"
        cfg.write_text("paths:\n  document_root: /tmp\n", encoding="utf-8")
        assert feature_flag_config_overlay(environ={"KAIRIX_CONFIG_PATH": str(cfg)}) == {}

    @pytest.mark.unit
    def test_malformed_yaml_returns_empty_dict(self, tmp_path) -> None:
        """Malformed YAML doesn't raise — gracefully falls back to empty."""
        from kairix.paths import feature_flag_config_overlay

        cfg = tmp_path / "kairix.config.yaml"
        cfg.write_text("features: this is not a dict\n  - oops\n", encoding="utf-8")
        assert feature_flag_config_overlay(environ={"KAIRIX_CONFIG_PATH": str(cfg)}) == {}

    @pytest.mark.unit
    def test_non_dict_features_section_returns_empty_dict(self, tmp_path) -> None:
        """``features: []`` (a list, not a dict) → empty overlay."""
        from kairix.paths import feature_flag_config_overlay

        cfg = tmp_path / "kairix.config.yaml"
        cfg.write_text("features: []\n", encoding="utf-8")
        assert feature_flag_config_overlay(environ={"KAIRIX_CONFIG_PATH": str(cfg)}) == {}


class TestAgentKnowledgeDirName:
    """`agent_knowledge_dir_name()` — config-driven directory override."""

    @pytest.mark.unit
    def test_default_when_config_missing(self) -> None:
        """No config → canonical ``04-Agent-Knowledge`` directory name."""
        from kairix.paths import agent_knowledge_dir_name

        assert agent_knowledge_dir_name(config={}) == "04-Agent-Knowledge"

    @pytest.mark.unit
    def test_override_via_config_kwarg(self) -> None:
        """Config seam returns the operator-supplied override verbatim."""
        from kairix.paths import agent_knowledge_dir_name

        assert agent_knowledge_dir_name(config={"agent_knowledge_dir": "team-memory"}) == "team-memory"

    @pytest.mark.unit
    def test_empty_string_falls_back_to_default(self) -> None:
        """An empty string is treated as 'unset' and falls back."""
        from kairix.paths import agent_knowledge_dir_name

        assert agent_knowledge_dir_name(config={"agent_knowledge_dir": ""}) == "04-Agent-Knowledge"


class TestAgentMemoryGlob:
    """`agent_memory_glob()` — config-driven glob override for the layout
    used by `kairix onboard check agent_knowledge_populated`."""

    @pytest.mark.unit
    def test_default_glob_is_broadest_pattern(self) -> None:
        """Default ``**/*.md`` matches any markdown anywhere under the tree —
        the broadest workable pattern so layout differences don't trip the
        healthcheck."""
        from kairix.paths import agent_memory_glob

        assert agent_memory_glob(config={}) == "**/*.md"

    @pytest.mark.unit
    def test_strict_per_agent_layout_override(self) -> None:
        """Operators with a stricter convention pin the per-agent ``memory/``
        subdir layout via ``paths.agent_memory_glob``."""
        from kairix.paths import agent_memory_glob

        assert agent_memory_glob(config={"agent_memory_glob": "*/memory/*.md"}) == "*/memory/*.md"


class TestBronzeTtlDays:
    """`bronze_ttl_days()` — #316 TTL for bronze raw blobs."""

    @pytest.mark.unit
    def test_default_is_seven_when_unset(self) -> None:
        from kairix.paths import bronze_ttl_days

        assert bronze_ttl_days(env={}) == 7

    @pytest.mark.unit
    def test_parses_integer_override(self) -> None:
        from kairix.paths import bronze_ttl_days

        assert bronze_ttl_days(env={"KAIRIX_BRONZE_TTL_DAYS": "14"}) == 14

    @pytest.mark.unit
    def test_non_integer_falls_back_to_default(self, caplog: pytest.LogCaptureFixture) -> None:
        from kairix.paths import bronze_ttl_days

        with caplog.at_level("WARNING"):
            assert bronze_ttl_days(env={"KAIRIX_BRONZE_TTL_DAYS": "not-an-int"}) == 7
        assert any("not an int" in r.getMessage() for r in caplog.records)

    @pytest.mark.unit
    def test_negative_falls_back_to_default(self, caplog: pytest.LogCaptureFixture) -> None:
        from kairix.paths import bronze_ttl_days

        with caplog.at_level("WARNING"):
            assert bronze_ttl_days(env={"KAIRIX_BRONZE_TTL_DAYS": "-1"}) == 7
        assert any("negative" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# embedding_cache_path() — closes #426
# ---------------------------------------------------------------------------
#
# Pre-fix: embedding_cache_path() resolved to <document_root>/.kairix/cache/
# which on production = the operator's synced Obsidian vault. The cache file
# grew to 8.7 GB and was dragged into vault sync. The fix routes the default
# through cache_dir() so the cache lands in a kairix-controlled writable dir
# (/var/cache/kairix on FHS, $XDG_CACHE_HOME/kairix on user installs).
#
# Sabotage proof: reverting embedding_cache_path's default branch to
# `document_root() / ".kairix" / "cache" / "embedding_cache.sqlite"` makes
# test_default_does_not_land_under_document_root fail with the path
# containing the document_root prefix.


@pytest.mark.unit
class TestEmbeddingCachePath:
    @pytest.mark.unit
    def test_default_resolves_through_cache_dir(self) -> None:
        """The mode=None default uses default_cache_dir() — not document_root.

        default_cache_dir honours KAIRIX_CACHE_DIR env first, so an operator
        override (e.g. /var/cache/kairix on FHS) wins over platform defaults.
        """
        from kairix.paths import default_cache_dir, embedding_cache_path

        env = {"KAIRIX_CACHE_DIR": "/tmp/test-cache"}

        result = embedding_cache_path(env=env)
        assert result == default_cache_dir(env=env) / "embedding_cache.sqlite"
        assert result == Path("/tmp/test-cache/embedding_cache.sqlite")

    @pytest.mark.unit
    def test_default_does_not_land_under_document_root(self) -> None:
        """Regression for #426 — cache must NOT land under the operator's
        document_root (which is typically a synced knowledge store)."""
        from kairix.paths import embedding_cache_path

        env = {"KAIRIX_DOCUMENT_ROOT": "/data/obsidian-vault", "KAIRIX_CACHE_DIR": "/var/cache/kairix"}

        result = embedding_cache_path(env=env)
        assert "/data/obsidian-vault" not in str(result)
        assert "/var/cache/kairix" in str(result)

    @pytest.mark.unit
    def test_explicit_mode_uses_data_dir_per_mode(self) -> None:
        """The explicit-mode branch (installer + contract test surface)
        resolves to data_dir(mode)/cache/embedding_cache.sqlite. Pinning
        this so the fix to the mode=None branch doesn't accidentally
        change the mode-explicit behaviour."""
        from kairix.paths import Mode, data_dir, embedding_cache_path

        result_system = embedding_cache_path(Mode.system)
        assert result_system == data_dir(Mode.system) / "cache" / "embedding_cache.sqlite"


@pytest.mark.unit
class TestRechunkSweepPerTickCap:
    """ADR-028 Wave F.4 — KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP env resolution."""

    def test_default_when_unset(self) -> None:
        assert rechunk_sweep_per_tick_cap(env={}) == 200

    def test_valid_positive_int_from_env(self) -> None:
        assert rechunk_sweep_per_tick_cap(env={"KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP": "50"}) == 50

    def test_non_int_falls_back_to_default(self) -> None:
        assert rechunk_sweep_per_tick_cap(env={"KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP": "not-a-number"}) == 200

    def test_non_positive_falls_back_to_default(self) -> None:
        assert rechunk_sweep_per_tick_cap(env={"KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP": "0"}) == 200
        assert rechunk_sweep_per_tick_cap(env={"KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP": "-5"}) == 200


# ---------------------------------------------------------------------------
# briefing_dir() (PLA-267)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBriefingDir:
    """``briefing_dir`` is paths-routed, lazy, and HOME-free on FHS deploys.

    The retired writer evaluated ``Path.home()`` at module import, which
    crashed the briefing import on a hardened no-HOME VM. These tests drive
    the deployment shape by INJECTING the mode / an explicit env mapping —
    never by clearing HOME (F2) — and prove the resolver routes through the
    per-mode cache dir.
    """

    @pytest.mark.unit
    def test_container_and_system_modes_are_home_free(self) -> None:
        """A hardened no-HOME container/system install resolves to the FHS
        cache path with no ``Path.home()`` / ``expanduser`` — so importing +
        writing a brief never depends on HOME being set."""
        assert briefing_dir(Mode.container) == Path("/var/cache/kairix/briefing")
        assert briefing_dir(Mode.system) == Path("/var/cache/kairix/briefing")

    @pytest.mark.unit
    def test_user_mode_routes_through_xdg_cache(self, monkeypatch) -> None:
        """User-mode sits under the XDG cache root (XDG_CACHE_HOME is a POSIX
        spec var, not a kairix internal — F2/F4-clean)."""
        monkeypatch.setenv("XDG_CACHE_HOME", "/custom/xdg-cache")
        assert briefing_dir(Mode.user) == Path("/custom/xdg-cache/kairix/briefing")

    @pytest.mark.unit
    def test_env_override_wins_on_runtime_path(self) -> None:
        """``KAIRIX_BRIEFING_DIR`` overrides the default — exercised through the
        F2-clean ``environ=`` seam (no process-env mutation)."""
        result = briefing_dir(environ={"KAIRIX_BRIEFING_DIR": "/srv/briefs"})
        assert result == Path("/srv/briefs")

    @pytest.mark.unit
    def test_runtime_default_is_under_briefing_subdir(self) -> None:
        """With no override, the runtime path is the auto-detected cache dir
        with a ``briefing`` leaf."""
        result = briefing_dir(environ={})
        assert result.name == "briefing"


@pytest.mark.unit
class TestPathConfinement:
    """The canonical allow-list sanitiser (S2083 / S8707 confinement).

    Lives here (same-module as ``kairix/paths.py``) so mutation-parity always
    runs it against ``confine_to`` / ``confine_to_roots`` mutants — a widely
    imported module's own tests are guaranteed in-window where a topical test
    module would be evicted by the impacted-test cap.
    """

    @pytest.mark.unit
    def test_confine_to_accepts_relative_inside_root(self, tmp_path: Path) -> None:
        (tmp_path / "suites").mkdir()
        resolved = confine_to(tmp_path, "suites/canary.yaml")
        assert resolved == (tmp_path / "suites" / "canary.yaml").resolve()

    @pytest.mark.unit
    def test_confine_to_rejects_dotdot_escape(self, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError) as excinfo:
            confine_to(tmp_path, "../../../etc/passwd")
        assert "escapes allowed root" in str(excinfo.value)

    @pytest.mark.unit
    def test_confine_to_roots_accepts_path_inside_a_root(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        target.write_text("{}", encoding="utf-8")
        # Pins the ``root in resolved.parents`` arm (killed by the or->and mutant).
        assert confine_to_roots(target, [tmp_path]) == target.resolve()

    @pytest.mark.unit
    def test_confine_to_roots_accepts_the_root_itself(self, tmp_path: Path) -> None:
        # Pins the ``resolved == root`` arm: a dir is not in its own .parents,
        # so without the equality check this case is wrongly rejected
        # (kills the ==->!= mutant).
        assert confine_to_roots(tmp_path, [tmp_path]) == tmp_path.resolve()

    @pytest.mark.unit
    def test_confine_to_roots_rejects_dotdot_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError) as excinfo:
            confine_to_roots(tmp_path / ".." / ".." / "etc" / "passwd", [tmp_path])
        assert "escapes the allowed roots" in str(excinfo.value)

    @pytest.mark.unit
    def test_confine_to_roots_rejects_absolute_path_outside_every_root(self, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError):
            confine_to_roots("/etc/passwd", [tmp_path])

    @pytest.mark.unit
    def test_agent_cli_roots_admits_tempdir_and_extra_roots(self, tmp_path: Path) -> None:
        # tmp_path lives under the system temp dir → admitted by the defaults.
        probe = tmp_path / "probe.txt"
        probe.write_text("ok", encoding="utf-8")
        assert confine_to_roots(probe, agent_cli_roots()) == probe.resolve()
        # An extra root widens the allow-list for a surface with its own base.
        nested = tmp_path / "vault" / "deep" / "f.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("x", encoding="utf-8")
        assert confine_to_roots(nested, agent_cli_roots(tmp_path / "vault")) == nested.resolve()
