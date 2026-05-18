Feature: Anthropic provider plugin pins the api.anthropic.com wire shape
  As an operator routing kairix chat traffic through Anthropic
  I want the anthropic plugin to authenticate via the x-api-key header,
  to declare an anthropic-version, to target the messages endpoint, and
  to refuse embed calls with a typed EmbedNotSupported error rather
  than silently falling back
  So that operators who pick Anthropic see a clear typed error on the
  surface that does not exist on this provider, and so the chat path
  uses Anthropic's actual auth model instead of misapplying Bearer.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the anthropic provider configured with model "claude-3-7-sonnet"
    And the configured credential resolver returns the api key "anthropic-test-key"

  @happy_path
  Scenario: An Anthropic chat request uses x-api-key plus anthropic-version on /v1/messages
    Given the configured endpoint is "https://api.anthropic.com"
    When the operator runs a single chat completion via the anthropic plugin
    Then the recorded request host is "api.anthropic.com"
    And the recorded request path equals "/v1/messages"
    And the recorded request header "x-api-key" equals "anthropic-test-key"
    And the recorded request has no header named "Authorization"
    And the recorded request header "anthropic-version" is set
    And the recorded request body contains model "claude-3-7-sonnet"

  @error
  Scenario: Calling embed on the anthropic plugin raises a typed EmbedNotSupported error
    Given the configured endpoint is "https://api.anthropic.com"
    When the operator embeds a single text via the anthropic plugin
    Then the anthropic plugin raises a canonical EmbedNotSupported error
    And the error message names the configured provider as "anthropic"
    And no outbound request was recorded by the wire endpoint

  @error
  Scenario: Anthropic returning 429 on chat maps to a canonical RateLimited error
    Given the configured endpoint is "https://api.anthropic.com"
    And the wire endpoint will respond with status 429 and a Retry-After header
    When the operator runs a single chat completion via the anthropic plugin
    Then the anthropic plugin raises a canonical RateLimited error

  @error
  Scenario: Anthropic returning 401 on chat maps to a canonical AuthError
    Given the configured endpoint is "https://api.anthropic.com"
    And the wire endpoint will respond with status 401
    When the operator runs a single chat completion via the anthropic plugin
    Then the anthropic plugin raises a canonical AuthError
    And the error message names the configured provider as "anthropic"
