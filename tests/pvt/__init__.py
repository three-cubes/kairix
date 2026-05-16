"""Production-verification tests (PVT) — opt-in, real-server scenarios.

See ``docs/architecture/performance-testing-approach.md`` for the four-layer
taxonomy and why PVT is explicitly out of CI.

Scenarios run only when ``KAIRIX_PVT=1`` is set in the environment. The
``pvt`` pytest marker auto-skips otherwise. Harness behind the scenarios
(MCP HTTP client + server fixture) lands with #284; until then step defs
raise ``pytest.skip`` with the GitHub link.
"""
