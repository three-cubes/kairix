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
    And the crawl reports 1 person found
    And the crawl reports 0 organisations found

  Scenario: store health --json emits structured output reflecting Neo4j unavailability
    When the operator runs the store CLI with `health --json`
    Then the store CLI stdout is parseable JSON
    And the store JSON "ok" field equals false
    And the store JSON "neo4j_available" field equals false
    And the store JSON "total_entities" field equals 0
    And the store JSON "issues" field contains "Neo4j unavailable"

  Scenario: store with no subcommand prints help and exits non-zero
    When the operator runs the store CLI without any subcommand
    Then the store CLI exits with status 1
    And the output names every store subcommand
