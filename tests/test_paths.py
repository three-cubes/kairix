"""Tests for kairix.paths — centralised path resolution."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kairix.paths import (
    KairixPaths,
    _default_cache_dir,
    _default_data_dir,
    _is_docker,
    _is_service_install,
    _load_paths_from_config,
    clear_cache,
    document_root,
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
# _is_docker() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDocker:
    @pytest.mark.unit
    def test_dockerenv_file_present(self, monkeypatch) -> None:
        """/.dockerenv existing should trigger docker detection."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("os.path.exists", return_value=True):
            assert _is_docker() is True

    @pytest.mark.unit
    def test_kairix_docker_env_var(self, monkeypatch) -> None:
        """KAIRIX_DOCKER=1 should trigger docker detection."""
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        with patch("os.path.exists", return_value=False):
            assert _is_docker() is True

    @pytest.mark.unit
    def test_container_env_var(self, monkeypatch) -> None:
        """Non-empty 'container' env var should trigger docker detection."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.setenv("container", "podman")
        with patch("os.path.exists", return_value=False):
            assert _is_docker() is True

    @pytest.mark.unit
    def test_not_docker_when_nothing_set(self, monkeypatch) -> None:
        """Should return False when no docker indicators present."""
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("os.path.exists", return_value=False):
            assert _is_docker() is False


# ---------------------------------------------------------------------------
# _is_service_install() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsServiceInstall:
    @pytest.mark.unit
    def test_service_install_when_venv_exists(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            assert _is_service_install() is True

    @pytest.mark.unit
    def test_not_service_install_when_no_venv(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            assert _is_service_install() is False


# ---------------------------------------------------------------------------
# _default_data_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultDataDir:
    @pytest.mark.unit
    def test_docker_returns_data_kairix(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        monkeypatch.delenv("container", raising=False)
        result = _default_data_dir()
        assert result == Path("/data/kairix")

    @pytest.mark.unit
    def test_xdg_data_home(self, monkeypatch) -> None:
        monkeypatch.delenv("KAIRIX_DOCKER", raising=False)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        with patch("os.path.exists", return_value=False):
            with patch.object(Path, "exists", return_value=False):
                result = _default_data_dir()
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
            patch("kairix.paths.sys") as mock_sys,
        ):
            mock_sys.platform = "win32"
            result = _default_data_dir()
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
            result = _default_data_dir()
        assert result == Path.home() / ".local" / "share" / "kairix"


# ---------------------------------------------------------------------------
# _default_cache_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultCacheDir:
    @pytest.mark.unit
    def test_docker_returns_data_kairix(self, monkeypatch) -> None:
        monkeypatch.setenv("KAIRIX_DOCKER", "1")
        monkeypatch.delenv("container", raising=False)
        result = _default_cache_dir()
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
            result = _default_cache_dir()
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
            patch("kairix.paths.sys") as mock_sys,
        ):
            mock_sys.platform = "win32"
            result = _default_cache_dir()
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
            result = _default_cache_dir()
        assert result == Path.home() / ".cache" / "kairix"


# ---------------------------------------------------------------------------
# _load_paths_from_config() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadPathsFromConfig:
    @pytest.mark.unit
    def test_returns_empty_when_no_config(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
        result = _load_paths_from_config()
        assert result == {}

    @pytest.mark.integration
    def test_loads_paths_from_yaml(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("paths:\n  document_root: /from/config\n  db_path: /from/config/db.sqlite\n")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = _load_paths_from_config()
        assert result.get("document_root") == "/from/config"
        assert result.get("db_path") == "/from/config/db.sqlite"

    @pytest.mark.integration
    def test_graceful_fallback_on_malformed_yaml(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = _load_paths_from_config()
        # Should return {} rather than raising
        assert isinstance(result, dict)

    @pytest.mark.integration
    def test_returns_empty_when_no_paths_section(self, monkeypatch, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("logging:\n  level: DEBUG\n")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = _load_paths_from_config()
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
