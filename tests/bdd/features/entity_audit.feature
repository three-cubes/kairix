Feature: kairix entity audit and purge
  As an operator hygiene-checking the entity graph
  I want a single `kairix entity audit` command that emits a structured report
  And a `kairix entity purge` command that consumes it
  So that I can replace the six-command runbook with two purposeful commands.

  Scenario: audit emits a JSON report containing the documented shape
    Given an entity graph with no audit findings
    When the operator runs `kairix entity audit --mode all --format json`
    Then the audit CLI exits with status 0
    And the audit JSON output names mode, generated_at, total, and rows

  Scenario: purge dry-run reads the audit report and previews the deletes
    Given an audit report file with one entity row
    When the operator runs purge with `--dry-run` against the audit report
    Then the audit CLI exits with status 0
    And the purge output names DRY-RUN and the row id
    And the graph receives no Cypher calls
