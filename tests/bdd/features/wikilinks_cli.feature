Feature: kairix wikilinks CLI
  As an operator running wikilink injection over a knowledge store
  I want `kairix wikilinks` to surface its subcommands and reject unknown ones
  So that I can discover what's available and find typos quickly.

  Scenario: With no subcommand, prints usage and exits 0
    When the operator runs the wikilinks CLI with no arguments
    Then the wikilinks CLI exits with status 0
    And the output names every wikilinks subcommand

  Scenario: --help prints usage and exits 0
    When the operator runs the wikilinks CLI with `--help`
    Then the wikilinks CLI exits with status 0
    And the output names every wikilinks subcommand

  Scenario: An unknown subcommand exits 1 and names the bad command
    When the operator runs the wikilinks CLI with `not-a-subcommand`
    Then the wikilinks CLI exits with status 1
    And stderr names the unknown wikilinks subcommand

  Scenario: status reports entity counts even when empty
    When the operator runs the wikilinks CLI with `status`
    Then the wikilinks CLI exits with status 0
    And the output reports "Entities loaded:"
