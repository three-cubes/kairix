Feature: Warm-server baseline latency for a single agent
  As an AI agent calling tool_search against a warm kairix MCP server
  I want my queries to complete within the agent-perceived-performance budget
  So that I don't avoid the tool because of slow responses

  Background:
    Given an MCP client connected to the kairix server at the PVT target URL
    And the kairix server is warm

  Scenario: Single agent, 50 sequential search calls — p95 within budget
    When the agent makes 50 sequential tool_search calls with mixed queries
    Then the p95 latency across all calls is at most 500 milliseconds
    And the p99 latency across all calls is at most 2000 milliseconds
    And no call returns an error envelope

  Scenario: First call after warm is not penalised
    When the agent makes 1 tool_search call as the first call against the warm server
    Then the call completes within 1500 milliseconds
    And the response contains a results list

  Scenario: Empty-results queries are still fast
    When the agent makes 10 tool_search calls with queries unlikely to match anything
    Then the p95 latency across the 10 calls is at most 500 milliseconds
    And every response is a valid envelope with empty results and no error
