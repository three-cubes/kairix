Feature: Agent bootstrap — orientation envelope on session start
  As an AI agent connecting to kairix at the start of a session
  I want a structured orientation envelope (role, board, recent memory, goals, health)
  So that I can ground my first actions in the right context

  Scenario: Bootstrap returns the canonical orientation envelope for a known agent
    Given a known agent named "shape"
    When the agent calls bootstrap for the agent
    Then the envelope contains a role field
    And the envelope contains a board field
    And the envelope contains a recent_memory section
    And the envelope contains a goals section
    And the envelope contains a health summary

  Scenario: Bootstrap with a missing document root returns a structured error
    Given a document root that does not exist on disk
    When the agent calls bootstrap for any agent
    Then the envelope carries a non-empty error field
    And the envelope carries a remediation directive
    And the envelope does not raise an exception

  Scenario: Bootstrap envelope is JSON-serialisable
    Given a known agent named "shape"
    When the agent calls bootstrap for the agent
    Then the envelope round-trips through json.dumps and json.loads cleanly
