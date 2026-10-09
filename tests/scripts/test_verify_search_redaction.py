"""``scripts/verify-search.py`` report notes never carry exception text."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-search.py"
_SENTINEL = "credential-sentinel-7f3a"  # stand-in for a secret an exception message could echo


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_search_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_search_under_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib stubs omit exec_module
    return mod


def test_failed_search_note_is_class_only(tmp_path: Path) -> None:
    """A search whose subprocess cannot start (FileNotFoundError, whose message
    names the binary path — here carrying the sentinel) yields a class-only
    note in the JSON report.

    Sabotage-proof: restore ``note=str(exc)[:120]`` in ``check_search`` — the
    sentinel path lands in the note. Restored.
    """
    mod = _load()
    missing_bin = tmp_path / _SENTINEL / "kairix"
    result = mod.check_search("probe", "q", "semantic", 1, "agent-alpha", str(missing_bin))
    assert result.passed is False
    assert result.note == "raised FileNotFoundError"
    assert _SENTINEL not in result.note
