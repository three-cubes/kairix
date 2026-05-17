Feature: Operator switches provider by config alone
  As an operator running kairix on any VM or any enterprise cloud
  I want to choose the LLM/embed endpoint by setting one env var
  So that swapping providers is a deploy-time decision, not a code
  change, and a typo on the env var fails fast with a typed error
  listing the providers I actually have installed.

  # The switch journey uses representative pairs across providers — it
  # does not exhaustively cover every plugin in every row. The opt-out
  # tags below acknowledge that the pair-based design intentionally
  # omits azure_legacy / bedrock / anthropic; F28 honours these tags
  # as explicit "not in this journey" markers rather than coverage gaps.
  @azure_legacy_no_switch @bedrock_no_switch @anthropic_no_switch

  Background:
    Given the kairix provider registry is loaded from installed entry points

  @happy_path
  Scenario Outline: Switching the configured provider with no code change keeps embed working
    Given the operator sets KAIRIX_PROVIDER to "<first_provider>"
    And the credential variable "<first_key_env>" is set to "<first_value_env>"
    When the operator embeds the text "example team workspace"
    Then the result envelope records the provider name "<first_provider>"
    Given the operator sets KAIRIX_PROVIDER to "<second_provider>"
    And the credential variable "<second_key_env>" is set to "<second_value_env>"
    When the operator embeds the text "example team workspace"
    Then the result envelope records the provider name "<second_provider>"
    And no kairix source file under kairix/ was modified between the two embeds

    Examples: Cross-provider switches that operators actually run
      | first_provider | first_key_env       | first_value_env       | second_provider | second_key_env       | second_value_env       |
      | openai         | OPENAI_API_KEY      | fake-openai-key       | azure_foundry   | AZURE_OPENAI_KEY     | fake-azure-key         |
      | azure_foundry  | AZURE_OPENAI_KEY    | fake-azure-key        | bedrock         | AWS_ACCESS_KEY_ID    | fake-aws-key           |
      | ollama         | OLLAMA_HOST         | http://localhost:11434 | openai         | OPENAI_API_KEY       | fake-openai-key        |
      | litellm_proxy  | LITELLM_PROXY_URL   | http://localhost:4000 | openai          | OPENAI_API_KEY       | fake-openai-key        |

  @error
  Scenario: Unknown provider name fails with a typed error listing the installed providers
    Given the operator sets KAIRIX_PROVIDER to "nonexistent"
    When the operator embeds the text "example team workspace"
    Then the operator sees a typed ProviderNotRegistered error
    And the error reports the requested name "nonexistent"
    And the error lists every installed provider name under an "available" field
    And the "available" field includes "openai"
    And the "available" field includes "azure_foundry"

  @error
  Scenario: A provider directory shipped without an entry-points registration is invisible to the registry
    Given a provider directory "kairix/providers/orphan/" exists without an entry-points registration
    When the operator sets KAIRIX_PROVIDER to "orphan"
    And the operator embeds the text "example team workspace"
    Then the operator sees a typed ProviderNotRegistered error
    And the "available" field does not include "orphan"

  Scenario: Switching providers reuses one process — no restart required
    Given the operator sets KAIRIX_PROVIDER to "openai"
    And the credential variable "OPENAI_API_KEY" is set to "fake-openai-key"
    When the operator embeds the text "first call"
    And the operator sets KAIRIX_PROVIDER to "azure_foundry"
    And the credential variable "AZURE_OPENAI_KEY" is set to "fake-azure-key"
    And the operator embeds the text "second call"
    Then both embeds succeeded in the same process
    And the second result envelope records the provider name "azure_foundry"
