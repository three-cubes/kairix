Feature: Probe — concurrent-load latency measurement
  As a kairix operator
  I want probe search and probe burst to surface latency / throughput signals
  So that I can pull the right Tier 1 tuning lever before changing config

  Scenario: probe search at low concurrency reports p50/p95/p99 stats
    Given a fake search client returning results in under 50ms
    When the operator runs probe search with 30 queries at concurrency 2
    Then the probe result is passed
    And the overall p95 is under the threshold
    And the per-category map covers every default-weighted category

  Scenario: probe search fires the bottleneck heuristic when p95 exceeds threshold
    Given a fake search client returning results in 600ms (above threshold)
    When the operator runs probe search with 8 queries at concurrency 4
    Then the probe result is not passed
    And the bottleneck recommendation names a kind
    And the bottleneck recommendation includes the failing p95 figure

  Scenario: probe search seed determinism reproduces the same sampled queries
    Given two probe search runs with the same seed
    When the operator captures the case_ids each run executed
    Then both runs executed the same set of case_ids

  Scenario: probe burst surfaces queries-per-second buckets
    Given a fake search client returning results in under 20ms
    When the operator runs probe burst with 30 total queries at peak concurrency 5
    Then the burst result has at least 1 bucket
    And the sum of queries_completed across buckets equals 30
    And peak_qps is greater than zero
