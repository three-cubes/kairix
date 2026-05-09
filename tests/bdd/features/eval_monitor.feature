Feature: Eval monitor regression detection
  As a kairix operator
  I want a daily monitor run to flag retrieval-quality regressions and
  attribute infrastructure failures (e.g. vector-search outages)
  So that I notice silent corruption or backend issues before they reach users

  Background:
    Given an injected suite loader returning 5 recall cases
    And an injected benchmark runner that returns deterministic scores

  Scenario: First run with no previous log records does not flag a regression
    Given no previous monitor log entries
    And the benchmark runner returns weighted_total 0.75
    When the operator runs the monitor
    Then the result reports regression as False
    And the result reports regression_detail as None

  Scenario: A drop beyond the alert threshold flags regression and names the baseline
    Given a previous monitor log with weighted_ndcg 0.80 from one day ago
    And the benchmark runner returns weighted_total 0.50
    And an alert threshold of 0.05
    When the operator runs the monitor
    Then the result reports regression as True
    And the regression_detail names the baseline and the drop amount

  Scenario: A drop within the alert threshold does not flag a regression
    Given a previous monitor log with weighted_ndcg 0.80 from one day ago
    And the benchmark runner returns weighted_total 0.78
    And an alert threshold of 0.05
    When the operator runs the monitor
    Then the result reports regression as False

  Scenario: vec_failed_count reflects cases whose retrieval reported vector-search failure
    Given the benchmark runner returns 4 case results, 3 of which carry vec_failed True
    When the operator runs the monitor
    Then the result reports vec_failed_count as 3

  Scenario: Each run appends an entry to the JSONL log
    Given an empty monitor log file
    And the benchmark runner returns weighted_total 0.72
    When the operator runs the monitor 3 times
    Then the monitor log file contains 3 JSONL entries
