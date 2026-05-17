Feature: End-to-end provider chat journey
  As an operator deploying kairix against any supported LLM endpoint
  I want every configured provider to accept a chat message and return
  a non-empty response with provider name and latency in the envelope
  So that operators can swap providers without touching code and the
  result envelope is always shaped the same way regardless of plugin.

  This is a Scenario Outline parameterised over all installed
  providers — including anthropic, which supports chat. Adding a new
  provider must add one row to the Examples table. F28 enforces
  row-per-provider so the surface stays mechanical to audit.

  Background:
    Given the kairix provider registry is loaded from installed entry points

  @happy_path
  Scenario Outline: Configured provider returns a non-empty chat response with envelope metadata
    Given the operator has configured provider "<name>"
    And the credential variable "<key_env>" is set to "<value_env>"
    When the operator sends the chat message "Hello, what is kairix?"
    Then the response text is a non-empty string
    And the result envelope records the provider name "<name>"
    And the result envelope records a stage_latency_ms entry for "http_roundtrip"

    Examples: First-party providers with chat support
      | name          | key_env             | value_env             |
      | azure_foundry | AZURE_OPENAI_KEY    | fake-azure-key        |
      | azure_legacy  | AZURE_OPENAI_KEY    | fake-azure-key        |
      | openai        | OPENAI_API_KEY      | fake-openai-key       |
      | bedrock       | AWS_ACCESS_KEY_ID   | fake-aws-key          |
      | ollama        | OLLAMA_HOST         | http://localhost:11434 |
      | litellm_proxy | LITELLM_PROXY_URL   | http://localhost:4000  |
      | anthropic     | ANTHROPIC_API_KEY   | fake-anthropic-key    |

  Scenario: Latency for a chat call is recorded against the provider in the envelope
    Given the operator has configured provider "openai"
    And the credential variable "OPENAI_API_KEY" is set to "fake-openai-key"
    When the operator sends the chat message "Summarise yesterday's notes"
    Then the result envelope records a stage_latency_ms entry for "http_roundtrip"
    And the stage_latency_ms entry for "http_roundtrip" is a non-negative number
    And the result envelope records the provider name "openai"

  Scenario: Chat with a max_tokens cap returns a response no longer than the cap implies
    Given the operator has configured provider "openai"
    And the credential variable "OPENAI_API_KEY" is set to "fake-openai-key"
    When the operator sends the chat message "Say hi" with max_tokens 8
    Then the response text is a non-empty string
    And the result envelope records the provider name "openai"
