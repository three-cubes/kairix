Feature: MCP agent entity lookup
  As an AI agent calling tool_entity
  I want to look up known entities by name
  So that I can provide accurate entity information

  Scenario: Known entity returns complete card
    Given Neo4j has entity "Alice Smith" of type "Person" with summary "Founder"
    When the agent calls tool_entity with name "Alice Smith"
    Then the entity response has name "Alice Smith"
    And the entity response has type "Person"
    And the entity response has a non-empty summary
    And the entity response error is empty

  Scenario: Unknown entity returns structured not-found
    Given Neo4j has no entity named "Nonexistent Corp"
    When the agent calls tool_entity with name "Nonexistent Corp"
    Then the entity response error contains "EntityNotFound"

  Scenario: Entity lookup never raises
    When the agent calls tool_entity with name ""
    Then no entity exception was raised
    And the entity response is a valid dict
