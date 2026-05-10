Feature: Structured search logging for SRE observability
  As an SRE running kairix in production
  I want every search call to append a structured JSONL event to a known path
  So that I can grep recent searches by agent, scope, or query hash without instrumenting the running process

  Background:
    Given a kairix search pipeline wired to a JsonlSearchLogger writing to a temporary search log path

  Scenario: A search call appends a JSONL event the SRE can grep
    When the agent "agent-alpha" runs a search for "hello" with scope "shared+agent"
    Then the search log contains exactly 1 JSONL line
    And the most recent search log event has field "agent" equal to "agent-alpha"
    And the most recent search log event has field "scope" equal to "shared+agent"
    And the most recent search log event has a field "query_hash"
    And the most recent search log event has a field "intent"
    And the most recent search log event has a field "ts"

  Scenario: Multiple searches each append their own line
    When the agent "agent-alpha" runs a search for "hello" with scope "shared+agent"
    And the agent "agent-beta" runs a search for "world" with scope "shared"
    Then the search log contains exactly 2 JSONL lines

  Scenario: Logging failure does not break search
    Given the search log path is unwritable
    When the agent "agent-alpha" runs a search for "hello" with scope "shared+agent"
    Then the search call returned a SearchResult without raising
