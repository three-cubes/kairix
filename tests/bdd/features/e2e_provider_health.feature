Feature: Operator runs probe-config to verify provider health
  As an operator who just deployed kairix against my own endpoint
  I want a single command that runs a representative workload against
  the configured provider and emits a JSON health report
  So that I can share that report when opening a support issue and
  the recommended tuning is right for my endpoint distance.

  The JSON schema is defined by SK-7 at
  ``docs/architecture/probe-config-schema.md``. This feature describes
  the operator-visible behaviour of the ``kairix probe-config`` CLI;
  step impls (Wave 2 IM-9) read the schema for field-level assertions.

  Background:
    Given the kairix provider registry is loaded from installed entry points
    And the operator has configured provider "openai"
    And the credential variable "OPENAI_API_KEY" is set to "fake-openai-key"

  @happy_path
  Scenario: Operator runs probe-config against a healthy provider and gets a JSON report with exit code 0
    When the operator runs "kairix probe-config"
    Then the command exits with code 0
    And stdout is valid JSON
    And the JSON report names the configured provider
    And the JSON report includes a cold_latency_ms entry for the configured provider
    And the JSON report includes a warm_latency_ms entry for the configured provider
    And the JSON report includes a "status" field with the value "healthy"

  Scenario Outline: Probe-config reports cold and warm latency per configured provider
    Given the operator has configured provider "<name>"
    And the credential variable "<key_env>" is set to "<value_env>"
    When the operator runs "kairix probe-config"
    Then the JSON report includes a cold_latency_ms entry for the configured provider
    And the JSON report includes a warm_latency_ms entry for the configured provider
    And the warm_latency_ms entry is less than or equal to the cold_latency_ms entry

    Examples: First-party providers
      | name          | key_env             | value_env             |
      | azure_foundry | AZURE_OPENAI_KEY    | fake-azure-key        |
      | azure_legacy  | AZURE_OPENAI_KEY    | fake-azure-key        |
      | openai        | OPENAI_API_KEY      | fake-openai-key       |
      | bedrock       | AWS_ACCESS_KEY_ID   | fake-aws-key          |
      | ollama        | OLLAMA_HOST         | http://localhost:11434 |
      | litellm_proxy | LITELLM_PROXY_URL   | http://localhost:4000  |
      | anthropic     | ANTHROPIC_API_KEY   | fake-anthropic-key    |

  Scenario: Probe-config flags a slow warm call with a tuning recommendation
    Given the configured provider answers warm calls in 2500 milliseconds
    When the operator runs "kairix probe-config"
    Then the JSON report includes a "warnings" array with at least one entry
    And one warning names the configured provider as a slow_warm_call source
    And one warning includes a "recommendation" field with non-empty text

  @error
  Scenario: Probe-config exits with code 1 when the configured provider is degraded
    Given the configured provider fails every healthcheck call
    When the operator runs "kairix probe-config"
    Then the command exits with code 1
    And stdout is valid JSON
    And the JSON report includes a "status" field with the value "degraded"
    And the JSON report records the failure mode for the configured provider

  Scenario: Probe-config records the kairix version and probe schema version in the report
    When the operator runs "kairix probe-config"
    Then the JSON report includes a "kairix_version" field
    And the JSON report includes a "schema_version" field
    And the schema_version field matches the schema documented at probe-config-schema.md
