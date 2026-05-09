Feature: kairix entity CLI
  As an operator managing the entity graph
  I want `kairix entity` to surface its subcommands and fail cleanly when
  the index is missing
  So that I can discover what's available and diagnose missing prerequisites.

  Scenario: --help lists every entity subcommand
    When the operator runs the entity CLI with `--help`
    Then the entity CLI exits with status 0
    And the output names every entity subcommand

  Scenario: No subcommand fails with argparse usage error
    When the operator runs the entity CLI with no arguments
    Then the entity CLI exits with status 2

  Scenario: seed --dry-run reports the missing index
    When the operator runs the entity CLI with `seed --dry-run`
    Then the entity CLI exits with status 1
    And stderr names the missing index
