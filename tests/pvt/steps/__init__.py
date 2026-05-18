"""PVT step definitions — placeholders until #284 ships the harness.

Every step in every PVT feature file currently routes to a single
``pytest.skip`` call pointing at the GitHub issue for the harness build.
This keeps the Gherkin scenarios as visible authoritative spec without
forcing a maintenance burden on step bodies that aren't yet executable.

When #284 lands, replace this module with real step bodies that drive
``MCPHttpSearchClient`` (or equivalent) against the PVT target.
"""
