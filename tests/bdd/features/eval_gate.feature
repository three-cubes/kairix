Feature: Quality gate (KFEAT-013, stage 5)
  As an operator finishing the kairix onboarding flow
  I want the eval gate to tell me whether my tuned config is good enough
  So that I know whether to ship or to apply more recommendations

  Scenario: Every category at or above floor produces a PASS
    Given a benchmark result with all categories at 0.80
    When I run the quality gate with floor 0.50
    Then the verdict is "pass"
    And no weak categories are reported
    And no gate recommendations are produced

  Scenario: One category below floor produces a HOLD with recommendations
    Given a benchmark result where temporal is 0.30 with date-named corpus and others are 0.80
    When I run the quality gate with floor 0.50
    Then the verdict is "hold"
    And "temporal" is reported as a weak gate category
    And at least one gate recommendation targets "temporal"

  Scenario: Multiple weak categories all generate recommendations
    Given a benchmark result where temporal is 0.20 and conceptual is 0.30 with date-named corpus and others are 0.80
    When I run the quality gate with floor 0.50
    Then the verdict is "hold"
    And both "temporal" and "conceptual" are weak gate categories
    And gate recommendations exist for both

  Scenario: Recommendations are gated by corpus hints
    Given a benchmark result where temporal is 0.20 with no date-named corpus and others are 0.80
    When I run the quality gate with floor 0.50
    Then the verdict is "hold"
    And "temporal" is a weak gate category
    But no gate recommendation targets "temporal" with date_path_boost

  Scenario: Floor controls strictness
    Given a benchmark result where every category is exactly 0.55
    When I run the quality gate with floor 0.60
    Then the verdict is "hold"
    When I run the quality gate with floor 0.50
    Then the verdict is "pass"

  Scenario: Output formatting renders verdict and category breakdown
    Given a benchmark result where recall is 0.90 and procedural is 0.40 with procedural-doc corpus
    When I run the quality gate with floor 0.50
    Then the gate output contains the verdict label
    And the gate output contains every category score
    And the gate output contains the recommendation for procedural
