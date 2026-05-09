Feature: kairix timeline CLI
  As an operator running temporal queries over the knowledge store
  I want `kairix timeline` to validate inputs and surface clear errors
  So that bad date arguments fail fast rather than during search.

  Scenario: --help lists every documented flag
    When the operator runs the timeline CLI with `--help`
    Then the timeline CLI exits with status 0
    And the timeline help output names every documented flag

  Scenario: Missing query argument fails with argparse usage error
    When the operator runs the timeline CLI with no arguments
    Then the timeline CLI exits with status 2

  Scenario: Invalid --since date is rejected with a clear error
    When the operator runs the timeline CLI with `"some query" --since not-a-date`
    Then the timeline CLI exits with status 1
    And stderr names the invalid since date

  Scenario: Invalid --type choice rejected by argparse
    When the operator runs the timeline CLI with `"some query" --type bogus`
    Then the timeline CLI exits with status 2
    And stderr names the bad type choice
