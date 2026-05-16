Feature: Session-start storm — many agents connecting at once
  As an operator running a teaming workshop or a model-spawned multi-agent task
  I want the server to absorb a burst of simultaneous agent connections
  So that no agent's first call fails or times out

  Background:
    Given an MCP client harness that can drive multiple MCP clients in parallel
    And the kairix server is warm

  Scenario: 20 agents connect simultaneously, each issues 2 queries — no failures
    When 20 MCP clients connect simultaneously to the kairix server
    And each client makes 2 tool_search calls back-to-back as soon as connected
    Then no client fails to establish a connection
    And no client receives an error envelope on either query
    And the p99 latency across all 40 calls is at most 2000 milliseconds

  Scenario: Burst peak QPS does not collapse to single-agent sequential rate
    When 20 MCP clients connect simultaneously to the kairix server
    And each client makes 2 tool_search calls back-to-back as soon as connected
    Then the peak observed queries-per-second across the burst is at least 10
