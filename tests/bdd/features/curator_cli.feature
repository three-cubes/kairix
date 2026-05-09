Feature: kairix curator CLI
  As an operator monitoring entity-graph health
  I want `kairix curator health` to expose a stable surface and emit
  a structured JSON report including the overall ok bit
  So that cron + agents can scrape the report without parsing markdown.

  Scenario: --help lists the curator subcommands
    When the operator runs the curator CLI with `--help`
    Then the curator CLI exits with status 0
    And the curator help output names the health subcommand

  Scenario: No subcommand fails with argparse usage error
    When the operator runs the curator CLI with no arguments
    Then the curator CLI exits with status 2

  Scenario: health --format json emits structured output with ok + total_entities
    When the operator runs the curator CLI with `health --format json`
    Then the curator CLI exits with status 0
    And the curator CLI stdout is parseable JSON
    And the curator JSON has an "ok" field
    And the curator JSON has a "total_entities" field
    And the curator JSON has a "neo4j_available" field
