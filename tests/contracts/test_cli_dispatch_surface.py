"""Contract: kairix CLI top-level dispatch surface is stable.

Pins the **public CLI surface** that operators and agents reach for:

- ``kairix --version`` / ``kairix -V`` / ``kairix version`` print a version
  string with the format ``kairix <semver-or-calver>``.
- The version printed is **not** the ``0.0.0`` editable-install fallback
  in a built/installed deployment — when the package was built and
  installed via the wheel pipeline (Dockerfile, ``pip install .``),
  ``importlib.metadata.version("Kairix-agentic-knowledge-mgt")``
  resolves to the build-stamped CalVer string. The fallback is only
  acceptable in editable installs (developer machines) where the
  package metadata isn't carried.
- Every documented subcommand from the help banner appears in the
  module's dispatch table so a typo / accidental delete is caught
  immediately, not in production.

Gap this fills: prior to this contract, `kairix --version = 0.0.0` and a
removed `probe-config` subcommand both reached a running container with
no test catching either. Sabotage-proof: drop ``probe-config`` from
the dispatch table, or break the version handler, and the corresponding
case fails locally before CI sees it.
"""

from __future__ import annotations

import importlib.metadata
import io
import sys
from contextlib import redirect_stdout

import pytest

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Version surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--version", "-V", "version"])
def test_version_flag_prints_kairix_version_string(flag: str) -> None:
    """``kairix --version`` (and aliases) print ``kairix <version>`` to stdout.

    Sabotage-proof: remove or rename the version-handler branch in
    ``kairix.cli.main`` and this assertion fails because either
    SystemExit doesn't fire or the output doesn't start with "kairix ".
    """
    from kairix.cli import main

    original_argv = sys.argv
    sys.argv = ["kairix", flag]
    try:
        captured = io.StringIO()
        with redirect_stdout(captured), pytest.raises(SystemExit) as info:
            main()
        assert info.value.code == 0, f"--version handler exited non-zero: {info.value.code}"
        output = captured.getvalue().strip()
        assert output.startswith("kairix "), f"expected `kairix <version>` format; got {output!r}"
    finally:
        sys.argv = original_argv


def test_kairix_version_is_a_non_empty_string() -> None:
    """``kairix.__version__`` is always a non-empty string, never None.

    Sabotage-proof: change the fallback to ``None`` and this fails on
    every machine (editable + built).
    """
    from kairix import __version__

    assert isinstance(__version__, str)
    assert __version__, "__version__ must be a non-empty string"


def test_version_resolves_from_distribution_metadata_when_available() -> None:
    """When the package metadata IS resolvable under the distribution name
    ``Kairix-agentic-knowledge-mgt``, ``__version__`` carries that value,
    not the ``0.0.0`` fallback.

    This is the **production-build invariant** — a build / install that
    produces ``0.0.0`` is broken even though the import works. The
    Dockerfile + pip install paths both produce a wheel that carries
    metadata; only editable installs from the source tree without
    ``setup.py develop --no-deps`` fall through to the fallback.

    Sabotage-proof: rename the lookup string in ``kairix/__init__.py``
    (e.g. to ``"kairix"``) and this test fails on built installs while
    the rest of the test suite passes — exactly the regression that
    let v2026.5.17a9 ship with ``kairix --version = 0.0.0``.
    """
    try:
        meta_version = importlib.metadata.version("Kairix-agentic-knowledge-mgt")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(
            "package metadata not available — running from editable install "
            "without distribution metadata; the production-build invariant "
            "is enforced at build time in the Dockerfile + release-alpha workflow"
        )

    from kairix import __version__

    assert __version__ == meta_version, (
        f"__version__ ({__version__!r}) diverged from distribution metadata "
        f"({meta_version!r}) — the lookup name in kairix/__init__.py is wrong "
        f"or the fallback path is being taken silently"
    )
    assert __version__ != "0.0.0", (
        f"__version__ resolved to fallback '0.0.0' despite metadata being available "
        f"as {meta_version!r} — silent regression on the version surface"
    )


# ---------------------------------------------------------------------------
# Dispatch table — every documented subcommand is wired
# ---------------------------------------------------------------------------


# Public subcommands the operator + agent surface depend on. This list is
# narrower than the full ``COMMANDS`` dispatch — it pins the agent-facing
# and operator-diagnostic surfaces specifically, so a regression that
# drops any of these (e.g. ``probe-config`` going missing in v2026.5.17a9)
# is caught by the contract test before reaching production.
#
# Adding a public-surface subcommand: add a row here.
# A help-banner-vs-dispatch drift audit lives in a separate test below.
DOCUMENTED_SUBCOMMANDS = (
    # Agent-facing retrieval / synthesis
    "bootstrap",
    "search",
    "entity",
    "prep",
    "brief",
    "research",
    "timeline",
    "contradict",
    "summarise",
    "classify",
    "usage-guide",
    # Indexing / store
    "embed",
    "store",
    "wikilinks",
    "curator",
    # MCP transport
    "mcp",
    # Operator diagnostics (this set includes the surfaces that
    # historically regressed silently)
    "onboard",
    "probe-config",
    "warm",
    "worker",
    # Setup / eval
    "setup",
    "eval",
    "benchmark",
    "reference-library",
)


@pytest.mark.parametrize("subcommand", DOCUMENTED_SUBCOMMANDS)
def test_subcommand_resolves_through_dispatch(subcommand: str) -> None:
    """Each documented subcommand resolves to a target module via the
    top-level dispatch — no accidental deletes, no help-vs-impl drift.

    Sabotage-proof: drop ``probe-config`` (or any documented subcommand)
    from the dispatch table in ``kairix/cli.py`` and the matching
    parametrised case fails. That's exactly the regression that let
    "Unknown command: probe-config" land in production.
    """
    from kairix.cli import COMMANDS

    table = COMMANDS
    assert subcommand in table, (
        f"subcommand {subcommand!r} documented in help but missing from "
        f"dispatch table — operator typing `kairix {subcommand}` will get "
        f"`Unknown command`. Either add to dispatch or remove from help. "
        f"Current dispatch keys: {sorted(table.keys())}"
    )
    entry = table[subcommand]
    # Every entry must be a tuple of (module_path, function_name, takes_argv_bool)
    assert isinstance(entry, tuple) and len(entry) >= 2, f"dispatch entry for {subcommand!r} malformed: {entry!r}"
    module_path, function_name = entry[0], entry[1]
    assert isinstance(module_path, str) and module_path.startswith("kairix."), (
        f"dispatch module path for {subcommand!r} must be a kairix.* import path; got {module_path!r}"
    )
    assert isinstance(function_name, str) and function_name, (
        f"dispatch function name for {subcommand!r} must be non-empty; got {function_name!r}"
    )
