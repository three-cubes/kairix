Feature: OpenAI-direct provider plugin pins the openai.com wire shape
  As an operator pointing kairix at api.openai.com
  I want the openai plugin to send a Bearer-token request to the
  configured base URL and to map 4xx and 5xx responses to canonical
  typed errors
  So that callers in the rest of kairix can match one error type per
  failure class, regardless of which provider is loaded.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the openai provider configured with model "text-embedding-3-large"
    And the configured credential resolver returns the api key "sk-test-openai"

  @happy_path
  Scenario: A configured api.openai.com base URL is sent through unchanged
    Given the configured endpoint is "https://api.openai.com/v1"
    When the operator embeds a single text via the openai plugin
    Then the recorded request host is "api.openai.com"
    And the recorded request path begins with "/v1/"
    And the recorded request header "Authorization" equals "Bearer sk-test-openai"
    And the recorded request body contains model "text-embedding-3-large"

  @error
  Scenario: OpenAI returning 429 maps to a canonical RateLimited error
    Given the configured endpoint is "https://api.openai.com/v1"
    And the wire endpoint will respond with status 429 and a Retry-After header
    When the operator embeds a single text via the openai plugin
    Then the openai plugin raises a canonical RateLimited error
    And the error carries the upstream retry-after hint

  @error
  Scenario: OpenAI returning 401 maps to a canonical AuthError
    Given the configured endpoint is "https://api.openai.com/v1"
    And the wire endpoint will respond with status 401
    When the operator embeds a single text via the openai plugin
    Then the openai plugin raises a canonical AuthError
    And the error message names the configured provider as "openai"

  @error
  Scenario: OpenAI returning 500 maps to a canonical UpstreamError
    Given the configured endpoint is "https://api.openai.com/v1"
    And the wire endpoint will respond with status 500
    When the operator embeds a single text via the openai plugin
    Then the openai plugin raises a canonical UpstreamError
    And the error message names the configured provider as "openai"
