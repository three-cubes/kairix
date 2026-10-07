"""F68 failure-injection contract test for :class:`BrowserLauncher`.

  * ``open`` → ``unavailable`` (headless / $DISPLAY missing / no browser)

One body runs over BOTH the production default launcher (the
``webbrowser.open`` wrapper every ``kairix.connect.oauth2`` flow falls
back to) and the canonical :class:`tests.fakes.FakeBrowserLauncher`
(F43 behavioural parity). The production launcher is "unavailable" in
the pytest session because ``tests/conftest.py`` sets the
``KAIRIX_CONNECT_DISABLE_BROWSER`` kill-switch — exactly the
no-browser-located branch, where it returns ``False`` (the
``webbrowser.open`` contract) instead of raising.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.connect.oauth2.google import DefaultBrowserLauncher
from kairix.connect.protocols import BrowserLauncher
from tests.fakes import FakeBrowserLauncher

pytestmark = pytest.mark.contract

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"

_IMPLEMENTATIONS: list[tuple[str, Callable[[], BrowserLauncher]]] = [
    ("real", DefaultBrowserLauncher),
    ("fake", lambda: FakeBrowserLauncher(result=False)),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_open_unavailable_when_no_browser_found(name: str, factory: Callable[[], BrowserLauncher]) -> None:
    """``open`` returns ``False`` (never raises) when no browser could be
    launched — the caller prints the URL for the operator to open by hand.

    Sabotage proof (executed): in ``_DefaultBrowser.open`` (google flow)
    change the kill-switch branch's ``return False`` to ``return True``.
    Re-run: the ``real`` case fails. Restored.
    """
    launcher = factory()
    assert isinstance(launcher, BrowserLauncher), name
    result = launcher.open(_AUTHORIZE_URL)
    assert result is False, f"{name}: unavailable browser must report False, got {result!r}"
    if isinstance(launcher, FakeBrowserLauncher):
        # The fake also records the URL so tests can see what was
        # attempted even when the launch failed.
        assert launcher.opened == [_AUTHORIZE_URL]
