Feature: Operator probes their configured provider for health and tuning
  As a kairix operator who has just configured an LLM/embed provider
  I want to run "kairix probe-config" against my own deployment
  So that I can confirm the setup is healthy and get tuning advice
  specific to my endpoint distance and latency tail

  Scenario: Healthy provider yields a clean report and exit code zero
    Given a configured provider that responds within 100ms on every call
    When the operator runs "kairix probe-config"
    Then the JSON report has status "healthy"
    And the JSON report has a cold_ms timing
    And the JSON report has a warm_p50_ms timing
    And the JSON report has a warm_p95_ms timing
    And the JSON report has a coalesce_ratio between zero and one
    And the JSON report has a cache_hit_rate between zero and one
    And the JSON report tuning_recommendations list is empty
    And the process exits with code 0

  Scenario: Degraded provider yields tuning advice and a non-zero exit
    Given a configured provider whose responses exceed 2 seconds
    When the operator runs "kairix probe-config"
    Then the JSON report has status "degraded"
    And the JSON report tuning_recommendations contains advice to increase pool_size or decrease coalesce_window_ms
    And the process exits with code 1

  Scenario: Unreachable provider is flagged with an error
    Given a configured provider that errors on every call
    When the operator runs "kairix probe-config"
    Then the JSON report has status "unreachable"
    And the JSON report error field is populated
    And the process exits with code 2

  Scenario: High coalesce ratio recommends a smaller coalesce window
    Given a configured provider whose coalescer fires for solo requests
    When the operator runs "kairix probe-config"
    Then the JSON report tuning_recommendations contains advice to decrease coalesce_window_ms

  Scenario: Low cache hit rate recommends a larger cache
    Given a configured provider under a repeated-query workload
    And the observed cache hit rate is below five percent
    When the operator runs "kairix probe-config"
    Then the JSON report tuning_recommendations contains advice to increase cache_max_entries

  Scenario: Baseline comparison flags regressions over twenty percent
    Given the operator has a previous probe-config JSON report saved as baseline.json
    And the current run is more than twenty percent slower at any stage
    When the operator runs "kairix probe-config --compare baseline.json"
    Then the JSON report comparison section lists each regressed stage
    And each flagged stage shows the percentage slower than baseline

  Scenario Outline: Stage timings vary across providers but the report schema is identical
    Given a configured provider named "<provider>"
    When the operator runs "kairix probe-config"
    Then the JSON report stage_latency_ms section is present
    And the JSON report stage_latency_ms contains pool_acquire
    And the JSON report stage_latency_ms contains coalesce_wait
    And the JSON report stage_latency_ms contains cache_lookup
    And the JSON report stage_latency_ms contains http_roundtrip
    And the JSON report stage_latency_ms contains response_parse
    And no provider-specific fields appear in the JSON report

    Examples:
      | provider       |
      | azure_foundry  |
      | openai         |
      | ollama         |
