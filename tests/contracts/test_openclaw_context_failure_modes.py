"""F68 failure-mode contract for the ``OpenclawContext`` Protocol.

The kairix-memory-prompt plugin delivers the bootstrap envelope (or the
degraded fallback) through ``context.appendSystemContext``. That method
belongs to openclaw, not kairix: when openclaw's own prompt assembly
fails, the plugin must surface the error to openclaw's plugin host
verbatim — exactly ONE append attempt, no retry, no second
fallback append that would double-write a half-assembled prompt.

F43 parity: no kairix-side ``OpenclawContext`` implementation exists
(openclaw supplies the runtime object), so the body runs over BOTH plugin
code paths that call ``appendSystemContext`` — the bootstrap-success
path and the bootstrap-failed fallback path — via a parametrized
fixture, through the canonical ``FakeOpenclawContext``.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from kairix.plugins.openclaw import memory_prompt_dir
from tests.fakes import FakeOpenclawContext

pytestmark = pytest.mark.contract

_ENVELOPE = "# Bootstrap envelope: agent-alpha\n## Board\npriorities: ship\n"


def _load_plugin() -> ModuleType:
    """Load ``plugin.py`` the way openclaw's plugin loader does (hyphenated dir)."""
    plugin_path = memory_prompt_dir() / "plugin.py"
    spec = importlib.util.spec_from_file_location("memory_prompt_plugin_f68", plugin_path)
    assert spec is not None and spec.loader is not None, "plugin.py not found"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap_ok(_agent: str) -> str:
    return _ENVELOPE


def _bootstrap_fails(_agent: str) -> str:
    raise RuntimeError("kairix bootstrap exited 2: document root missing")


@pytest.fixture(params=["bootstrap-ok", "bootstrap-fails"])
def bootstrap_path(request: pytest.FixtureRequest) -> tuple[str, object]:
    """The two plugin paths that reach ``appendSystemContext``, with the
    text each one tries to append."""
    plugin = _load_plugin()
    if request.param == "bootstrap-ok":
        return (_ENVELOPE, _bootstrap_ok)
    return (plugin.FALLBACK_MESSAGE, _bootstrap_fails)


def test_appendSystemContext_raises_propagates_once_without_retry(  # noqa: N802 — F68 binds the test name to openclaw's camelCase method name
    bootstrap_path: tuple[str, object],
) -> None:
    """``raises``: when openclaw's ``appendSystemContext`` raises, the
    plugin surfaces that exact error after ONE attempt carrying the text
    for its path — it never retries or appends a second (fallback) copy.

    Sabotage proof (executed): wrap the final
    ``context.appendSystemContext(markdown)`` in ``on_session_start`` in
    ``try/except Exception: context.appendSystemContext(FALLBACK_MESSAGE)``
    → the ``bootstrap-ok`` case fails (two append attempts, and the
    error is swallowed). Restored.
    """
    expected_text, run_bootstrap = bootstrap_path
    plugin = _load_plugin()
    context = FakeOpenclawContext(append_raises=RuntimeError("openclaw prompt assembly failed"))
    with pytest.raises(RuntimeError, match="openclaw prompt assembly failed"):
        plugin.on_session_start(context, deps=plugin.PluginDeps(run_bootstrap=run_bootstrap))
    assert context.append_attempts == [expected_text]
    assert context.appended == []
