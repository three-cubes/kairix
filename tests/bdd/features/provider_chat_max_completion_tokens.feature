Feature: Reasoning-class chat models receive max_completion_tokens
  As an operator switching kairix's chat backend to a reasoning-class model
  (gpt-5.x, o1-x, o3-x) on Azure Foundry
  I want the azure_foundry provider to translate kairix's public ``max_tokens``
  kwarg to the model-specific ``max_completion_tokens`` wire parameter
  So that I can swap in the newer deployment by changing config alone,
  without a kairix code change and without seeing a 400 from Azure that
  says "Unsupported parameter: 'max_tokens' is not supported with this model".

  Background:
    Given a recording transport client that captures every chat.completions.create call
    And the configured credential resolver returns the api key "foundry-test-key"

  @happy_path
  Scenario: A gpt-5 deployment receives max_completion_tokens on the wire
    Given the foundry chat provider is configured against deployment "gpt-5.4-mini"
    When the operator invokes the foundry chat method with max_tokens 250
    Then the recorded chat.completions.create call carries max_completion_tokens 250
    And the recorded chat.completions.create call does not carry max_tokens

  Scenario: An o1 deployment receives max_completion_tokens on the wire
    Given the foundry chat provider is configured against deployment "o1-mini"
    When the operator invokes the foundry chat method with max_tokens 100
    Then the recorded chat.completions.create call carries max_completion_tokens 100
    And the recorded chat.completions.create call does not carry max_tokens

  Scenario: An o3 deployment receives max_completion_tokens on the wire
    Given the foundry chat provider is configured against deployment "o3-mini"
    When the operator invokes the foundry chat method with max_tokens 750
    Then the recorded chat.completions.create call carries max_completion_tokens 750
    And the recorded chat.completions.create call does not carry max_tokens

  Scenario: A gpt-4o-mini deployment continues to receive max_tokens on the wire
    Given the foundry chat provider is configured against deployment "gpt-4o-mini"
    When the operator invokes the foundry chat method with max_tokens 500
    Then the recorded chat.completions.create call carries max_tokens 500
    And the recorded chat.completions.create call does not carry max_completion_tokens

  Scenario: The kairix public surface keeps max_tokens as the kwarg the caller passes
    Given the foundry chat provider is configured against deployment "gpt-5.4-mini"
    When the operator invokes the foundry chat method with max_tokens 1234
    Then the foundry chat method's public signature still accepts the kwarg "max_tokens"
    And the foundry chat method's public signature does not accept the kwarg "max_completion_tokens"
