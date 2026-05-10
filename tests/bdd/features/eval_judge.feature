Feature: LLM-judge relevance grading
  As a kairix operator running benchmark evaluations
  I want the LLM judge to grade retrieved documents against a query
  So that I can build graded gold-standard suites and verify retrieval quality

  Scenario: Judge produces graded results when the model returns a valid response
    Given a chat backend that returns grades A=2, B=1, C=0
    And three candidate documents for the query "How do I deploy?"
    When the operator runs the LLM judge against the candidates
    Then each candidate receives its assigned grade
    And the judge model name is recorded in the result

  Scenario: Judge swallows backend errors and returns all-zero grades
    Given a chat backend that always raises a connection error
    And three candidate documents for the query "How do I deploy?"
    When the operator runs the LLM judge against the candidates
    Then every candidate receives grade 0
    And the judge call returns without raising

  Scenario: Judge clamps out-of-range grades to the 0..2 rubric
    Given a chat backend that returns grades A=5, B=-1, C=1
    And three candidate documents for the query "How do I deploy?"
    When the operator runs the LLM judge against the candidates
    Then the candidate scoring 5 is clamped to grade 2
    And the candidate scoring -1 is clamped to grade 0
    And the candidate scoring 1 keeps grade 1

  Scenario: Calibration passes when anchors return their expected grades
    Given a chat backend that answers each calibration anchor correctly
    When the operator runs the calibration sweep
    Then calibration passes

  Scenario: Calibration fails when too many anchors return wrong grades
    Given a chat backend that returns wrong grades for every calibration anchor
    When the operator runs the calibration sweep
    Then calibration raises an error so the operator can stop the run
