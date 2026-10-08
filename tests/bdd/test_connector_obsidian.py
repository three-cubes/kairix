"""pytest-bdd binding for connector_obsidian.feature (IM-5 Wave 2).

Steps live in :mod:`tests.bdd.steps.connector_obsidian_steps`.

The scenarios exercise the real :class:`kairix.connectors.obsidian.ObsidianConnector`
against a ``tmp_path`` vault — no monkey-patching. The connector's watcher
runs on the in-process ``FakeWatchdogObserver`` (injected through the
documented ``watcher_factory`` seam) and is closed at scenario teardown, so
no scenario depends on OS filesystem-event timing.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "connector_obsidian.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
