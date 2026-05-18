Feature: End-to-end provider embed journey
  As an operator deploying kairix against any supported LLM endpoint
  I want every configured provider to embed a text string and return
  a vector of the provider's declared dimension
  So that switching the provider is a config change, not a code path,
  and every plugin under kairix/providers/ proves the same contract.

  This is a Scenario Outline parameterised over all installed
  providers. Adding a new provider must add one row to the Examples
  table — not a new feature file. F28 enforces row-per-provider so
  the surface stays mechanical to audit.

  Background:
    Given the kairix provider registry is loaded from installed entry points

  @happy_path
  Scenario Outline: Configured provider embeds a text and returns a vector of its declared dimension
    Given the operator has configured provider "<name>"
    And the credential variable "<key_env>" is set to "<value_env>"
    When the operator embeds the text "<text>"
    Then the result is a vector of dimension <dim>
    And the result envelope records the provider name "<name>"

    Examples: First-party providers with embed support
      | name          | key_env             | value_env       | text                   | dim  |
      | azure_foundry | AZURE_OPENAI_KEY    | fake-azure-key  | example team workspace   | 1536 |
      | azure_legacy  | AZURE_OPENAI_KEY    | fake-azure-key  | example team workspace   | 1536 |
      | openai        | OPENAI_API_KEY      | fake-openai-key | example team workspace   | 1536 |
      | bedrock       | AWS_ACCESS_KEY_ID   | fake-aws-key    | example team workspace   | 1024 |
      | ollama        | OLLAMA_HOST         | http://localhost:11434 | example team workspace | 768  |
      | litellm_proxy | LITELLM_PROXY_URL   | http://localhost:4000  | example team workspace | 1536 |

  @anthropic_no_embed
  Scenario: Anthropic provider rejects embed with a clear, typed error
    Given the operator has configured provider "anthropic"
    And the credential variable "ANTHROPIC_API_KEY" is set to "fake-anthropic-key"
    When the operator embeds the text "example team workspace"
    Then the operator sees a typed EmbedNotSupported error
    And the error names the provider "anthropic"
    And the error suggests configuring a different provider for embeddings

  Scenario: Batch embed returns one vector per input text in order
    Given the operator has configured provider "openai"
    And the credential variable "OPENAI_API_KEY" is set to "fake-openai-key"
    When the operator embeds the batch:
      | first text  |
      | second text |
      | third text  |
    Then the result contains 3 vectors in the same order as the inputs
    And every vector has dimension 1536
