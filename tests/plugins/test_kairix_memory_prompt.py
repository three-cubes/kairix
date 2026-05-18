"""Unit tests for the kairix-memory-prompt openclaw plugin (#246 W5).

The plugin's directory is hyphenated (``memory-prompt/``) because that
is the slug openclaw discovers via ``plugins.load.paths``. Python
cannot import hyphenated module names directly, so we load ``plugin.py``
via :func:`importlib.util.spec_from_file_location` — the same mechanism
openclaw's plugin loader uses. This mirrors how the plugin runs in
production and means the test path is the same as the runtime path.

The fake openclaw context is constructed inline (a Protocol-shaped
``dataclass``-light) rather than reaching into ``tests/fakes.py`` —
``tests/fakes.py`` hosts kairix domain fakes (FakePaths, FakeNeo4j) and
not openclaw runtime stand-ins. Inline is the right scope for a runtime
adapter shim.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kairix.plugins.openclaw import memory_prompt_dir


def _load_plugin() -> ModuleType:
    """Load ``plugin.py`` exactly the way openclaw would load it."""
    plugin_path = memory_prompt_dir() / "plugin.py"
    spec = importlib.util.spec_from_file_location("memory_prompt_plugin", plugin_path)
    assert spec is not None and spec.loader is not None, "plugin.py not found"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin() -> Iterator[ModuleType]:
    """Load the plugin module fresh for each test."""
    yield _load_plugin()


class FakeOpenclawContext:
    """Minimal stand-in for openclaw's plugin context object.

    Exposes the two surfaces ``plugin.on_session_start`` relies on:
    ``agent_name`` (attribute) and ``appendSystemContext`` (method).
    Captures appended strings in ``appended`` so tests can assert on
    what reached the agent's system prompt.
    """

    def __init__(self, agent_name: str = "alpha") -> None:
        self.agent_name = agent_name
        self.appended: list[str] = []

    def appendSystemContext(self, text: str) -> None:  # noqa: N802 — openclaw API name
        self.appended.append(text)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_start_appends_bootstrap_markdown_on_success(plugin: ModuleType) -> None:
    """When kairix bootstrap succeeds, its stdout is appended verbatim."""
    ctx = FakeOpenclawContext(agent_name="alpha")
    deps = plugin.PluginDeps(
        run_bootstrap=lambda agent: f"# Bootstrap envelope: {agent}\n## Board\npriorities: ship\n",
    )

    plugin.on_session_start(ctx, deps=deps)

    assert len(ctx.appended) == 1, "exactly one appendSystemContext call expected"
    assert "# Bootstrap envelope: alpha" in ctx.appended[0]
    assert "priorities: ship" in ctx.appended[0]
    # Specifically — NOT the fallback. Sabotage-prove: an implementation that
    # always appended the fallback would pass an "appended at all" check.
    assert plugin.FALLBACK_MESSAGE not in ctx.appended[0]


@pytest.mark.unit
def test_session_start_passes_agent_name_through_to_bootstrap(plugin: ModuleType) -> None:
    """The agent name openclaw supplies is what gets passed to bootstrap.

    Sabotage-prove: if the plugin hard-coded a placeholder name, this
    test fails because the captured agent name is not ``beta``.
    """
    ctx = FakeOpenclawContext(agent_name="beta")
    captured_agent: list[str] = []

    def fake_bootstrap(agent: str) -> str:
        captured_agent.append(agent)
        return f"envelope for {agent}"

    deps = plugin.PluginDeps(run_bootstrap=fake_bootstrap)
    plugin.on_session_start(ctx, deps=deps)

    assert captured_agent == ["beta"]
    assert ctx.appended == ["envelope for beta"]


@pytest.mark.unit
def test_session_start_strips_whitespace_around_agent_name(plugin: ModuleType) -> None:
    """Trailing/leading whitespace in agent_name is normalised before use."""
    ctx = FakeOpenclawContext(agent_name="  gamma\n")
    captured: list[str] = []

    def fake_bootstrap(agent: str) -> str:
        captured.append(agent)
        return f"envelope for {agent}"

    deps = plugin.PluginDeps(run_bootstrap=fake_bootstrap)
    plugin.on_session_start(ctx, deps=deps)

    assert captured == ["gamma"], "agent name should be stripped"


# ---------------------------------------------------------------------------
# Failure / fallback paths — session start MUST NOT be blocked
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_start_appends_fallback_when_bootstrap_raises(plugin: ModuleType) -> None:
    """Subprocess raised — plugin must still call appendSystemContext."""
    ctx = FakeOpenclawContext(agent_name="alpha")

    def boom(agent: str) -> str:
        raise RuntimeError("kairix binary not on PATH")

    deps = plugin.PluginDeps(run_bootstrap=boom)
    plugin.on_session_start(ctx, deps=deps)

    assert ctx.appended == [plugin.FALLBACK_MESSAGE]


@pytest.mark.unit
def test_fallback_message_is_short_and_actionable(plugin: ModuleType) -> None:
    """The fallback string tells the admin what to run.

    Sabotage-prove: an implementation that emitted a multi-paragraph
    diagnostic would bloat the agent prompt and break this assertion.
    The contract is: short, prescriptive, names the canonical command.
    """
    assert len(plugin.FALLBACK_MESSAGE) < 200, "fallback must be short"
    assert "kairix onboard check" in plugin.FALLBACK_MESSAGE
    assert "kairix bootstrap unavailable" in plugin.FALLBACK_MESSAGE


@pytest.mark.unit
def test_session_start_fallback_on_empty_stdout(plugin: ModuleType) -> None:
    """Zero-byte stdout is treated as failure — agent still gets fallback."""
    ctx = FakeOpenclawContext(agent_name="alpha")
    deps = plugin.PluginDeps(run_bootstrap=lambda agent: "")
    plugin.on_session_start(ctx, deps=deps)

    assert ctx.appended == [plugin.FALLBACK_MESSAGE]


@pytest.mark.unit
def test_session_start_fallback_on_whitespace_only_stdout(plugin: ModuleType) -> None:
    """Whitespace-only stdout is functionally empty for the agent."""
    ctx = FakeOpenclawContext(agent_name="alpha")
    deps = plugin.PluginDeps(run_bootstrap=lambda agent: "  \n\t\n  ")
    plugin.on_session_start(ctx, deps=deps)

    assert ctx.appended == [plugin.FALLBACK_MESSAGE]


@pytest.mark.unit
def test_session_start_fallback_on_blank_agent_name(plugin: ModuleType) -> None:
    """openclaw passed an empty agent_name — plugin uses fallback, does not crash."""
    ctx = FakeOpenclawContext(agent_name="")
    # Sabotage-prove: if the plugin still called the subprocess, this
    # would record a call. The fallback path must NOT shell out.
    called: list[str] = []

    def trip(agent: str) -> str:
        called.append(agent)
        return "should never be returned"

    deps = plugin.PluginDeps(run_bootstrap=trip)
    plugin.on_session_start(ctx, deps=deps)

    assert called == [], "plugin must not invoke bootstrap with a blank agent name"
    assert ctx.appended == [plugin.FALLBACK_MESSAGE]


@pytest.mark.unit
def test_session_start_fallback_on_missing_agent_name_attribute(plugin: ModuleType) -> None:
    """Context object lacks agent_name entirely — still no crash."""

    class ContextWithoutAgentName:
        def __init__(self) -> None:
            self.appended: list[str] = []

        def appendSystemContext(self, text: str) -> None:  # noqa: N802
            self.appended.append(text)

    ctx = ContextWithoutAgentName()
    plugin.on_session_start(ctx, deps=plugin.PluginDeps(run_bootstrap=lambda agent: "x"))
    assert ctx.appended == [plugin.FALLBACK_MESSAGE]


@pytest.mark.unit
def test_session_start_never_raises_even_when_bootstrap_returns_garbage(
    plugin: ModuleType,
) -> None:
    """The hard contract: on_session_start NEVER raises out of the plugin.

    Sabotage-prove for "degraded != broken". An openclaw session start
    that raises out of a plugin would crash the agent boot. This test
    pins that contract: every exception class the subprocess might
    raise reduces to a fallback append, never a propagated exception.
    """
    ctx = FakeOpenclawContext(agent_name="alpha")

    for exc_factory in (
        lambda: TimeoutError("kairix bootstrap timed out"),
        lambda: FileNotFoundError("kairix"),
        lambda: PermissionError("no permission to exec kairix"),
        lambda: RuntimeError("non-zero exit"),
    ):

        def raise_it(agent: str, factory: Any = exc_factory) -> str:
            raise factory()

        deps = plugin.PluginDeps(run_bootstrap=raise_it)
        # Must not raise.
        plugin.on_session_start(ctx, deps=deps)

    # Every iteration appended the fallback — four iterations total.
    assert ctx.appended == [plugin.FALLBACK_MESSAGE] * 4


# ---------------------------------------------------------------------------
# Plugin manifest — operator-facing contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plugin_manifest_declares_canonical_name() -> None:
    """plugin.json names the plugin exactly as openclaw's plugins.allow expects."""
    import json

    manifest_path = memory_prompt_dir() / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"] == "kairix-memory-prompt"
    assert manifest["runtime"] == "python"
    assert manifest["entry"] == "plugin.py"
    assert manifest["entryFunction"] == "on_session_start"
    # Append, not replace — the runtime must merge memory into the existing
    # system prompt rather than overwrite the host's prompt.
    assert manifest["capabilities"]["promptInjection"] == "append"


