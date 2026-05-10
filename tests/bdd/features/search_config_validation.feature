Feature: Search config validation reports clear operator errors
  As a kairix operator
  I want config-validation errors that name the bad field
  So that I can fix kairix.config.yaml without reading source code

  Scenario: A retrieval override typo names the bad key
    Given an operator config with a retrieval override key 'rrfk' instead of 'rrf_k'
    When the operator runs config validation
    Then the result is non-empty
    And an error message names the offending key 'rrfk'
    And the error message lists valid override keys

  Scenario: An agent_pattern missing the placeholder is named explicitly
    Given an operator config whose agent_pattern omits the agent placeholder
    When the operator runs config validation
    Then an error message mentions agent_pattern
    And the error message names the missing placeholder

  Scenario: Two agents writing to overlapping paths are both named
    Given an operator config where agent 'alpha' and agent 'beta' write into nested paths
    When the operator runs config validation
    Then an error message says the paths overlap
    And both agent names appear in the error
