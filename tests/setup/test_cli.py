"""Unit tests for ``kairix.platform.setup.cli.main``.

Cover the ``ctx=None`` branch that auto-detects a SetupContext.
BDD coverage in ``tests/bdd/test_setup_cli.py`` always passes an
explicit ``ctx`` to avoid touching ``XDG_CONFIG_HOME`` and stdout
TTY state, leaving the auto-detect path uncovered. These tests
pin that path under non-interactive + JSON mode so the auto-detect
branch is exercised without any interactive prompts.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest


@pytest.mark.unit
def test_main_with_ctx_none_auto_detects_context(monkeypatch, tmp_path: Path) -> None:
    """When ``ctx`` is None, main() calls SetupContext.auto_detect().

    We point XDG_CONFIG_HOME at a tmp dir so auto_detect creates its
    state file there (not in the developer's real ~/.config). We use
    --non-interactive and --json so the wizard never prompts and so
    auto_detect picks json_mode=True (no TTY probe needed).
    Note: XDG_CONFIG_HOME is NOT a kairix env var — F2 only forbids
    monkeypatching KAIRIX_* vars.
    """
    from kairix.platform.setup.cli import main as setup_main

    # Redirect XDG_CONFIG_HOME so auto_detect doesn't write to ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    docroot = tmp_path / "docs"
    docroot.mkdir()
    (docroot / "hello.md").write_text("# hello\n")

    out = io.StringIO()
    err = io.StringIO()
    argv = [
        "--non-interactive",
        "--json",
        "--preset",
        "general",
        "--path",
        str(docroot),
        "--output",
        str(tmp_path / "kairix.config.yaml"),
    ]

    with redirect_stdout(out), redirect_stderr(err):
        # main() calls sys.exit(0 if success else 1) — catch it.
        with pytest.raises(SystemExit) as exc_info:
            setup_main(argv)  # ctx=None on purpose — exercises auto_detect branch

    assert exc_info.value.code == 0, f"setup should succeed; stderr={err.getvalue()!r}"
    # --json mode emits parseable JSON.
    payload = json.loads(out.getvalue())
    assert "paths" in payload, f"missing paths in JSON output: {payload}"
    # The auto-detect path also creates the XDG state file as a side-effect.
    assert (tmp_path / "xdg" / "kairix").exists(), "auto_detect should have created XDG config dir"
