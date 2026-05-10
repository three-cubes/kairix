Feature: kairix embed CLI
  As an operator running the embedding pipeline
  I want `kairix embed --help` to surface every documented subcommand and flag
  So that I can discover what's available and find typos before invoking
  a long-running embedding run.

  Scenario: --help lists every documented subcommand
    When the operator runs the embed CLI with `--help`
    Then the embed CLI exits with status 0
    And the embed help output names every subcommand

  Scenario: embed --help documents every flag
    When the operator runs the embed CLI with `embed --help`
    Then the embed CLI exits with status 0
    And the embed help output names every embed flag

  Scenario: An unknown subcommand fails with argparse usage error
    When the operator runs the embed CLI with `not-a-subcommand`
    Then the embed CLI exits with status 2
