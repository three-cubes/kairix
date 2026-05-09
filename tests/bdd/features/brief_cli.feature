Feature: kairix brief CLI
  As an operator running per-agent session briefings
  I want `kairix brief <agent>` to validate inputs and surface clear errors
  So that I find typos and missing arguments before the briefing pipeline starts.

  Scenario: An invalid agent name is rejected with a helpful stderr
    When the operator runs the brief CLI with `not-an-agent`
    Then the brief CLI exits with status 1
    And stderr names the invalid agent
    And stderr lists the valid agent names

  Scenario: Help text lists every valid agent
    When the operator runs the brief CLI with `--help`
    Then the brief CLI exits with status 0
    And the output names every valid agent

  Scenario: A missing agent argument produces a usage error
    When the operator runs the brief CLI with no arguments
    Then the brief CLI exits with status 2
