Feature: Azure Foundry provider plugin pins the wire shape Foundry expects
  As an operator who picked Azure Foundry as the kairix endpoint
  I want the foundry plugin to send requests to the right URL, with the
  right auth header, and to map Foundry's error responses to canonical
  typed errors
  So that switching providers does not change how the rest of kairix
  reasons about failure modes, and so a misconfigured endpoint surfaces
  a clear actionable error instead of a generic 4xx blob.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the azure_foundry provider configured with deployment "text-embedding-3-large"
    And the configured credential resolver returns the api key "foundry-test-key"

  @happy_path
  Scenario: A configured Foundry endpoint gets the /openai/v1 suffix appended
    Given the configured endpoint is "https://example-resource.services.ai.azure.com"
    When the operator embeds a single text via the foundry plugin
    Then the recorded request path begins with "/openai/v1/"
    And the recorded request host is "example-resource.services.ai.azure.com"
    And the recorded request header "api-key" equals "foundry-test-key"
    And the recorded request body contains model "text-embedding-3-large"

  Scenario: An endpoint already including /openai/v1 is not double-suffixed
    Given the configured endpoint is "https://example-resource.services.ai.azure.com/openai/v1"
    When the operator embeds a single text via the foundry plugin
    Then the recorded request path begins with "/openai/v1/"
    And the recorded request path does not contain "/openai/v1/openai/v1"

  @error
  Scenario: Foundry returning 429 maps to a canonical typed RateLimited error
    Given the configured endpoint is "https://example-resource.services.ai.azure.com"
    And the wire endpoint will respond with status 429 and a Retry-After header
    When the operator embeds a single text via the foundry plugin
    Then the foundry plugin raises a canonical RateLimited error
    And the error carries the upstream retry-after hint

  @error
  Scenario: Foundry returning 401 maps to a canonical typed AuthError
    Given the configured endpoint is "https://example-resource.services.ai.azure.com"
    And the wire endpoint will respond with status 401
    When the operator embeds a single text via the foundry plugin
    Then the foundry plugin raises a canonical AuthError
    And the error message names the configured provider as "azure_foundry"

  Scenario: The configured deployment name flows through as the model parameter
    Given the configured endpoint is "https://example-resource.services.ai.azure.com"
    And the azure_foundry provider configured with deployment "text-embedding-3-small"
    When the operator embeds a single text via the foundry plugin
    Then the recorded request body contains model "text-embedding-3-small"
