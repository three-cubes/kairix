Feature: Search backends route queries to the FTS index
  As an operator running kairix search
  I want each query to reach the BM25 backend (DocumentRepository)
  And produce results derived from the indexed documents
  So that callers see results that actually came from the FTS index

  Background:
    Given a document repository indexed with kairix architecture and runbook docs

  Scenario: BM25 backend returns the document matching the query
    When I search the BM25 backend for "architecture"
    Then the BM25 backend returns 1 result
    And the BM25 result paths include "vault/architecture.md"

  Scenario: BM25 backend honours the collection filter
    When I search the BM25 backend for "kairix" restricted to collection "alpha"
    Then the BM25 backend returns 1 result
    And the BM25 result paths include "vault/agent-alpha-notes.md"
    And the BM25 result paths exclude "vault/architecture.md"

  Scenario: BM25 backend returns no results when nothing matches
    When I search the BM25 backend for "nonexistent-query-token"
    Then the BM25 backend returns 0 results
