"""Tests for kairix.paths — centralised path resolution.

All tests pass an explicit ``env={...}`` mapping (and where relevant
``is_docker=`` / ``is_service_install=`` / ``platform=`` / ``dockerenv_exists=``
flags) to the helpers rather than mutating ``os.environ``. The lru_cache
on ``KairixPaths.resolve()`` only fires when ``env=None``; tests bypass
it by passing env explicitly.
"""

from pathlib import Path

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
    """Clear the no-arg path cache before/after each test so cached values
    against ``os.environ`` don't leak across tests."""
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
        """Default paths should not contain /data/ unless Docker is detected."""
        paths = KairixPaths.resolve(env={})
        assert "/data/" not in str(paths.document_root)
        assert "/data/" not in str(paths.db_path)

    @pytest.mark.unit
    def test_docker_detection_via_env(self) -> None:
        paths = KairixPaths.resolve(env={"KAIRIX_DOCKER": "1"})
        assert str(paths.document_root) == "/data/documents"

    @pytest.mark.unit
    def test_explicit_env_per_call_returns_independent_paths(self) -> None:
        """Two resolve() calls with different env mappings return different
        results — env= is honoured per-call, not pinned to the cache."""
        paths1 = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "/first"})
        paths2 = KairixPaths.resolve(env={"KAIRIX_DOCUMENT_ROOT": "/second"})
        assert paths1.document_root != paths2.document_root
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
# _is_docker() tests — pass env + dockerenv_exists kwargs explicitly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDocker:
    @pytest.mark.unit
    def test_dockerenv_file_present(self) -> None:
        """/.dockerenv existing should trigger docker detection."""
        assert _is_docker(env={}, dockerenv_exists=True) is True

    @pytest.mark.unit
    def test_kairix_docker_env_var(self) -> None:
        """KAIRIX_DOCKER=1 should trigger docker detection."""
        assert _is_docker(env={"KAIRIX_DOCKER": "1"}, dockerenv_exists=False) is True

    @pytest.mark.unit
    def test_container_env_var(self) -> None:
        """Non-empty 'container' env var should trigger docker detection."""
        assert _is_docker(env={"container": "podman"}, dockerenv_exists=False) is True

    @pytest.mark.unit
    def test_not_docker_when_nothing_set(self) -> None:
        """Should return False when no docker indicators present."""
        assert _is_docker(env={}, dockerenv_exists=False) is False


# ---------------------------------------------------------------------------
# _is_service_install() tests — pass venv_exists kwarg explicitly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsServiceInstall:
    @pytest.mark.unit
    def test_service_install_when_venv_exists(self) -> None:
        assert _is_service_install(venv_exists=True) is True

    @pytest.mark.unit
    def test_not_service_install_when_no_venv(self) -> None:
        assert _is_service_install(venv_exists=False) is False


# ---------------------------------------------------------------------------
# _default_data_dir() tests — pass env + is_docker + is_service_install +
# platform explicitly so the test exercises one branch deterministically.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultDataDir:
    @pytest.mark.unit
    def test_docker_returns_data_kairix(self) -> None:
        result = _default_data_dir(is_docker=True, is_service_install=False, platform="linux", env={})
        assert result == Path("/data/kairix")

    @pytest.mark.unit
    def test_xdg_data_home(self) -> None:
        result = _default_data_dir(
            env={"XDG_DATA_HOME": "/custom/data"},
            is_docker=False,
            is_service_install=False,
            platform="linux",
        )
        assert result == Path("/custom/data/kairix")

    @pytest.mark.unit
    def test_windows_localappdata(self) -> None:
        result = _default_data_dir(
            env={"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"},
            is_docker=False,
            is_service_install=False,
            platform="win32",
        )
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix"

    @pytest.mark.unit
    def test_default_fallback(self) -> None:
        result = _default_data_dir(env={}, is_docker=False, is_service_install=False, platform="linux")
        assert result == Path.home() / ".local" / "share" / "kairix"


# ---------------------------------------------------------------------------
# _default_cache_dir() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultCacheDir:
    @pytest.mark.unit
    def test_docker_returns_data_kairix(self) -> None:
        result = _default_cache_dir(is_docker=True, is_service_install=False, platform="linux", env={})
        assert result == Path("/data/kairix")

    @pytest.mark.unit
    def test_xdg_cache_home(self) -> None:
        result = _default_cache_dir(
            env={"XDG_CACHE_HOME": "/custom/cache"},
            is_docker=False,
            is_service_install=False,
            platform="linux",
        )
        assert result == Path("/custom/cache/kairix")

    @pytest.mark.unit
    def test_windows_localappdata_cache(self) -> None:
        result = _default_cache_dir(
            env={"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"},
            is_docker=False,
            is_service_install=False,
            platform="win32",
        )
        assert result == Path("C:\\Users\\test\\AppData\\Local") / "kairix" / "cache"

    @pytest.mark.unit
    def test_default_fallback(self) -> None:
        result = _default_cache_dir(env={}, is_docker=False, is_service_install=False, platform="linux")
        assert result == Path.home() / ".cache" / "kairix"


# ---------------------------------------------------------------------------
# _load_paths_from_config() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadPathsFromConfig:
    @pytest.mark.unit
    def test_returns_empty_when_no_config(self, tmp_path) -> None:
        result = _load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(tmp_path / "nonexistent.yaml")})
        assert result == {}

    @pytest.mark.integration
    def test_loads_paths_from_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("paths:\n  document_root: /from/config\n  db_path: /from/config/db.sqlite\n")
        result = _load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        assert result.get("document_root") == "/from/config"
        assert result.get("db_path") == "/from/config/db.sqlite"

    @pytest.mark.integration
    def test_graceful_fallback_on_malformed_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        result = _load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        # Should return {} rather than raising
        assert isinstance(result, dict)

    @pytest.mark.integration
    def test_returns_empty_when_no_paths_section(self, tmp_path) -> None:
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("logging:\n  level: DEBUG\n")
        result = _load_paths_from_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        assert result == {}


# ---------------------------------------------------------------------------
# clear_cache() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClearCache:
    @pytest.mark.unit
    def test_clear_cache_invalidates(self, monkeypatch) -> None:
        """Calling clear_cache() should allow the no-arg cached resolver to
        re-read os.environ on the next call.

        This is the only test that legitimately uses monkeypatch.setenv —
        it explicitly exercises the cached ``KairixPaths.resolve()`` (no-arg)
        path that reads ``os.environ``. The cache itself is the contract
        being tested.
        """
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/alpha")
        p1 = KairixPaths.resolve()
        clear_cache()
        monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/beta")
        p2 = KairixPaths.resolve()
        assert p1.document_root == Path("/alpha")
        assert p2.document_root == Path("/beta")
