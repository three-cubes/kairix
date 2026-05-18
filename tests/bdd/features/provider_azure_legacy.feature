Feature: Azure legacy provider plugin pins the older AzureOpenAI wire shape
  As an operator whose tenancy still exposes the legacy
  https://<resource>.openai.azure.com endpoint shape
  I want the azure_legacy plugin to use the AzureOpenAI client style,
  including the api-version query parameter, and to map auth and
  rate-limit errors to the same canonical types as the other plugins
  So that legacy and foundry tenants share the same error contract and
  swapping between them is a configuration change, not a code change.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the azure_legacy provider configured with deployment "text-embedding-3-large"
    And the configured credential resolver returns the api key "legacy-test-key"

  @happy_path
  Scenario: A legacy endpoint sends an api-version query parameter
    Given the configured endpoint is "https://example-resource.openai.azure.com"
    When the operator embeds a single text via the azure_legacy plugin
    Then the recorded request host is "example-resource.openai.azure.com"
    And the recorded request path does not contain "/openai/v1"
    And the recorded request query contains the parameter "api-version"
    And the recorded request header "api-key" equals "legacy-test-key"

  Scenario: The api-version parameter defaults to the value pinned by the ADR
    Given the configured endpoint is "https://example-resource.openai.azure.com"
    And no operator override for the api-version parameter
    When the operator embeds a single text via the azure_legacy plugin
    Then the recorded request query "api-version" equals the ADR default api-version

  Scenario: An operator override for api-version is honoured on the wire
    Given the configured endpoint is "https://example-resource.openai.azure.com"
    And the operator override for api-version is "2024-02-01"
    When the operator embeds a single text via the azure_legacy plugin
    Then the recorded request query "api-version" equals "2024-02-01"

  @error
  Scenario: Legacy endpoint returning 429 maps to a canonical RateLimited error
    Given the configured endpoint is "https://example-resource.openai.azure.com"
    And the wire endpoint will respond with status 429 and a Retry-After header
    When the operator embeds a single text via the azure_legacy plugin
    Then the azure_legacy plugin raises a canonical RateLimited error
    And the error carries the upstream retry-after hint

  @error
  Scenario: Legacy endpoint returning 401 maps to a canonical AuthError
    Given the configured endpoint is "https://example-resource.openai.azure.com"
    And the wire endpoint will respond with status 401
    When the operator embeds a single text via the azure_legacy plugin
    Then the azure_legacy plugin raises a canonical AuthError
    And the error message names the configured provider as "azure_legacy"
