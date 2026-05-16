Feature: Agent cold-start experience over MCP
  As an AI agent calling kairix tools for the first time after a deploy
  I want an immediate structured response when the server isn't yet warm
  So that I don't commit "kairix is flaky" to my memory because of a startup race

  Background:
    Given an MCP client connected to the kairix server at the PVT target URL

  Scenario: Cold server returns ColdStart envelope, not a hang
    Given the kairix container has just started and is still warming
    When the agent calls tool_search with query "anything"
    Then the response arrives within 100 milliseconds
    And the response error is "ColdStart"
    And the response includes estimated_seconds_remaining
    And the response guidance names a concrete retry interval

  Scenario: ColdStart envelope identifies the tool the agent called
    Given the kairix container has just started and is still warming
    When the agent calls tool_prep with query "anything"
    Then the response error is "ColdStart"
    And the response tool field is "prep"

  Scenario: ColdStart points the agent at the runbook
    Given the kairix container has just started and is still warming
    When the agent calls tool_search with query "anything"
    Then the response see_also contains the retrieval-health runbook reference
