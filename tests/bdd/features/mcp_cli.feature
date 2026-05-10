Feature: kairix mcp CLI
  As an operator launching the kairix MCP server
  I want `kairix mcp --help` and argument validation to surface the
  documented options before the server tries to bind a port
  So that typos and missing flags fail fast with a clear message.

  Scenario: --help lists the serve subcommand
    When the operator runs the mcp CLI with `--help`
    Then the mcp CLI exits with status 0
    And the help output names the serve subcommand

  Scenario: serve --help documents every transport choice
    When the operator runs the mcp CLI with `serve --help`
    Then the mcp CLI exits with status 0
    And the help output names every transport choice

  Scenario: No subcommand prints help and exits non-zero
    When the operator runs the mcp CLI with no arguments
    Then the mcp CLI exits with status 1
    And the output names the serve subcommand

  Scenario: serve rejects an unknown transport via argparse
    When the operator runs the mcp CLI with `serve --transport not-a-transport`
    Then the mcp CLI exits with status 2
    And stderr names the bad transport
