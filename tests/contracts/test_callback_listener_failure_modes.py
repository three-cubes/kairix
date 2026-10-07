"""F68 failure-injection contract tests for :class:`CallbackListener`.

* ``wait_for_callback`` → ``times_out`` (operator never completes flow)
* ``redirect_uri`` (property) → ``returns_empty`` shape: the URL is never
  blank — it always carries the loopback host and the canonical
  ``/oauth2callback`` path
* ``close`` → ``unavailable`` shape pinned via idempotent-after-close

Every body runs over BOTH the production
:class:`LocalhostCallbackListener` (bound to a free loopback port) and
the canonical :class:`tests.fakes.FakeCallbackListener` (F43 behavioural
parity).
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator

import pytest

from kairix.connect.listener import LocalhostCallbackListener
from kairix.connect.protocols import CallbackListener, CallbackTimeoutError
from tests.fakes import FakeCallbackListener

pytestmark = pytest.mark.contract


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _real_listener() -> CallbackListener:
    return LocalhostCallbackListener(port=_free_port())


def _fake_listener() -> CallbackListener:
    # The fake never completes a callback — the timeout knob models the
    # "operator never finished the browser flow" path the real listener
    # reaches by waiting out ``timeout_s`` with no request.
    port = _free_port()
    return FakeCallbackListener(timeout=True, redirect_uri=f"http://127.0.0.1:{port}/oauth2callback", port=port)


_IMPLEMENTATIONS: list[tuple[str, Callable[[], CallbackListener]]] = [
    ("real", _real_listener),
    ("fake", _fake_listener),
]


@pytest.fixture
def listeners() -> Iterator[list[CallbackListener]]:
    """Collects every listener a test builds and closes it afterwards so
    the real listener's socket is released even on assertion failure."""
    built: list[CallbackListener] = []
    yield built
    for listener in built:
        listener.close()


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_wait_for_callback_times_out_when_no_browser_completes(
    name: str, factory: Callable[[], CallbackListener], listeners: list[CallbackListener]
) -> None:
    """No callback arrives → :class:`CallbackTimeoutError` with an
    actionable ``fix:`` pointer, on both impls.

    Sabotage proof (executed): in ``LocalhostCallbackListener.wait_for_callback``
    replace the ``raise CallbackTimeoutError(...)`` on the not-completed
    branch with ``return None``. Re-run: the ``real`` case fails. Restored.
    """
    listener = factory()
    listeners.append(listener)
    with pytest.raises(CallbackTimeoutError) as exc_info:
        listener.wait_for_callback(timeout_s=0.05)
    assert "fix:" in str(exc_info.value), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_redirect_uri_returns_empty_path_when_listener_never_bound(
    name: str, factory: Callable[[], CallbackListener], listeners: list[CallbackListener]
) -> None:
    """``redirect_uri`` is never blank — before any callback is awaited
    it already names the loopback host and the canonical
    ``/oauth2callback`` path the OAuth provider must redirect to.

    Sabotage proof: in ``LocalhostCallbackListener.redirect_uri`` drop
    the ``_CALLBACK_PATH`` suffix. Re-run: the ``real`` case fails.
    Restored.
    """
    listener = factory()
    listeners.append(listener)
    uri = listener.redirect_uri
    assert uri.startswith("http://127.0.0.1:"), f"{name}: {uri!r}"
    assert uri.endswith("/oauth2callback"), f"{name}: {uri!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_close_unavailable_when_called_twice(name: str, factory: Callable[[], CallbackListener]) -> None:
    """``close`` is idempotent — the second call must not raise even if
    the socket is already torn down.

    Sabotage proof: in ``LocalhostCallbackListener._cleanup`` remove the
    ``except (OSError, AttributeError)`` guard and make ``server_close``
    raise on a second call. Re-run: the ``real`` case fails. Restored.
    """
    listener = factory()
    listener.close()
    listener.close()  # second close — must not raise
