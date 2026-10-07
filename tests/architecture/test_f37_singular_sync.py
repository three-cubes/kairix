"""Unit tests for F37 (``scripts/checks/check_f37_singular_sync.py``).

F37 forbids change-detection / sync code (imports of ``watchdog``,
``msgraph``, ``notion_client``, ``slack_sdk.rtm``/``.socket_mode``,
``dulwich``) anywhere under ``kairix/`` except
``kairix/connectors/<name>/`` and ``kairix/core/connectors/``. Mirrors
F29's singular-surface shape (perf code only under
``kairix/quality/probe/``).

Each test has an inline sabotage-proof: the violating file is moved
to a sanctioned location and the gate clears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f37_singular_sync.py"


def _load_detector():
    """Load the F37 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f37_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f37_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_real_repo_gate_is_green() -> None:
    """The real F37 detector run against the full repo emits no
    violations (there is no grandfathering).
    Today (pre-Wave 1) the connector trees do not yet exist and no
    file imports a change-detection library, so the result is
    vacuous-green.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_watchdog_import_under_connectors_is_allowed(tmp_path: Path) -> None:
    """A ``watchdog`` import in ``kairix/connectors/obsidian/watcher.py``
    is the canonical home — never flagged.

    Sabotage-proof inline: relocate the same import to
    ``kairix/worker.py`` and the detector fires.
    """
    detector = _load_detector()
    canonical = tmp_path / "kairix" / "connectors" / "obsidian" / "watcher.py"
    _write(canonical, "from watchdog.observers import Observer\n")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: same import under kairix/worker.py.
    canonical.unlink()
    sabotage = tmp_path / "kairix" / "worker.py"
    _write(sabotage, "from watchdog.observers import Observer\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/worker.py") in violations


def test_msgraph_import_under_core_connectors_is_allowed(tmp_path: Path) -> None:
    """An ``msgraph`` import inside ``kairix/core/connectors/`` is
    allowed — the orchestration layer is one of the two sanctioned
    homes for change-detection code.
    """
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "core" / "connectors" / "delta_runner.py",
        "from msgraph import GraphServiceClient\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_msgraph_import_in_corpus_is_flagged(tmp_path: Path) -> None:
    """``kairix/corpus/poll.py`` importing ``msgraph`` is rejected —
    F37's whole point is no parallel sync surfaces under corpus/."""
    detector = _load_detector()
    target = tmp_path / "kairix" / "corpus" / "poll.py"
    _write(target, "from msgraph import GraphServiceClient\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/corpus/poll.py") in violations

    # Sabotage: move into kairix/core/connectors/ — gate clears.
    target.unlink()
    _write(
        tmp_path / "kairix" / "core" / "connectors" / "poll.py",
        "from msgraph import GraphServiceClient\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_notion_import_in_transport_is_flagged(tmp_path: Path) -> None:
    """``notion_client`` imported under ``kairix/transport/`` is a
    parallel sync surface — flagged."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "transport" / "notion_pump.py",
        "import notion_client\n",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/transport/notion_pump.py") in violations


def test_slack_sdk_web_import_is_not_flagged(tmp_path: Path) -> None:
    """``slack_sdk.web`` is the one-shot HTTP surface, not a sync
    loop — F37 must not flag it.

    Sabotage-proof: switching to ``slack_sdk.socket_mode`` (real-time)
    in the same out-of-tree location does fire the detector.
    """
    detector = _load_detector()
    web_path = tmp_path / "kairix" / "transport" / "slack_post.py"
    _write(web_path, "from slack_sdk.web import WebClient\n")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: switch to socket_mode — that IS sync.
    web_path.write_text(
        "from slack_sdk.socket_mode import SocketModeClient\n",
        encoding="utf-8",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/transport/slack_post.py") in violations


def test_dulwich_import_under_per_connector_tree_is_allowed(tmp_path: Path) -> None:
    """``dulwich`` change-detection inside a per-connector subtree
    (``kairix/connectors/github/`` for example) is allowed."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "connectors" / "github" / "delta.py",
        "import dulwich.client\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_non_sync_files_are_not_flagged(tmp_path: Path) -> None:
    """A file that imports nothing from the sync library set is
    untouched, regardless of where it sits."""
    detector = _load_detector()
    _write(tmp_path / "kairix" / "transport" / "pool" / "client.py", "import httpx\n")
    _write(tmp_path / "kairix" / "providers" / "openai" / "embed.py", "import openai\n")
    _write(tmp_path / "kairix" / "core" / "search" / "bm25.py", "import sqlite3\n")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_kairix_directory_passes(tmp_path: Path) -> None:
    """Fresh checkout: no ``kairix/`` directory — gate green."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_is_sync_import_matches_expected_targets() -> None:
    """The import classifier picks up the documented change-detection
    libraries and rejects the rest."""
    detector = _load_detector()
    for good in (
        "watchdog",
        "watchdog.observers",
        "msgraph",
        "msgraph.core",
        "msgraph_core",
        "notion_client",
        "notion_client.client",
        "slack_sdk.rtm",
        "slack_sdk.socket_mode",
        "dulwich",
        "dulwich.client",
    ):
        assert detector._is_sync_import(good), good
    for bad in (
        "httpx",
        "openai",
        "sqlite3",
        "slack_sdk",  # bare slack_sdk → Web client only
        "slack_sdk.web",
        "kairix.core.protocols",
    ):
        assert not detector._is_sync_import(bad), bad


def test_remediation_carries_action_markers() -> None:
    """F37's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
