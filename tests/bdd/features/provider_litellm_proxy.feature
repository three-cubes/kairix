Feature: LiteLLM proxy provider plugin pins the proxy-sidecar wire shape
  As an operator who runs a LiteLLM proxy in front of N upstream LLMs
  I want the litellm_proxy plugin to send Bearer-token requests in
  OpenAI shape to the configured proxy URL and to map proxy errors via
  the same canonical types as the openai plugin
  So that swapping a direct OpenAI integration for a proxied one is a
  configuration change, not a behaviour change for kairix callers.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the litellm_proxy provider configured with model "text-embedding-3-large"
    And the configured credential resolver returns the virtual key "sk-litellm-test-vk"

  @happy_path
  Scenario: A LiteLLM proxy request sends Bearer auth and OpenAI-shape body
    Given the configured endpoint is "http://localhost:4000/v1"
    When the operator embeds a single text via the litellm_proxy plugin
    Then the recorded request host is "localhost:4000"
    And the recorded request path begins with "/v1/"
    And the recorded request header "Authorization" equals "Bearer sk-litellm-test-vk"
    And the recorded request body contains model "text-embedding-3-large"

  Scenario: An operator-configured proxy URL is honoured verbatim
    Given the configured endpoint is "http://proxy.internal:8000/v1"
    When the operator embeds a single text via the litellm_proxy plugin
    Then the recorded request host is "proxy.internal:8000"
    And the recorded request path begins with "/v1/"

  @error
  Scenario: LiteLLM proxy returning 429 maps to a canonical RateLimited error
    Given the configured endpoint is "http://localhost:4000/v1"
    And the wire endpoint will respond with status 429 and a Retry-After header
    When the operator embeds a single text via the litellm_proxy plugin
    Then the litellm_proxy plugin raises a canonical RateLimited error

  @error
  Scenario: LiteLLM proxy returning 401 maps to a canonical AuthError
    Given the configured endpoint is "http://localhost:4000/v1"
    And the wire endpoint will respond with status 401
    When the operator embeds a single text via the litellm_proxy plugin
    Then the litellm_proxy plugin raises a canonical AuthError
    And the error message names the configured provider as "litellm_proxy"
