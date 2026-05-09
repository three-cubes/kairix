Feature: kairix top-level CLI dispatch
  As an operator interacting with the kairix command-line entry point
  I want `--help`, `--version`, and unknown commands to behave predictably
  So that scripts wrapping kairix can exit cleanly and print actionable text.

  Scenario: --help prints the subcommand list and exits 0
    When the operator invokes the kairix entry point with `--help`
    Then the kairix CLI exits with status 0
    And the output names every documented subcommand

  Scenario: -h is a synonym for --help
    When the operator invokes the kairix entry point with `-h`
    Then the kairix CLI exits with status 0
    And the output names every documented subcommand

  Scenario: --version prints the package version and exits 0
    When the operator invokes the kairix entry point with `--version`
    Then the kairix CLI exits with status 0
    And the output starts with "kairix "

  Scenario: An unknown subcommand exits non-zero with operator-actionable text
    When the operator invokes the kairix entry point with `not-a-command`
    Then the kairix CLI exits with status 1
    And stderr names the unknown command
    And stderr lists the documented subcommands
