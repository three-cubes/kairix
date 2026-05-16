Feature: Teaming-load latency across multiple MCP clients
  As an operator running kairix for 5-10 agents in a session
  I want every agent to see fast responses even under realistic teaming concurrency
  So that the tool stays trusted across the session

  Background:
    Given an MCP client harness that can drive multiple MCP clients in parallel
    And the kairix server is warm

  Scenario: 10 simulated agents, 5 queries each over 60s — across-client p95 in budget
    When 10 MCP clients each make 5 tool_search calls spaced over 60 seconds
    Then the p95 latency across all 50 calls is at most 500 milliseconds
    And the p99 latency across all 50 calls is at most 2000 milliseconds
    And no client receives an error envelope
    And no Azure 429 status appears in the response diagnostics

  Scenario: Per-client latency does not diverge across the session
    When 10 MCP clients each make 5 tool_search calls spaced over 60 seconds
    Then the per-client p95 of the slowest client is within 50 percent of the per-client p95 of the fastest client

  Scenario: Server-reported mean concurrency tracks requested concurrency
    When 10 MCP clients each make 5 tool_search calls spaced over 60 seconds
    Then the server's reported mean active concurrency at peak is at least 60 percent of 10
