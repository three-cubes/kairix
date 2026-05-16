Feature: Soak test — does kairix hold together under repeated load?
  As a kairix operator
  I want soak to assert per-iteration RSS / log-volume / fd / signature stability
  So that scale-fragile regressions surface before they reach production

  Scenario: Soak passes when the workload is deterministic across repeats
    Given a workload that returns the same envelope on every call
    When the operator runs soak with repeat 3
    Then soak passes
    And every iteration has a measurement record

  Scenario: Soak fires the memory_growth gate when RSS climbs past iter-0
    Given a workload that grows RSS by 200 MB on every iteration after the first
    When the operator runs soak with repeat 3
    Then soak fails
    And the failure kind is "memory_growth"
    And the failure mentions the iteration that breached the cap

  Scenario: Soak fires the signature_mismatch gate when the workload drifts
    Given a workload that returns different envelopes on each call
    When the operator runs soak with repeat 2
    Then soak fails
    And the failure kind is "signature_mismatch"

  Scenario: Soak CLI failure output carries F21 affordance markers
    Given a workload that fires the memory_growth gate
    When the operator invokes the soak CLI with repeat 3
    Then the stderr or stdout contains "fix:"
    And the stderr or stdout contains "next:"
