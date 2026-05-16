"""Catch-all PVT step shim — every step routes to pytest.skip.

The PVT scenarios in ``tests/pvt/features/*.feature`` describe the agent-
experienced behaviours we want to verify against a real running MCP server.
The harness that drives them (``MCPHttpSearchClient`` + server fixture)
lands with #284.

Until then this module satisfies pytest-bdd's step-binding requirement by
defining catch-all Given/When/Then steps that match any phrase and raise
``pytest.skip`` with a pointer to the harness-build issue. Combined with
the ``conftest.py`` autoskip on the ``pvt`` marker (the primary defence),
this is the second line of defence so even an operator who manually sets
``KAIRIX_PVT=1`` before the harness is built gets a clear skip message
rather than ``StepDefinitionNotFoundError``.
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, then, when

_HARNESS_NOT_BUILT = (
    "PVT harness not yet built — see "
    "https://github.com/three-cubes/kairix/issues/284 "
    "for the MCP HTTP client + server fixture roadmap."
)


@given(parsers.re(r".+"))
def _pvt_catchall_given() -> None:
    pytest.skip(_HARNESS_NOT_BUILT)


@when(parsers.re(r".+"))
def _pvt_catchall_when() -> None:
    pytest.skip(_HARNESS_NOT_BUILT)


@then(parsers.re(r".+"))
def _pvt_catchall_then() -> None:
    pytest.skip(_HARNESS_NOT_BUILT)