@pytest.mark.unit
def test_memory_prompt_dir_points_at_real_directory() -> None:
    """The helper resolves to a directory shipped with the package."""
    plugin_dir = memory_prompt_dir()
    assert plugin_dir.is_dir()
    assert (plugin_dir / "plugin.py").is_file()
    assert (plugin_dir / "plugin.json").is_file()
    assert (plugin_dir / "README.md").is_file()


@pytest.mark.unit
def test_plugin_readme_documents_required_openclaw_config_keys() -> None:
    """README contains the three openclaw config keys an admin must paste."""
    readme = (memory_prompt_dir() / "README.md").read_text(encoding="utf-8")
    # The three required keys, by canonical dotted name.
    assert "plugins.load.paths" in readme
    assert "plugins.allow" in readme
    assert "plugins.entries.kairix-memory-prompt.hooks.allowPromptInjection" in readme or (
        "kairix-memory-prompt" in readme and "allowPromptInjection" in readme
    )
    # Canonical install path admins paste.
    assert "/opt/kairix/plugins/openclaw" in readme


# ---------------------------------------------------------------------------
# Default subprocess wiring — exercises the production branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_run_bootstrap_invokes_kairix_via_resolved_path(plugin: ModuleType, tmp_path: Path) -> None:
    """When ``kairix`` is on PATH, the default wiring shells out to it.

    We stand up a fake kairix shell script in a tmp dir and prepend it
    to ``PATH`` via the subprocess ``env`` argument — never via
    ``os.environ`` mutation (F2 / F4 clean). The fake script exits 0
    and echoes a recognisable marker so we can assert the wiring works.
    """
    import os
    import stat

    fake_kairix = tmp_path / "kairix"
    fake_kairix.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "bootstrap" ] && [ -n "$2" ]; then\n'
        '  echo "FAKE_BOOTSTRAP_OUTPUT for $2"\n'
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_kairix.chmod(fake_kairix.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Run inside a subshell whose PATH only sees our fake binary +
    # /usr/bin (so the shebang resolves /usr/bin/env). We do this via
    # subprocess.run's env kwarg in a small wrapper that re-invokes the
    # plugin's default wiring with a PATH-controlled environment.
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-c", _DEFAULT_WIRING_PROBE],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "FAKE_BOOTSTRAP_OUTPUT for alpha" in completed.stdout


# Tiny script the test above runs in a clean subprocess. Imports the
# plugin module, calls the default wiring, prints stdout. Kept inline
# so the test is self-contained. ``sys.modules`` registration before
# ``exec_module`` is required so the ``@dataclass`` decorator inside
# ``plugin.py`` can resolve forward references on Python 3.14+.
_DEFAULT_WIRING_PROBE = """\
import importlib.util, sys
from kairix.plugins.openclaw import memory_prompt_dir
spec = importlib.util.spec_from_file_location("mp", memory_prompt_dir() / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["mp"] = mod
spec.loader.exec_module(mod)
print(mod._default_run_bootstrap("alpha"))
"""


@pytest.mark.unit
def test_default_run_bootstrap_raises_when_binary_missing(plugin: ModuleType, tmp_path: Path) -> None:
    """If ``shutil.which("kairix")`` returns None, the default wiring raises."""
    import os
    import subprocess

    # An empty bin dir, plus /usr/bin for the python interpreter. The
    # kairix binary is intentionally not in the PATH.
    completed = subprocess.run(
        [sys.executable, "-c", _DEFAULT_WIRING_PROBE],
        env={"PATH": f"{tmp_path}:/usr/bin", "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode != 0, "default wiring must raise when binary is missing"
    assert "kairix binary not on PATH" in completed.stderr


# ---------------------------------------------------------------------------
# In-process unit tests for _default_run_bootstrap. The two tests above
# spawn a clean subprocess so they verify the wiring on a real PATH but
# do NOT contribute coverage to ``plugin.py`` in the parent pytest run
# (coverage instruments only the parent process). The in-process tests
# below ``monkeypatch.setattr`` shutil.which and subprocess.run on the
# loaded plugin module so each branch (missing binary / non-zero exit /
# happy path) executes inside the measured process.
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Tiny stand-in for ``subprocess.CompletedProcess`` — only fields the
    plugin reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
def test_default_run_bootstrap_raises_in_process_when_binary_missing(
    plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process branch coverage: ``shutil.which`` → None → raises.

    Sabotage-prove: if the wrapper silently returned an empty string
    instead of raising, the agent prompt would silently degrade. The
    contract is that the caller (``on_session_start``) catches the
    exception and emits the fallback message.
    """
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="kairix binary not on PATH"):
        plugin._default_run_bootstrap("alpha")


@pytest.mark.unit
def test_default_run_bootstrap_happy_path_returns_stdout(plugin: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process branch coverage: exit 0 → wrapper returns captured stdout.

    Sabotage-prove: if the wrapper returned ``completed.stderr`` instead
    of stdout, this assertion would catch the swap.
    """
    captured_argv: list[list[str]] = []

    def _fake_which(name: str) -> str:
        return f"/fake/bin/{name}"

    def _fake_run(argv: list[str], **_kwargs: Any) -> _FakeCompleted:
        captured_argv.append(argv)
        return _FakeCompleted(returncode=0, stdout="# envelope for alpha\n")

    monkeypatch.setattr(plugin.shutil, "which", _fake_which)
    monkeypatch.setattr(plugin.subprocess, "run", _fake_run)

    out = plugin._default_run_bootstrap("alpha")

    assert out == "# envelope for alpha\n"
    assert captured_argv == [["/fake/bin/kairix", "bootstrap", "alpha"]]


@pytest.mark.unit
def test_default_run_bootstrap_raises_with_stderr_tail_on_non_zero_exit(
    plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process branch coverage: non-zero exit → wrapper raises with
    the stderr tail spliced into the message.

    Sabotage-prove: if the wrapper raised with just the return code and
    no stderr context, an operator reading the failure would have no
    clue what actually went wrong. The substring assertions pin both
    sides of the message.
    """
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: "/fake/bin/kairix")
    stderr_blob = "line1\nline2\nERROR: agent slug not found\n"
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda _argv, **_kw: _FakeCompleted(returncode=2, stderr=stderr_blob),
    )

    with pytest.raises(RuntimeError) as excinfo:
        plugin._default_run_bootstrap("alpha")

    msg = str(excinfo.value)
    assert "exited 2" in msg
    assert "ERROR: agent slug not found" in msg


@pytest.mark.unit
def test_default_run_bootstrap_handles_empty_stderr_on_non_zero_exit(
    plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty stderr → message includes the sentinel ``<no stderr>``.

    Sabotage-prove: if the wrapper omitted the sentinel and emitted a
    bare ``exited 1`` the operator would still need to grep logs to find
    what happened. The sentinel keeps the failure self-describing.
    """
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: "/fake/bin/kairix")
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda _argv, **_kw: _FakeCompleted(returncode=1, stderr=""),
    )

    with pytest.raises(RuntimeError, match="<no stderr>"):
        plugin._default_run_bootstrap("alpha")
