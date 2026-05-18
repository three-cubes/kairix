Feature: Probe — per-query stage telemetry for tail-latency root cause analysis
  As a kairix operator
  I want the probe envelope to carry per-query stage records and split the vector stage
  So that I can rank the slow tail queries and tell Azure HTTP tail apart from local ANN cost

  Scenario: Per-query stage records appear in the probe envelope
    Given a stage-aware fake search client returning a per-query stage map
    When the operator runs the probe with 6 queries at concurrency 2
    Then the probe envelope contains a per_query_stages list
    And each per_query_stages record carries case_id and category and latency_ms and stage_latency_ms

  Scenario: Vector stage is split into embed_http and vector_ann
    Given a real search pipeline composed from canonical fakes
    When the operator runs the probe with 5 queries at concurrency 1
    Then every per-query stage map contains embed_http and vector_ann
    And the sum of embed_http and vector_ann approximates the vector total

  Scenario: Slow queries surface in per-query records
    Given a fake search client that returns one slow query and many fast queries
    When the operator runs the probe with 20 queries at concurrency 4
    Then the slow query latency is visibly higher than every fast query latency
