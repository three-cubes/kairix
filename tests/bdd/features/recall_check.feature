Feature: Adaptive recall quality check
  As a kairix operator
  I want the recall check to detect embedding quality degradation
  So that I catch silent index corruption before it affects search

  Scenario: Adaptive queries are generated from indexed documents
    Given an index with titled documents
    When the recall check builds adaptive queries
    Then at least 3 recall queries are generated
    And each query has an id, query text, and expected fragment

  Scenario: Default recall queries are used when no documents exist
    Given an empty search index
    When the recall check builds queries
    Then the default recall queries are used
    And at least 5 queries are returned

  Scenario: Recall gate alerts the operator when score drops more than 10 percent
    Given a previous recall log file recording a score of 0.90
    And a recall checker configured to return a current score of 0.70
    When the operator runs the recall gate
    Then the gate reports the run as failed
    And the alert callback is invoked exactly once
    And the alert message names the previous and current scores

  Scenario: Recall gate passes when score drops within 10 percent
    Given a previous recall log file recording a score of 0.90
    And a recall checker configured to return a current score of 0.85
    When the operator runs the recall gate
    Then the gate reports the run as passed
    And the alert callback is not invoked
