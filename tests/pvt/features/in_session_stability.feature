Feature: In-session stability for one long-lived agent
  As an AI agent in a long-running session that calls kairix periodically
  I want my call-30 to be as fast as my call-1
  So that I can rely on kairix throughout a task, not just at session start

  Background:
    Given an MCP client connected to the kairix server at the PVT target URL
    And the kairix server is warm

  Scenario: 30 queries spaced 1s apart over 30s — no latency drift
    When the agent makes 30 tool_search calls spaced 1 second apart
    Then the latency of call 30 is within 20 percent of the latency of call 1
    And no call returns an error envelope

  Scenario: Memory and fd counts stay stable across the session
    When the agent makes 30 tool_search calls spaced 1 second apart
    Then the server's reported RSS at call 30 is within 50 megabytes of RSS at call 1
    And the server's reported open file descriptor count does not increase by more than 5 across the session

  Scenario: Cache hit rate climbs across repeated queries
    When the agent makes 30 tool_search calls of which 20 are repeats of the first 10
    Then the server reports a non-zero cache hit rate
    And the p95 latency of the repeat calls is at most 70 percent of the p95 latency of the unique calls
