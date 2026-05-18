Feature: Usage guide — agent-facing capability documentation
  As an AI agent unsure which kairix tool fits the task at hand
  I want a guide that I can query by topic
  So that I get the right tool affordance instead of guessing

  Scenario: Empty topic returns the full guide
    Given a usage guide fixture with multiple sections
    When the agent requests the usage guide with no topic
    Then the response contains the full guide text
    And the response includes a capabilities-section reference

  Scenario: Known topic returns a focused slice
    Given a usage guide fixture with multiple sections
    And a known guide topic "search"
    When the agent requests the usage guide with that topic
    Then the response is shorter than the full guide
    And the response mentions the topic name

  Scenario: Unknown topic returns the fallback orientation slice
    Given a usage guide fixture with multiple sections
    And a guide topic that does not exist
    When the agent requests the usage guide with that topic
    Then the response contains the fallback orientation slice
    And the fallback slice references at least one valid topic
