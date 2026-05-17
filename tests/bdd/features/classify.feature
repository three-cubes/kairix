Feature: Classify — auto-assign collection for memory writes
  As a kairix operator writing a memory note
  I want classify to suggest the right collection based on content + agent
  So that downstream search routes the note to the right surface

  Scenario: Content with strong domain signals classifies into the matching collection
    Given a memory content "Pattern: deployment runbook update — Step 1: prepare release"
    And an explicit agent "builder"
    When the operator runs classify
    Then the classified type is non-empty
    And the classified target path includes the pattern-related filename

  Scenario: Explicit type override beats the rule-based auto-classifier
    Given a memory content with strong domain signals
    And an explicit agent "builder"
    And an explicit classification type "semantic-decision"
    When the operator resolves the target path with the explicit type
    Then the resolved target path matches the explicit type's filename

  Scenario: Classify with an unknown agent returns a structured error
    Given an agent name that is not registered
    And a memory content "Pattern: anything"
    When the operator runs classify for the unknown agent
    Then the classify result contains an error message naming the missing agent

  @error
  Scenario: Rule classifier raises ValueError — CLI exits 1 with structured error envelope
    Given classify CLI inputs content="anything" agent="builder"
    And the injected rule classifier raises ValueError
    When the operator invokes the classify CLI with --no-llm
    Then the classify CLI exits with code 1
    And the classify CLI stderr contains a structured JSON error envelope

  @error
  Scenario: Rule classifier raises a generic exception — CLI masks the message
    Given classify CLI inputs content="anything" agent="builder"
    And the injected rule classifier raises RuntimeError carrying "secret-internal-detail"
    When the operator invokes the classify CLI with --no-llm
    Then the classify CLI exits with code 1
    And the classify CLI stderr error envelope does NOT leak "secret-internal-detail"

  Scenario: LLM fallback engages when the rule classifier returns "unknown"
    Given classify CLI inputs content="ambiguous content" agent="builder"
    And the injected rule classifier returns type "unknown"
    And the injected LLM classifier returns type "semantic-decision" with reason "llm fallback hit"
    When the operator invokes the classify CLI without --no-llm
    Then the classify CLI stdout JSON has type "semantic-decision"
    And the classify CLI stdout JSON has reason "llm fallback hit"
