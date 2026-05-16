Feature: Worker — background maintenance loop with operator controls
  As a kairix operator
  I want worker status / pause / resume to reflect state file changes
  So that I can quiesce the maintenance loop during incidents without restarting

  Scenario: Worker status returns the structured envelope
    Given a worker state file with phase "idle" and last_run "2026-05-17T00:00:00Z"
    When the operator runs worker status
    Then the status envelope contains a phase field
    And the status envelope contains a last_run timestamp
    And the status envelope contains a last_error field

  Scenario: Worker pause writes the pause flag to the state file
    Given a worker state file with phase "idle"
    When the operator runs worker pause
    Then the state file's paused flag is True

  Scenario: Worker resume clears the pause flag
    Given a worker state file with paused flag True
    When the operator runs worker resume
    Then the state file's paused flag is False
