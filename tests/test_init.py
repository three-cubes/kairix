"""Tests for the top-level kairix package import.

Covers:
  - happy path: __version__ is a non-empty string and the public API
    symbols are importable
  - fallback path: when importlib.metadata.version raises, __version__
    falls back to "0.0.0"
  - guarded imports: when an optional submodule fails to import, the
    package still loads (the symbol just isn't exposed)

The fallback and guarded-import paths run in a fresh interpreter
(``subprocess``): importing ``kairix`` again in the shared test process would
mean evicting or re-executing live kairix modules (F1).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_PUBLIC_API = {
    "SearchResult": "kairix.core.search.pipeline",
    "RetrievalConfig": "kairix.core.search.config",
    "QueryIntent": "kairix.core.search.intent",
}


def _run_python(code: str) -> str:
    """Run ``code`` in a fresh interpreter and return its stripped stdout."""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    return result.stdout.strip()


def test_kairix_version_is_non_empty_string() -> None:
    import kairix

    assert isinstance(kairix.__version__, str)
    assert kairix.__version__  # non-empty


def test_public_api_symbols_available() -> None:
    """SearchResult, RetrievalConfig, QueryIntent are importable from kairix."""
    import kairix

    for symbol in _PUBLIC_API:
        assert hasattr(kairix, symbol), symbol


def test_version_falls_back_when_metadata_lookup_raises() -> None:
    """Any failing ``importlib.metadata.version`` yields ``__version__ == "0.0.0"``.

    The child's lookup raises a non-ImportError (a broken metadata backend),
    so the test pins the broad ``except Exception`` fallback.

    Sabotage proof (executed): narrowing that ``except Exception`` in
    kairix/__init__.py to ``except ImportError`` makes the child crash and
    this test fails; restored.
    """
    code = (
        "import importlib.metadata as m\n"
        "def _broken(name):\n"
        "    raise RuntimeError(name)\n"
        "m.version = _broken\n"
        "import kairix\n"
        "print(kairix.__version__)\n"
    )
    assert _run_python(code) == "0.0.0"


@pytest.mark.parametrize(("symbol", "module"), sorted(_PUBLIC_API.items()))
def test_package_loads_without_an_optional_public_module(symbol: str, module: str) -> None:
    """A public-API module that cannot be imported leaves its symbol unbound.

    ``sys.modules[name] = None`` makes ``import name`` raise ImportError in
    the child — the missing-optional-dependency shape. The package still
    loads (the child exits 0); the symbol simply isn't bound.

    Sabotage proof (executed): removing the ``try / except ImportError``
    around the ``QueryIntent`` import makes the child crash and the
    QueryIntent case fails; restored.
    """
    code = f"import sys\nsys.modules[{module!r}] = None\nimport kairix\nprint(hasattr(kairix, {symbol!r}))\n"
    assert _run_python(code) == "False"
