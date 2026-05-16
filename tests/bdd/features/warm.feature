Feature: Warm — preload kairix caches before agent traffic
  As a kairix operator (or container entrypoint)
  I want warm to pay factory-build + spaCy + Azure-pool init costs eagerly
  So that the first agent call doesn't hit a cold pipeline

  Scenario: Warm reports per-step success in the envelope
    Given fake warm steps that all succeed
    When the operator runs warm
    Then warm reports ok=True
    And the envelope contains a step per registered warm-up phase
    And every step's ok flag is True

  Scenario: Warm continues past a failing step and reports the failure
    Given fake warm steps where one fails with detail "boom"
    When the operator runs warm
    Then warm reports ok=False
    And the failures list contains the failing step name
    And the failures list detail mentions "boom"
    And subsequent step records still appear in the envelope

  Scenario: Warm is idempotent — second call after success is cheap
    Given a warm-up that has already completed successfully
    When the operator runs warm again
    Then warm reports ok=True
    And the second call's total_duration_s is at most one-tenth of the first call's
