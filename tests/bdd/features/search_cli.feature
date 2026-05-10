Feature: kairix search CLI
  As an operator running ad-hoc retrieval queries
  I want `kairix search "..."` to print classified intent, latency, and matching results
  So that I can verify the search pipeline against my document store
  And pipe JSON output into other tools when needed.

  Scenario: A successful search prints the intent and the top result
    Given a fake pipeline that returns one result for "alpha"
    When the operator runs `kairix search "alpha"`
    Then the CLI exits with status 0
    And the human-readable output names the classified intent
    And the human-readable output includes the result path

  Scenario: --json emits a machine-parseable JSON object with results array
    Given a fake pipeline that returns one result for "alpha"
    When the operator runs `kairix search "alpha" --json`
    Then the CLI exits with status 0
    And stdout is parseable JSON
    And the JSON has a "results" array of length 1
    And the JSON's top-level "intent" equals the pipeline's classified intent

  Scenario: --limit caps the number of results in the output
    Given a fake pipeline that returns 5 results for "alpha"
    When the operator runs `kairix search "alpha" --json --limit 2`
    Then the CLI exits with status 0
    And the JSON's "results" array has length 2

  Scenario: A pipeline error is reported and exits non-zero
    Given a fake pipeline that returns an error for "alpha"
    When the operator runs `kairix search "alpha" --json`
    Then the CLI exits with status 1
    And the JSON has an "error" field with operator-actionable text
