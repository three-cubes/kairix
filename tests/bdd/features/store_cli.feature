Feature: kairix store CLI
  As an operator running document-store crawls and health checks
  I want `kairix store crawl --dry-run` and `kairix store health` to work
  without a live Neo4j connection
  So that I can verify the document store is well-formed before connecting.

  Scenario: A dry-run crawl prints counts without writing to Neo4j
    Given a document store with one entity-shaped document
    When the operator runs the store CLI with `crawl --document-root TMP --dry-run`
    Then the store CLI exits with status 0
    And the output is in dry-run mode
    And the output reports the entity counts found

  Scenario: store health --json emits structured output
    When the operator runs the store CLI with `health --json`
    Then the store CLI stdout is parseable JSON
    And the JSON has an "ok" field
    And the JSON has a "neo4j_available" field

  Scenario: store with no subcommand prints help and exits non-zero
    When the operator runs the store CLI without any subcommand
    Then the store CLI exits with status 1
    And the output names every store subcommand
