Feature: Configurable default search scope
  As a kairix operator
  I want to mark some collections as opt-in (in_default: false)
  So that large or noisy corpora don't auto-join every default search,
  but remain reachable via an explicit --collection lookup

  Background:
    Given a kairix.config.yaml is loaded

  Scenario: A collection without in_default is in default scope
    Given the YAML declares a collection "home" with no in_default field
    When I resolve the SHARED scope for any agent
    Then "home" is included in the result

  Scenario: A collection marked in_default false is excluded from default scope
    Given the YAML declares a collection "archive" with in_default false
    When I resolve the SHARED scope for any agent
    Then "archive" is not in the result

  Scenario: An opt-in collection is excluded from every default scope
    Given the YAML declares a collection "home" with in_default true
    And the YAML declares a collection "archive" with in_default false
    And the YAML declares an agent "alpha"
    When I resolve the SHARED_AGENT scope for agent "alpha"
    Then "home" is included in the result
    And "alpha-memory" is included in the result
    And "archive" is not in the result

  Scenario: Opt-in collections remain reachable via explicit lookup
    Given the YAML declares a collection "archive" with in_default false
    When I look up the collection "archive" in the configured all-collection-names
    Then "archive" is found

  Scenario: Non-boolean in_default value is rejected at parse time
    Given the YAML declares a collection "archive" with in_default value "false-as-string"
    When I parse the collections config
    Then a ConfigValidationError is raised naming the offending key
