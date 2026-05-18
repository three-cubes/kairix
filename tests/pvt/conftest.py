"""PVT conftest — autoskip unless KAIRIX_PVT=1.

The PVT layer measures agent-experienced behaviour against a real running
MCP server (see ``docs/architecture/performance-testing-approach.md``).
It's intentionally NOT a CI gate — agents fire it on-demand against the
deployed VM, or the release pipeline fires it post-deploy.

Until the MCP HTTP client + server fixture ship (tracked in #284), the
step definitions raise ``pytest.skip`` with a pointer. The Gherkin
scenarios still live in this tree so the production-truth contract is
captured as repo-resident spec.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``pvt``-marked test unless the operator explicitly opts in.

    Reads ``KAIRIX_PVT=1`` from the environment. Anything else (unset,
    empty string, "0") leaves the marker honoured and the test skipped.
    """
    if os.environ.get("KAIRIX_PVT") == "1":
        return
    skip_marker = pytest.mark.skip(
        reason=("PVT scenarios run only with KAIRIX_PVT=1; see docs/architecture/performance-testing-approach.md")
    )
    for item in items:
        if "pvt" in item.keywords:
            item.add_marker(skip_marker)
