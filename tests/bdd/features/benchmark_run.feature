Feature: Benchmark suite execution
  As a kairix operator
  I want to run a benchmark suite and see quality gates
  So that I know if my retrieval system meets production standards

  Scenario: Benchmark produces category scores and gates
    Given a valid benchmark suite with recall and entity cases
    When the operator runs the benchmark with system "mock"
    Then the result has a weighted_total score
    And the result has category_scores for each category
    And the result has gate verdicts for phase1, phase2, and phase3

  Scenario: Perfect mock scores pass all gates
    Given a benchmark suite where all gold paths match mock results
    When the operator runs the benchmark with system "mock"
    Then all phase gates pass

  Scenario: Zero-match suite fails phase1 gate
    Given a benchmark suite where no gold paths match mock results
    When the operator runs the benchmark with system "mock"
    Then the phase1 gate fails
    And the weighted_total is below 0.62

  Scenario: NDCG cases produce hit rate and MRR metrics
    Given a benchmark suite with ndcg-scored cases
    When the operator runs the benchmark with system "mock"
    Then the result includes ndcg_at_10
    And the result includes hit_rate_at_5
    And the result includes mrr_at_10
