"""Tests for kairix.paths — centralised path resolution."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kairix.paths import (
    KairixPaths,
    bundled_suites_root,
    clear_cache,
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
    def test_document_root_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/custom/vault")
        paths = KairixPaths.resolve()
        assert paths.document_root == Path("/custom/vault")

    @pytest.mark.unit
    def test_db_path_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DB_PATH", "/custom/db/index.sqlite")
        paths = KairixPaths.resolve()
        assert paths.db_path == Path("/custom/db/index.sqlite")

    @pytest.mark.unit
    def test_log_dir_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_LOG_DIR", "/custom/logs")
        paths = KairixPaths.resolve()
        assert paths.log_dir == Path("/custom/logs")

    @pytest.mark.unit
    def test_workspace_root_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_WORKSPACE_ROOT", "/custom/workspaces")
        paths = KairixPaths.resolve()
        assert paths.workspace_root == Path("/custom/workspaces")

    @pytest.mark.unit
    def test_defaults_not_data_paths(self, monkeypatch) -> None:
        """Default paths should not contain /data/ (TC-specific)."""
        monkeypatch.delenv("KAIRIX_DOCUMENT_ROOT", raising=False)
        monkeypatch.delenv("KAIRIX_DB_PATH", raising=False)
        monkeypatch.delenv("KAIRIX_LOG_DIR", raising=False)
        monkeypatch.delenv("KAIRIX_WORKSPACE_ROOT", raising=False)
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        paths = KairixPaths.resolve()
        assert "/data/" not in str(paths.document_root)
        assert "/data/" not in str(paths.db_path)

    @pytest.mark.unit
    def test_docker_detection_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        monkeypatch.delenv("KAIRIX_DOCUMENT_ROOT", raising=False)
        monkeypatch.delenv("KAIRIX_DB_PATH", raising=False)
        paths = KairixPaths.resolve()
        assert str(paths.document_root) == "/data/documents"

    @pytest.mark.unit
    def test_clear_cache_allows_re_resolution(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/first")
        paths1 = KairixPaths.resolve()
        clear_cache()
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/second")
        paths2 = KairixPaths.resolve()
        assert paths1.document_root != paths2.document_root

    @pytest.mark.unit
    def test_tilde_expansion(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "~/my-vault")
        paths = KairixPaths.resolve()
        assert "~" not in str(paths.document_root)
        assert str(paths.document_root).endswith("/my-vault")


@pytest.mark.unit
class TestDocumentRootEnvVar:
    @pytest.mark.unit
    def test_document_root_from_env(self, monkeypatch, tmp_path):
        from kairix.paths import clear_cache

        clear_cache()
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path))
        result = document_root()
        assert result == tmp_path
        clear_cache()

    @pytest.mark.unit
    def test_document_root_default_when_unset(self, monkeypatch):
        from kairix.paths import clear_cache

        clear_cache()
        monkeypatch.delenv("KAIRIX_DOCUMENT_ROOT", raising=False)
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        result = document_root()
        assert result == Path.home() / "Documents"
        clear_cache()


# ---------------------------------------------------------------------------
# is_docker_runtime_check() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDocker:
    @pytest.mark.unit
    def test_dockerenv_file_present(self, monkeypatch) -> None:
        """/.dockerenv existing should trigger docker detection."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("os.path.exists", return_value=True):
            assert is_docker_runtime_check() is True

    @pytest.mark.unit
    def test_kairix_docker_env_var(self, monkeypatch) -> None:
        """KAIRIX_DOCKER=1 should trigger docker detection."""
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check() is True

    @pytest.mark.unit
    def test_container_env_var(self, monkeypatch) -> None:
        """Non-empty 'container' env var should trigger docker detection."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.setenv("container", "podman")
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check() is True

    @pytest.mark.unit
    def test_not_docker_when_nothing_set(self, monkeypatch) -> None:
        """Should return False when no docker indicators present."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("os.path.exists", return_value=False):
            assert is_docker_runtime_check() is False


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
    def test_docker_returns_data_kairix(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        monkeypatch.delenv("container", raising=False)
        result = default_data_dir()
        assert result == Path("/data/kairix")

    @pytest.mark.unit
    def test_xdg_data_home(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        with patch("os.path.exists", return_value=False):
            with patch.object(Path, "exists", return_value=False):
                result = default_data_dir()
        assert result == Path("/custom/data/kairix")

    @pytest.mark.unit
    def test_windows_localappdata(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_data_dir(platform="win32")
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix"

    @pytest.mark.unit
    def test_default_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_data_dir()
        assert result == Path.home() / ".local" / "share" / "kairix"


# ---------------------------------------------------------------------------
# default_cache_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultCacheDir:
    @pytest.mark.unit
    def test_docker_returns_data_kairix(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        monkeypatch.delenv("container", raising=False)
        result = default_cache_dir()
        assert result == Path("/data/kairix")

    @pytest.mark.unit
    def test_xdg_cache_home(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", "/custom/cache")
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir()
        assert result == Path("/custom/cache/kairix")

    @pytest.mark.unit
    def test_windows_localappdata_cache(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir(platform="win32")
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix" / "cache"

    @pytest.mark.unit
    def test_default_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = default_cache_dir()
        assert result == Path.home() / ".cache" / "kairix"


# ---------------------------------------------------------------------------
# load_paths_from_config() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadPathsFromConfig:
    @pytest.mark.unit
    def test_returns_empty_when_no_config(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
        result = load_paths_from_config()
        assert result == {}

    @pytest.mark.integration
    def test_loads_paths_from_yaml(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("paths:\n  document_root: /from/config\n  db_path: /from/config/db.sqlite\n")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = load_paths_from_config()
        assert result.get("document_root") == "/from/config"
        assert result.get("db_path") == "/from/config/db.sqlite"

    @pytest.mark.integration
    def test_graceful_fallback_on_malformed_yaml(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = load_paths_from_config()
        # Should return {} rather than raising
        assert isinstance(result, dict)

    @pytest.mark.integration
    def test_returns_empty_when_no_paths_section(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("logging:\n  level: DEBUG\n")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = load_paths_from_config()
        assert result == {}


# ---------------------------------------------------------------------------
# clear_cache() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClearCache:
    @pytest.mark.unit
    def test_clear_cache_invalidates(self, monkeypatch) -> None:
        """Calling clear_cache() should allow re-resolution with new env vars."""
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/alpha")
        p1 = KairixPaths.resolve()
        clear_cache()
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/beta")
        p2 = KairixPaths.resolve()
        assert p1.document_root == Path("/alpha")
        assert p2.document_root == Path("/beta")


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
    def test_service_install_document_root(self, monkeypatch) -> None:
        """When /opt/kairix/.venv exists and Docker is not, doc root is /var/lib/kairix/documents."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with (
            patch("os.path.exists", return_value=False),  # not Docker
            patch.object(Path, "exists", return_value=True),  # /opt/kairix/.venv present
        ):
            assert default_document_root() == Path("/var/lib/kairix/documents")

    @pytest.mark.unit
    def test_service_install_data_dir(self, monkeypatch) -> None:
        """Service install → data dir is /var/lib/kairix."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_data_dir() == Path("/var/lib/kairix")

    @pytest.mark.unit
    def test_service_install_cache_dir(self, monkeypatch) -> None:
        """Service install → cache dir is /var/cache/kairix (NOT /var/lib/kairix)."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_cache_dir() == Path("/var/cache/kairix")

    @pytest.mark.unit
    def test_service_install_workspace_root(self, monkeypatch) -> None:
        """Service install → workspaces under /data/workspaces (same as Docker)."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=True),
        ):
            assert default_workspace_root() == Path("/data/workspaces")

    @pytest.mark.unit
    def test_workspace_root_user_default(self, monkeypatch) -> None:
        """Neither Docker nor service install → workspaces under ~/.kairix/workspaces."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with (
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            assert default_workspace_root() == Path.home() / ".kairix" / "workspaces"


# ---------------------------------------------------------------------------
# reference_library_root / bundled_suites_root — env-var-driven shipping
# locations (uncovered because no test currently calls them).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShippedAssetPaths:
    @pytest.mark.unit
    def test_reference_library_root_default(self, monkeypatch) -> None:
        """Without KAIRIX_REFLIB_ROOT set, returns the in-container default."""
        monkeypatch.delenv("KAIRIX_REFLIB_ROOT", raising=False)
        assert reference_library_root() == Path("reference-library")

    @pytest.mark.unit
    def test_reference_library_root_env_override(self, monkeypatch) -> None:
        """KAIRIX_REFLIB_ROOT overrides the default."""
        monkeypatch.setenv("KAIRIX_REFLIB_ROOT", "/custom/reflib")
        assert reference_library_root() == Path("/custom/reflib")

    @pytest.mark.unit
    def test_bundled_suites_root_env_override(self, monkeypatch) -> None:
        """KAIRIX_SUITES_ROOT overrides every other lookup (step 1 of #268 resolution)."""
        monkeypatch.setenv("KAIRIX_SUITES_ROOT", "/custom/suites")
        assert bundled_suites_root() == Path("/custom/suites")


# ---------------------------------------------------------------------------
# log_dir() convenience wrapper — uncovered because tests above call
# KairixPaths.resolve() directly. Exercise the function itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogDirWrapper:
    @pytest.mark.unit
    def test_log_dir_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_LOG_DIR", "/custom/logs")
        assert log_dir() == Path("/custom/logs")


# ---------------------------------------------------------------------------
# maintenance_skip_noop_threshold — three branches: unset (default 10),
# valid int, invalid string (logs warning + falls back to 10).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMaintenanceSkipNoopThreshold:
    @pytest.mark.unit
    def test_unset_returns_default_10(self, monkeypatch) -> None:
        """Without the env var, threshold falls back to 10."""
        monkeypatch.delenv("KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD", raising=False)
        assert maintenance_skip_noop_threshold() == 10

    @pytest.mark.unit
    def test_valid_int_override(self, monkeypatch) -> None:
        """A valid integer string is parsed and returned."""
        monkeypatch.setenv("KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD", "42")
        assert maintenance_skip_noop_threshold() == 42

    @pytest.mark.unit
    def test_invalid_falls_back_to_10_and_warns(self, monkeypatch, caplog) -> None:
        """An unparseable value logs a warning and falls back to 10."""
        import logging

        monkeypatch.setenv("KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD", "not-an-int")
        with caplog.at_level(logging.WARNING, logger="kairix.paths"):
            assert maintenance_skip_noop_threshold() == 10
        assert any("not an int" in rec.message for rec in caplog.records), (
            "expected a warning about the invalid int value"
        )


# ---------------------------------------------------------------------------
# entity_overrides_path — #166: vault-driven entity-overrides file location.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEntityOverridesPath:
    @pytest.mark.unit
    def test_explicit_env_override_wins(self, monkeypatch, tmp_path) -> None:
        """``KAIRIX_ENTITY_OVERRIDES_PATH`` takes precedence over the default."""
        from kairix.paths import entity_overrides_path

        custom = tmp_path / "custom-overrides.md"
        monkeypatch.setenv("KAIRIX_ENTITY_OVERRIDES_PATH", str(custom))
        assert entity_overrides_path() == custom

    @pytest.mark.unit
    def test_default_lives_under_document_root(self, monkeypatch, tmp_path) -> None:
        """Without the override env var, the path sits under
        ``{document_root}/04-Agent-Knowledge/_entity-overrides.md``."""
        from kairix.paths import entity_overrides_path

        monkeypatch.delenv("KAIRIX_ENTITY_OVERRIDES_PATH", raising=False)
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path))
        assert entity_overrides_path() == tmp_path / "04-Agent-Knowledge" / "_entity-overrides.md"

    @pytest.mark.unit
    def test_explicit_env_expands_user(self, monkeypatch) -> None:
        """A ``~``-prefixed override path is expanded to the home directory."""
        from kairix.paths import entity_overrides_path

        monkeypatch.setenv("KAIRIX_ENTITY_OVERRIDES_PATH", "~/overrides.md")
        result = entity_overrides_path()
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
    def test_returns_int_when_set_valid(self, monkeypatch) -> None:
        """Set env → parsed int. Sabotage: replace return int(raw) with default → fails."""
        from kairix.paths import read_int_env

        monkeypatch.setenv("KAIRIX_TEST_INT", "42")
        assert read_int_env("KAIRIX_TEST_INT", default=10) == 42

    @pytest.mark.unit
    def test_returns_default_when_unset(self, monkeypatch) -> None:
        """Unset env → default. Sabotage: drop the None early-return → int(None) raises."""
        from kairix.paths import read_int_env

        monkeypatch.delenv("KAIRIX_TEST_INT", raising=False)
        assert read_int_env("KAIRIX_TEST_INT", default=7) == 7

    @pytest.mark.unit
    def test_returns_default_when_invalid(self, monkeypatch) -> None:
        """Garbage → default + warning. Sabotage: remove try/except → int('abc') crashes."""
        from kairix.paths import read_int_env

        monkeypatch.setenv("KAIRIX_TEST_INT", "not-an-int")
        assert read_int_env("KAIRIX_TEST_INT", default=99) == 99


class TestReadFloatEnv:
    @pytest.mark.unit
    def test_returns_float_when_set_valid(self, monkeypatch) -> None:
        """Set env → parsed float. Sabotage: change return to default."""
        from kairix.paths import read_float_env

        monkeypatch.setenv("KAIRIX_TEST_FLOAT", "3.5")
        assert read_float_env("KAIRIX_TEST_FLOAT", default=1.0) == 3.5

    @pytest.mark.unit
    def test_returns_default_when_unset(self, monkeypatch) -> None:
        """Unset → default. Sabotage: drop None early-return → float(None) raises."""
        from kairix.paths import read_float_env

        monkeypatch.delenv("KAIRIX_TEST_FLOAT", raising=False)
        assert read_float_env("KAIRIX_TEST_FLOAT", default=2.5) == 2.5

    @pytest.mark.unit
    def test_returns_default_when_invalid(self, monkeypatch) -> None:
        """Garbage → fallback. Sabotage: remove try/except → ValueError on float('xyz')."""
        from kairix.paths import read_float_env

        monkeypatch.setenv("KAIRIX_TEST_FLOAT", "xyz")
        assert read_float_env("KAIRIX_TEST_FLOAT", default=0.5) == 0.5


class TestEmbedPoolKeepalive:
    @pytest.mark.unit
    def test_valid_env_returns_set_value(self, monkeypatch) -> None:
        """KAIRIX_EMBED_POOL_KEEPALIVE=25 → 25. Sabotage: ignore env → default."""
        from kairix.paths import embed_pool_keepalive

        monkeypatch.setenv("KAIRIX_EMBED_POOL_KEEPALIVE", "25")
        assert embed_pool_keepalive(10) == 25

    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        """Garbage → default + warning. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_pool_keepalive

        monkeypatch.setenv("KAIRIX_EMBED_POOL_KEEPALIVE", "bad")
        assert embed_pool_keepalive(10) == 10


class TestEmbedPoolExpiry:
    @pytest.mark.unit
    def test_valid_env_returns_set_value(self, monkeypatch) -> None:
        """KAIRIX_EMBED_POOL_EXPIRY_S=45.5 → 45.5. Sabotage: ignore env → default."""
        from kairix.paths import embed_pool_expiry_s

        monkeypatch.setenv("KAIRIX_EMBED_POOL_EXPIRY_S", "45.5")
        assert embed_pool_expiry_s(30.0) == 45.5

    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        """Garbage → default. Sabotage: remove try/except → float() raises."""
        from kairix.paths import embed_pool_expiry_s

        monkeypatch.setenv("KAIRIX_EMBED_POOL_EXPIRY_S", "nope")
        assert embed_pool_expiry_s(30.0) == 30.0


class TestEmbedVectorDimsFallback:
    @pytest.mark.unit
    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        """KAIRIX_EMBED_DIMS=abc → default + warning. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_vector_dims

        monkeypatch.setenv("KAIRIX_EMBED_DIMS", "abc")
        assert embed_vector_dims(default=1536) == 1536


class TestEmbedCoalesceWindowMs:
    """Round-trip tests for ``embed_coalesce_window_ms`` (#288).

    F2-clean here because this file is the baselined home for
    kairix.paths env-var round-trip tests — env IS the public boundary
    we're verifying.
    """

    @pytest.mark.unit
    def test_default_when_unset(self, monkeypatch) -> None:
        """Unset env → documented default 50.

        Sabotage: change the default in ``embed_coalesce_window_ms``
        from 50 to e.g. 5 and the documented behaviour drifts away
        from the actual fall-back.
        """
        from kairix.paths import embed_coalesce_window_ms

        monkeypatch.delenv("KAIRIX_EMBED_COALESCE_WINDOW_MS", raising=False)
        assert embed_coalesce_window_ms() == 50

    @pytest.mark.unit
    def test_valid_int_passes_through(self, monkeypatch) -> None:
        """An in-range value comes back unchanged.

        Sabotage: hard-code the return value and the operator's
        configured 100ms window is silently ignored.
        """
        from kairix.paths import embed_coalesce_window_ms

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_WINDOW_MS", "100")
        assert embed_coalesce_window_ms() == 100

    @pytest.mark.unit
    def test_oob_high_clamps_to_500(self, monkeypatch) -> None:
        """Out-of-range high value clamps to the documented upper bound.

        Sabotage: drop the ``min(500, value)`` clamp and an operator
        typo (e.g. 99999) silently sets the window to 99 seconds —
        every embed call appears to hang.
        """
        from kairix.paths import embed_coalesce_window_ms

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_WINDOW_MS", "99999")
        assert embed_coalesce_window_ms() == 500

    @pytest.mark.unit
    def test_oob_low_clamps_to_zero(self, monkeypatch) -> None:
        """Negative value clamps to 0 (which is the documented "disable" mode).

        Sabotage: drop the ``max(0, ...)`` clamp and a negative window
        causes Condition.wait to fire instantly — defeats coalescing.
        """
        from kairix.paths import embed_coalesce_window_ms

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_WINDOW_MS", "-100")
        assert embed_coalesce_window_ms() == 0

    @pytest.mark.unit
    def test_invalid_falls_back_to_default(self, monkeypatch) -> None:
        """Garbage → default. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_coalesce_window_ms

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_WINDOW_MS", "nope")
        assert embed_coalesce_window_ms() == 50


class TestEmbedCoalesceMaxBatch:
    """Round-trip tests for ``embed_coalesce_max_batch`` (#288)."""

    @pytest.mark.unit
    def test_default_when_unset(self, monkeypatch) -> None:
        """Unset env → documented default 16."""
        from kairix.paths import embed_coalesce_max_batch

        monkeypatch.delenv("KAIRIX_EMBED_COALESCE_MAX_BATCH", raising=False)
        assert embed_coalesce_max_batch() == 16

    @pytest.mark.unit
    def test_valid_int_passes_through(self, monkeypatch) -> None:
        """In-range value comes back unchanged."""
        from kairix.paths import embed_coalesce_max_batch

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_MAX_BATCH", "32")
        assert embed_coalesce_max_batch() == 32

    @pytest.mark.unit
    def test_oob_high_clamps_to_64(self, monkeypatch) -> None:
        """Out-of-range high value clamps to 64.

        Sabotage: drop the ``min(64, value)`` clamp and an operator
        typo enables a 1000-text batch — large response payloads slow
        the dispatch loop and starve callers.
        """
        from kairix.paths import embed_coalesce_max_batch

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_MAX_BATCH", "999")
        assert embed_coalesce_max_batch() == 64

    @pytest.mark.unit
    def test_oob_low_clamps_to_one(self, monkeypatch) -> None:
        """A zero or negative max-batch clamps to 1 (minimum useful batch).

        Sabotage: drop the ``max(1, ...)`` clamp and a 0 batch size
        means the dispatcher never wakes via the batch-full path.
        """
        from kairix.paths import embed_coalesce_max_batch

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_MAX_BATCH", "0")
        assert embed_coalesce_max_batch() == 1

    @pytest.mark.unit
    def test_invalid_falls_back_to_default(self, monkeypatch) -> None:
        """Garbage → default. Sabotage: remove try/except → int() raises."""
        from kairix.paths import embed_coalesce_max_batch

        monkeypatch.setenv("KAIRIX_EMBED_COALESCE_MAX_BATCH", "nope")
        assert embed_coalesce_max_batch() == 16
