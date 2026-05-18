Feature: Ollama provider plugin pins the local-host no-auth wire shape
  As an operator running Ollama as a local sidecar
  I want the ollama plugin to send unauthenticated requests to Ollama's
  native /api/embeddings path on the configured local host, and to
  surface connection-refused as a typed ProviderUnreachable error
  So that running kairix offline against a stopped Ollama sidecar
  produces a clear typed failure the caller can match, not a generic
  connection traceback.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the ollama provider configured with model "nomic-embed-text"

  @happy_path
  Scenario: An Ollama embed request targets /api/embeddings with no auth header
    Given the configured endpoint is "http://localhost:11434"
    When the operator embeds a single text via the ollama plugin
    Then the recorded request host is "localhost:11434"
    And the recorded request path equals "/api/embeddings"
    And the recorded request has no header named "Authorization"
    And the recorded request has no header named "api-key"
    And the recorded request body contains model "nomic-embed-text"

  Scenario: Ollama uses its native endpoint shape not the OpenAI one
    Given the configured endpoint is "http://localhost:11434"
    When the operator embeds a single text via the ollama plugin
    Then the recorded request path does not contain "/v1/embeddings"
    And the recorded request path does not contain "/openai/"

  @error
  Scenario: A stopped Ollama sidecar produces a typed ProviderUnreachable error
    Given the configured endpoint is "http://localhost:11434"
    And the wire endpoint refuses the connection
    When the operator embeds a single text via the ollama plugin
    Then the ollama plugin raises a canonical ProviderUnreachable error
    And the error message names the configured provider as "ollama"
    And the error message names the configured endpoint
